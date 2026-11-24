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


def _write_all(fd: int, data: bytes) -> None:
    # Raw fd writes: no BufferedWriter lock that a blocked daemon thread could
    # still hold while the interpreter flushes stdout on exit.
    view = memoryview(data)
    while view:
        view = view[os.write(fd, view):]


def _pump(src_fd: int, out_fd: int, source: subprocess.Popen, mux: subprocess.Popen,
          stopped: threading.Event, reader_gone: threading.Event, clock=time.monotonic) -> None:
    """Copy the muxer's MPEG-TS to stdout and bridge silent gaps with null packets."""
    written = 0
    started = False
    last_data = clock()
    last_filler = 0.0
    filler = TS_NULL_PACKET * KEEPALIVE_PACKETS
    trickle = TS_NULL_PACKET * PREROLL_PACKETS
    try:
        while not stopped.is_set():
            ready, _, _ = select.select([src_fd], [], [], 0.25)
            now = clock()
            if ready:
                chunk = os.read(src_fd, 65536)
                if not chunk:
                    break
                _write_all(out_fd, chunk)
                written += len(chunk)
                started = True
                last_data = now
                continue
            silent = now - last_data
            if now - last_filler < KEEPALIVE_INTERVAL_SECONDS or written % TS_PACKET_SIZE:
                continue
            if source.poll() is not None or mux.poll() is not None:
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


def _relay_errors(pipe, label: str) -> None:
    try:
        while True:
            chunk = pipe.readline(8192)
            if not chunk:
                break
            clipped = len(chunk) == 8192 and not chunk.endswith(b"\n")
            text = _safe_log(chunk.decode("utf-8", errors="replace"))
            if text:
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
    received_signal = [0]
    old_handlers = {}

    def stop(signum, _frame):
        received_signal[0] = signum
        stopped.set()

    for signum in (signal.SIGTERM, signal.SIGINT):
        old_handlers[signum] = signal.signal(signum, stop)
    try:
        source = subprocess.Popen([streamlink, *arguments], stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE)
        children.append(source)
        mux = subprocess.Popen(mux_command(ffmpeg), stdin=source.stdout,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        children.append(mux)
        source.stdout.close()
        sys.stdout.flush()
        pump = threading.Thread(
            target=_pump,
            args=(mux.stdout.fileno(), sys.stdout.fileno(), source, mux, stopped, reader_gone),
            daemon=True,
        )
        pump.start()
        readers.append(pump)
        for child, label in ((source, "source"), (mux, "mux")):
            reader = threading.Thread(target=_relay_errors, args=(child.stderr, label), daemon=True)
            reader.start()
            readers.append(reader)
        source_done = None
        while mux.poll() is None and not stopped.is_set():
            if source.poll() is not None:
                if source.returncode:
                    return source.returncode if source.returncode > 0 else 128 - source.returncode
                source_done = source_done or time.monotonic()
                if time.monotonic() - source_done > 5:
                    print("Twitcharr mux did not finish after source EOF.", file=sys.stderr)
                    return 1
            stopped.wait(0.05)
        if received_signal[0]:
            return 128 + received_signal[0]
        if reader_gone.is_set():
            return EXIT_READER_GONE
        if source.poll() is not None and source.returncode:
            return source.returncode if source.returncode > 0 else 128 - source.returncode
        code = mux.returncode or 0
        return code if code >= 0 else 128 - code
    except OSError:
        print("Twitcharr could not start its media pipeline.", file=sys.stderr)
        return 1
    finally:
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
