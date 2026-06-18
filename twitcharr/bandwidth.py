"""Bandwidth measurement + adaptive quality selection.

Streamlink itself cannot do mid-stream ABR (it pipes one chosen variant out as
a single muxed stream). What we can do — and what this module exists for — is
pick the *highest* Twitch variant whose sustained bitrate still fits in the
user's real connection, and feed Streamlink a left-to-right fallback chain so
the next-best variant takes over if Twitch happens not to expose the preferred
one for a given streamer.

Workflow:

    1. measure_bandwidth_mbps() probes Cloudflare's speedtest CDN by streaming
       a known-size payload and timing it. Cheap, anonymous, no API key.
    2. quality_chain_for_bandwidth(mbps, safety_pct) maps the measured Mbps
       to a comma-separated streamlink quality chain.

The numbers below come from Twitch's published live encoder targets — the
actual on-the-wire bitrate jitters, so we apply a configurable safety margin
(default 30 %) on top.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import requests

logger = logging.getLogger(__name__)

# Cloudflare's anonymous speedtest endpoint — same one speed.cloudflare.com
# uses for its public test. No auth, no tokens, no rate limits we care about.
SPEEDTEST_URL_TEMPLATE = "https://speed.cloudflare.com/__down?bytes={bytes}"
DEFAULT_PAYLOAD_BYTES = 8_000_000  # fast enough for plugin action timeouts
DEFAULT_TIMEOUT_S = 8
DEFAULT_PROBES = 2  # take the *minimum* throughput across N back-to-back probes


# (label, approx upstream bitrate Mbps) — Twitch's published live encoder
# targets. Order matters: highest quality first. Adaptive mode intentionally
# prefers non-60fps labels for Emby/Jellyfin compatibility; users can still
# choose explicit 60fps qualities from the plugin settings.
_TWITCH_VARIANT_BITRATES: list[tuple[str, float]] = [
    ("1080p30", 4.5),
    ("1080p", 4.5),
    ("720p30", 3.0),
    ("720p", 3.0),
    ("480p30", 1.5),
    ("480p", 1.5),
    ("360p30", 0.7),
    ("360p", 0.7),
    ("160p30", 0.3),
    ("160p", 0.3),
]


@dataclass
class BandwidthResult:
    mbps: float
    bytes_downloaded: int
    seconds: float
    source: str

    def as_dict(self) -> dict:
        return {
            "mbps": round(self.mbps, 2),
            "bytes_downloaded": self.bytes_downloaded,
            "seconds": round(self.seconds, 3),
            "source": self.source,
        }


def _single_probe(payload_bytes: int, timeout: int) -> tuple[int, float]:
    """One HTTP-streaming download. Returns (bytes_downloaded, elapsed_seconds).

    Skips the first ~512 KB so TCP slow-start doesn't bias the reading
    downwards — sustained throughput is what matters for streaming.
    """
    url = SPEEDTEST_URL_TEMPLATE.format(bytes=int(payload_bytes))
    headers = {"User-Agent": "Twitcharr-BandwidthProbe", "Accept": "*/*"}

    warmup_bytes = 512 * 1024
    started: float | None = None
    consumed = 0
    measured = 0
    with requests.get(url, stream=True, timeout=timeout, headers=headers) as resp:
        resp.raise_for_status()
        for chunk in resp.iter_content(chunk_size=128 * 1024):
            if not chunk:
                continue
            consumed += len(chunk)
            if started is None:
                if consumed < warmup_bytes:
                    continue
                started = time.monotonic()
                measured = 0
                continue
            measured += len(chunk)
            if time.monotonic() - started > timeout:
                break

    if started is None:
        # Never got past warmup — fall back to whatever we did get.
        return consumed, max(0.001, timeout)
    elapsed = max(0.001, time.monotonic() - started)
    return measured, elapsed


def measure_bandwidth_mbps(
    *,
    payload_bytes: int = DEFAULT_PAYLOAD_BYTES,
    timeout: int = DEFAULT_TIMEOUT_S,
    probes: int = DEFAULT_PROBES,
) -> BandwidthResult:
    """Run `probes` back-to-back downloads and keep the *minimum* throughput.

    Why minimum and not average: streaming has to survive the worst second
    of your network, not the average one. Picking the slowest of N probes is
    cheap, anonymous, and gives us a "no stutter" floor to plan against.
    """
    n = max(1, int(probes))
    samples: list[tuple[int, float]] = []
    for _ in range(n):
        try:
            samples.append(_single_probe(payload_bytes, timeout))
        except Exception as exc:
            logger.warning("Bandwidth probe failed: %s", exc)
            if not samples:
                raise

    rates = [(b * 8) / s / 1_000_000.0 for b, s in samples]
    mbps = min(rates)
    total_bytes = sum(b for b, _ in samples)
    total_seconds = sum(s for _, s in samples)

    logger.info(
        "Bandwidth probe (min of %d): %.2f Mbps (rates=%s, %d bytes in %.2fs)",
        len(rates),
        mbps,
        ", ".join(f"{r:.2f}" for r in rates),
        total_bytes,
        total_seconds,
    )
    return BandwidthResult(
        mbps=mbps,
        bytes_downloaded=total_bytes,
        seconds=total_seconds,
        source=f"cloudflare-speedtest (min of {len(rates)} probes)",
    )


def quality_chain_for_bandwidth(
    mbps: float,
    *,
    safety_margin_pct: int = 50,
) -> str:
    """Return a streamlink quality chain that fits the given bandwidth.

    Picks the highest Twitch variant whose `bitrate * (1 + safety_margin_pct/100)`
    still fits in `mbps`, then appends progressively lower variants as fallbacks
    so the chain is robust when a streamer doesn't expose the preferred variant.
    Always ends with `best` so we never refuse to play.
    """
    if mbps <= 0:
        # No reading — be conservative: pick a mid-tier chain that survives
        # most home connections without pushing 60fps Twitch variants.
        return "720p30,720p,480p30,480p,360p30,360p,best"

    margin = max(0, min(200, int(safety_margin_pct))) / 100.0
    chain: list[str] = []
    started = False
    for label, bitrate in _TWITCH_VARIANT_BITRATES:
        if not started:
            if bitrate * (1.0 + margin) <= mbps:
                started = True
                chain.append(label)
        else:
            chain.append(label)

    if not chain:
        # Bandwidth too low even for 160p safety — let streamlink pick worst.
        return "worst,best"

    if "best" not in chain:
        chain.append("best")
    return ",".join(chain)


def describe_chain_for(mbps: float, *, safety_margin_pct: int = 50) -> dict:
    """Human-readable diagnostic — used by the 'Measure bandwidth' action."""
    chain = quality_chain_for_bandwidth(mbps, safety_margin_pct=safety_margin_pct)
    head = chain.split(",", 1)[0]
    return {
        "measured_mbps": round(mbps, 2),
        "safety_margin_pct": safety_margin_pct,
        "preferred_quality": head,
        "fallback_chain": chain,
    }
