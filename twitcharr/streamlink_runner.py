"""Deliver joinable MPEG-TS before Dispatcharr's shared rolling buffer."""
from __future__ import annotations

import os
import re
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
                               stdout=sys.stdout.buffer, stderr=subprocess.PIPE)
        children.append(mux)
        source.stdout.close()
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
        if source.poll() is not None and source.returncode:
            return source.returncode if source.returncode > 0 else 128 - source.returncode
        code = mux.returncode or 0
        return code if code >= 0 else 128 - code
    except OSError:
        print("Twitcharr could not start its media pipeline.", file=sys.stderr)
        return 1
    finally:
        _stop_children(children)
        for child in children:
            if child.stderr:
                child.stderr.close()
        for reader in readers:
            reader.join(timeout=0.5)
        for signum, handler in old_handlers.items():
            signal.signal(signum, handler)


if __name__ == "__main__":
    raise SystemExit(run(sys.argv[1:]))
