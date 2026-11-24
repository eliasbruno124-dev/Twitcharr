"""Deliver joinable MPEG-TS before Dispatcharr's shared rolling buffer."""
from __future__ import annotations

import os
import re
import select
import shutil
import signal
import subprocess
import sys
import threading
import time

INSPECTION_OPTIONS = frozenset({
    "--help", "-h", "--version", "--plugins", "--json", "-j",
    "--stream-url", "--can-handle-url", "--can-handle-url-no-redirect",
})


def mux_command(ffmpeg: str) -> list[str]:
    # The original Twitch fMP4 init appears once, before the rolling buffer.
    # TS repeats its program tables and survives a later reader joining midstream.
    # Copy both tracks unchanged. Emby's shared output profile keeps its
    # existing audio policy, including for non-Twitch channels using it.
    return [
        ffmpeg, "-hide_banner", "-loglevel", "warning", "-nostats",
        "-fflags", "+discardcorrupt+genpts", "-probesize", "1M",
        "-analyzeduration", "1000000", "-i", "pipe:0",
        "-map", "0:v:0", "-map", "0:a:0?", "-sn", "-dn",
        "-c:v", "copy", "-c:a", "copy",
        "-flush_packets", "1",
        "-mpegts_flags", "+pat_pmt_at_frames+resend_headers+initial_discontinuity",
        "-muxdelay", "0", "-muxpreload", "0", "-f", "mpegts", "pipe:1",
    ]


TS_PACKET_SIZE = 188
# A PID 0x1FFF null packet: every MPEG-TS reader discards it unseen.
TS_NULL_PACKET = b"\x47\x1f\xff\x10" + b"\xff" * (TS_PACKET_SIZE - 4)
# Twitch ad breaks (Streamlink filters them out and pauses its output, often
# for minutes on the hourly mid-roll) and proxy stalls leave the pipe silent.
# Dispatcharr treats ~10 s without bytes as a dead stream and reconnects; the
# new connection plays a few seconds and then meets the same ad break, so the
# channel looped until the break was over. While Streamlink and the muxer are
# still running, null packets keep the connection alive. Dispatcharr hands
# data to its clients in ~256 KB buffer chunks, so the filler must fill one
# every few seconds (about 128 KB/s, a few percent of a normal stream) or the
# clients would see nothing at all. A dead source still ends the pipeline:
# Streamlink's own stream-timeout exits it, and the filler stops after
# KEEPALIVE_MAX_SECONDS so Dispatcharr's health check can take over.
KEEPALIVE_AFTER_SECONDS = 4.0
KEEPALIVE_INTERVAL_SECONDS = 1.0
KEEPALIVE_MAX_SECONDS = 300.0
KEEPALIVE_PACKETS = 700
# Before the first real byte a pre-roll ad (15-30 s, also seen through ttv.lol
# proxies) keeps the pipe silent; Dispatcharr gives a new stream 15 s to send
# data and then reconnects, which plays the next pre-roll. Until the stream
# starts, a thin trickle proves the source is alive without filling
# Dispatcharr's buffer, so clients still start with real video.
PREROLL_AFTER_SECONDS = 2.0
PREROLL_MAX_SECONDS = 90.0
PREROLL_PACKETS = 7
EXIT_READER_GONE = 141  # 128 + SIGPIPE, as if ffmpeg had written to a closed pipe
# Ad blocking in two stages. Playback starts through the ttv.lol proxies. When
# Streamlink reports an ad anyway (pre-roll or mid-roll), the runner switches
# once to a new session straight from Twitch as an embedded player, for which
# Twitch plays far fewer ads. Streamlink and the muxer are restarted together:
# a fragmented-MP4 source cannot be spliced into a running ffmpeg. Dispatcharr
# just keeps receiving MPEG-TS, with a short gap instead of a long ad break.
AD_MARKERS = ("Waiting for pre-roll ads", "Detected advertisement break", "Filtering out segments")
DIRECT_MARKERS = ("falling back to Twitch servers", "excluded from playlist proxy")


def _write_all(fd: int, data: bytes) -> None:
    # Raw fd writes: no BufferedWriter lock that a blocked daemon thread could
    # still hold while the interpreter flushes stdout on exit.
    view = memoryview(data)
    while view:
        view = view[os.write(fd, view):]


def _pump(current: dict, out_fd: int, stopped: threading.Event, reader_gone: threading.Event,
          started_event: threading.Event | None = None, clock=time.monotonic) -> None:
    """Copy the muxer's MPEG-TS to stdout and bridge silent gaps with null packets.

    `current` holds the running pipeline ("fd" of the muxer's stdout, "source",
    "mux", "switching" event); the main loop can replace it at any time. Only
    whole 188-byte packets are forwarded, so a packet cut off by a switch is
    dropped instead of corrupting the stream.
    """
    written = 0
    started = False
    last_data = clock()
    last_filler = 0.0
    filler = TS_NULL_PACKET * KEEPALIVE_PACKETS
    trickle = TS_NULL_PACKET * PREROLL_PACKETS
    pending = b""
    reading_fd = None

    def alive() -> bool:
        return current["source"].poll() is None and current["mux"].poll() is None

    try:
        while not stopped.is_set():
            fd = current["fd"]
            if fd != reading_fd:
                reading_fd, pending = fd, b""
            try:
                ready, _, _ = select.select([fd], [], [], 0.25)
                chunk = os.read(fd, 65536) if ready else None
            except (OSError, ValueError):
                ready, chunk = True, b""
            now = clock()
            if ready:
                if not chunk:
                    if current.get("final"):
                        break  # the runner is done: everything is drained
                    # The pipeline ended; the main loop decides whether a new
                    # one follows (switch or fallback) or the runner ends.
                    time.sleep(0.05)
                    continue
                pending += chunk
                whole = len(pending) - len(pending) % TS_PACKET_SIZE
                if whole:
                    _write_all(out_fd, pending[:whole])
                    pending = pending[whole:]
                    written += whole
                    current["got_data"] = True
                    if not started:
                        started = True
                        if started_event is not None:
                            started_event.set()
                    last_data = now
                continue
            silent = now - last_data
            if now - last_filler < KEEPALIVE_INTERVAL_SECONDS or written % TS_PACKET_SIZE:
                continue
            if current["switching"].is_set() or not alive():
                continue
            if not started and PREROLL_AFTER_SECONDS <= silent <= PREROLL_MAX_SECONDS:
                _write_all(out_fd, trickle)
                written += len(trickle)
                last_filler = now
            elif started and KEEPALIVE_AFTER_SECONDS <= silent <= KEEPALIVE_MAX_SECONDS:
                _write_all(out_fd, filler)
                written += len(filler)
                last_filler = now
    except BrokenPipeError:
        reader_gone.set()
        stopped.set()
    except (OSError, ValueError):
        stopped.set()


def _safe_log(line: str) -> str:
    line = re.sub(r"https?://[^\s\]\"')]+", "<URL>", line)
    line = re.sub(r"(?i)(authorization\s*[:=]\s*)(?:(?:oauth|bearer)\s+)?[^\s,\]\"}]+",
                  r"\1<redacted>", line)
    line = re.sub(r"(?i)(oauth:|bearer\s+|(?:token|password|authorization)[=: ]+)[^\s,\]\"}]+",
                  r"\1<redacted>", line)
    return re.sub(r"\b[A-Za-z0-9_+/=-]{48,}\b", "<opaque>", line).rstrip()


def _relay_errors(pipe, label: str, on_line=None) -> None:
    """Forward warnings and errors; info/debug lines only go to `on_line`."""
    try:
        while True:
            chunk = pipe.readline(8192)
            if not chunk:
                break
            clipped = len(chunk) == 8192 and not chunk.endswith(b"\n")
            raw = chunk.decode("utf-8", errors="replace")
            if on_line is not None:
                on_line(raw)
            text = _safe_log(raw)
            if text and not re.search(r"\]\[(?:info|debug|trace|all)\]", text):
                print(f"[Twitcharr {label}] {text}", file=sys.stderr, flush=True)
            if clipped:
                while chunk and not chunk.endswith(b"\n"):
                    chunk = pipe.readline(8192)
    except (OSError, ValueError):
        pass


def _stop_children(children: list[subprocess.Popen], grace: float = 3.0) -> None:
    # Children stay in Dispatcharr's pipeline process group: its SIGKILL must
    # also reach them. Graceful local shutdown only addresses our direct children.
    for child in children:
        if child.poll() is None:
            try:
                child.terminate()
            except ProcessLookupError:
                pass
    deadline = time.monotonic() + grace
    for child in children:
        try:
            child.wait(timeout=max(0.01, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            try:
                child.kill()
            except ProcessLookupError:
                pass
            child.wait(timeout=2)


def _config_has_proxies(arguments: list[str]) -> bool:
    """True when the Streamlink config passed with --config uses ttv.lol proxies."""
    path = ""
    for i, arg in enumerate(arguments):
        if arg == "--config" and i + 1 < len(arguments):
            path = arguments[i + 1]
        elif arg.startswith("--config="):
            path = arg.split("=", 1)[1]
    try:
        with open(path, encoding="utf-8") as f:
            return any(line.startswith("twitch-proxy-playlist=") and line.split("=", 1)[1].strip() for line in f)
    except OSError:
        return False


def _channel_login(arguments: list[str]) -> str:
    for arg in arguments:
        match = re.search(r"twitch\.tv/([A-Za-z0-9_]+)", arg)
        if match:
            return match.group(1).lower()
    return ""


def run(arguments: list[str], *, streamlink: str | None = None,
        ffmpeg: str | None = None) -> int:
    streamlink = streamlink or shutil.which("streamlink")
    ffmpeg = ffmpeg or shutil.which("ffmpeg")
    if not streamlink or not ffmpeg:
        print("Twitcharr requires streamlink and ffmpeg.", file=sys.stderr)
        return 127
    if any(arg.split("=", 1)[0] in INSPECTION_OPTIONS for arg in arguments):
        # Metadata/URL inspection must remain standard Streamlink CLI behavior.
        os.execv(streamlink, [streamlink, *arguments])

    children: list[subprocess.Popen] = []
    readers: list[threading.Thread] = []
    stopped = threading.Event()
    reader_gone = threading.Event()
    started = threading.Event()
    ad_seen = threading.Event()
    direct = threading.Event()
    received_signal = [0]
    old_handlers = {}
    current: dict = {"switching": threading.Event()}
    login = _channel_login(arguments)
    can_switch = bool(login) and _config_has_proxies(arguments)

    def stop(signum, _frame):
        received_signal[0] = signum
        stopped.set()

    def watch(line: str) -> None:
        if any(marker in line for marker in DIRECT_MARKERS):
            direct.set()
        if any(marker in line for marker in AD_MARKERS):
            ad_seen.set()

    def start_pipeline(embed: bool) -> None:
        ad_seen.clear()
        if embed:
            direct.set()
        else:
            direct.clear()
        pipe_r, pipe_w = os.pipe()
        try:
            mux = subprocess.Popen(mux_command(ffmpeg), stdin=pipe_r,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        finally:
            os.close(pipe_r)
        children.append(mux)
        # Info level shows Streamlink's ad notices; the relay keeps those lines
        # out of Dispatcharr's log. Excluding the channel from the proxy makes
        # Streamlink ask Twitch directly, as the embedded player from the config.
        extra = ["--loglevel", "info"]
        if embed:
            extra.append(f"--twitch-proxy-playlist-exclude={login}")
        try:
            source = subprocess.Popen([streamlink, *extra, *arguments],
                                      stdout=pipe_w, stderr=subprocess.PIPE)
        finally:
            os.close(pipe_w)
        children.append(source)
        current.update(source=source, mux=mux, fd=mux.stdout.fileno(), got_data=False)
        for child, label, hook in ((source, "source", watch), (mux, "mux", None)):
            reader = threading.Thread(target=_relay_errors, args=(child.stderr, label, hook), daemon=True)
            reader.start()
            readers.append(reader)

    for signum in (signal.SIGTERM, signal.SIGINT):
        old_handlers[signum] = signal.signal(signum, stop)
    try:
        start_pipeline(embed=False)
        switched = False
        fell_back = False
        sys.stdout.flush()
        pump = threading.Thread(target=_pump, args=(current, sys.stdout.fileno(), stopped, reader_gone, started),
                                daemon=True)
        pump.start()
        readers.append(pump)
        source_done = None
        while not stopped.is_set():
            source = current["source"]
            if can_switch and not switched and ad_seen.is_set() and not direct.is_set():
                switched = True
                stage = "mid-roll" if started.is_set() else "pre-roll"
                print(f"Twitcharr: {stage} ad through ttv.lol, switching to the Twitch embed player",
                      file=sys.stderr, flush=True)
                current["switching"].set()
                try:
                    _stop_children([source, current["mux"]], grace=2.0)
                    if stopped.is_set():
                        break
                    start_pipeline(embed=True)
                finally:
                    current["switching"].clear()
                source_done = None
                continue
            if source.poll() is not None:
                if source.returncode and switched and not fell_back and not current.get("got_data"):
                    # The direct session failed before delivering anything (for
                    # example Twitch wanting a client-integrity token): go back
                    # to ttv.lol once and let Streamlink filter the ad there.
                    fell_back = True
                    print("Twitcharr: embed player session failed, back to ttv.lol", file=sys.stderr, flush=True)
                    current["switching"].set()
                    try:
                        _stop_children([current["mux"]], grace=2.0)
                        if stopped.is_set():
                            break
                        start_pipeline(embed=False)
                    finally:
                        current["switching"].clear()
                    source_done = None
                    continue
                if source.returncode:
                    return source.returncode if source.returncode > 0 else 128 - source.returncode
                if current["mux"].poll() is not None:
                    break
                source_done = source_done or time.monotonic()
                if time.monotonic() - source_done > 5:
                    print("Twitcharr mux did not finish after source EOF.", file=sys.stderr)
                    return 1
            elif current["mux"].poll() is not None:
                break
            stopped.wait(0.05)
        if received_signal[0]:
            return 128 + received_signal[0]
        if reader_gone.is_set():
            return EXIT_READER_GONE
        source = current["source"]
        if source.poll() is not None and source.returncode:
            return source.returncode if source.returncode > 0 else 128 - source.returncode
        code = current["mux"].returncode or 0
        return code if code >= 0 else 128 - code
    except OSError:
        print("Twitcharr could not start its media pipeline.", file=sys.stderr)
        return 1
    finally:
        current["final"] = True
        _stop_children(children)
        # Children are gone: let the pump drain the muxer's last bytes to EOF.
        for reader in readers:
            reader.join(timeout=1.0)
        stopped.set()
        for child in children:
            for stream in (child.stdout, child.stderr):
                if stream:
                    stream.close()
        for signum, handler in old_handlers.items():
            signal.signal(signum, handler)


if __name__ == "__main__":
    raise SystemExit(run(sys.argv[1:]))
