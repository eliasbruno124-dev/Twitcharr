"""Run Streamlink through an immediate MPEG-TS normalization stage.

Twitch Enhanced Broadcasting may deliver fragmented MP4. Dispatcharr stores
the StreamProfile output in a rolling buffer which assumes MPEG-TS; an FFmpeg
output profile attached later can therefore miss fMP4's initialization boxes.
This runner remuxes at the source, before Dispatcharr sees the first byte.
"""

from __future__ import annotations

import argparse
import signal
import subprocess
import sys
from collections.abc import Sequence


FFMPEG_MPEGTS_ARGUMENTS = (
    "-hide_banner",
    "-loglevel", "warning",
    "-fflags", "+discardcorrupt+genpts+nobuffer",
    "-probesize", "512K",
    "-analyzeduration", "0",
    "-i", "pipe:0",
    "-map", "0:v:0",
    "-map", "0:a:0?",
    "-sn",
    "-dn",
    "-c:v", "copy",
    "-c:a", "aac",
    "-b:a", "192k",
    "-ac", "2",
    "-max_muxing_queue_size", "4096",
    "-flush_packets", "1",
    "-mpegts_flags", "+pat_pmt_at_frames+resend_headers+initial_discontinuity",
    "-f", "mpegts",
    "pipe:1",
)


def build_streamlink_command(config_path: str, stream_url: str, quality: str) -> list[str]:
    return ["streamlink", "--config", config_path, stream_url, quality or "best"]


def build_ffmpeg_command() -> list[str]:
    return ["ffmpeg", *FFMPEG_MPEGTS_ARGUMENTS]


def _stop_process(process: subprocess.Popen | None) -> None:
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=1)


def run_pipeline(config_path: str, stream_url: str, quality: str) -> int:
    streamlink_process: subprocess.Popen | None = None
    ffmpeg_process: subprocess.Popen | None = None

    def handle_stop(signum, _frame):
        _stop_process(ffmpeg_process)
        _stop_process(streamlink_process)
        raise SystemExit(128 + signum)

    previous_handlers: dict[int, object] = {}
    for signum in (signal.SIGTERM, signal.SIGINT):
        try:
            previous_handlers[signum] = signal.signal(signum, handle_stop)
        except (OSError, ValueError):
            pass

    try:
        streamlink_process = subprocess.Popen(
            build_streamlink_command(config_path, stream_url, quality),
            stdout=subprocess.PIPE,
        )
        if streamlink_process.stdout is None:
            raise RuntimeError("Streamlink stdout pipe was not created")

        ffmpeg_process = subprocess.Popen(
            build_ffmpeg_command(),
            stdin=streamlink_process.stdout,
            stdout=sys.stdout.buffer,
        )
        # FFmpeg owns the pipe now. Closing the duplicate in this process lets
        # Streamlink receive SIGPIPE/EOF promptly when FFmpeg exits.
        streamlink_process.stdout.close()

        ffmpeg_returncode = ffmpeg_process.wait()
        if streamlink_process.poll() is None:
            _stop_process(streamlink_process)
        streamlink_returncode = streamlink_process.wait()
        return ffmpeg_returncode or streamlink_returncode
    except FileNotFoundError as exc:
        print(f"Twitcharr stream runner could not start {exc.filename}", file=sys.stderr)
        return 127
    finally:
        _stop_process(ffmpeg_process)
        _stop_process(streamlink_process)
        for signum, previous in previous_handlers.items():
            signal.signal(signum, previous)


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Normalize a Streamlink stream to MPEG-TS")
    parser.add_argument("--config", required=True, help="Streamlink configuration file")
    parser.add_argument("--quality", default="best", help="Streamlink quality or fallback chain")
    parser.add_argument("stream_url", help="Source URL substituted by Dispatcharr")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    return run_pipeline(args.config, args.stream_url, args.quality)


if __name__ == "__main__":
    raise SystemExit(main())
