"""Auto-updater for the newest verified streamlink-ttvlol twitch.py plugin.

Source: the newest stable GitHub release from 2bc4/streamlink-ttvlol.

The downloaded file lives in <data_dir>/streamlink_plugins/twitch.py and is
fed to streamlink via --plugin-dir (so the system-wide streamlink install is
never modified). Twitcharr never executes a newly downloaded file unless its
size, SHA-256 digest, basic content, and Python syntax all validate.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
import time
from dataclasses import dataclass
import requests

logger = logging.getLogger(__name__)

RELEASES_API = "https://api.github.com/repos/2bc4/streamlink-ttvlol/releases/latest"
ASSET_NAME = "twitch.py"
EXPECTED_DOWNLOAD_PREFIX = "https://github.com/2bc4/streamlink-ttvlol/releases/download/"
DEFAULT_TIMEOUT = 30
MIN_PLUGIN_BYTES = 10_000
MAX_PLUGIN_BYTES = 2_000_000


@dataclass
class TtvlolUpdateResult:
    updated: bool
    skipped_reason: str = ""
    bytes_written: int = 0
    target_path: str = ""
    release_tag: str = ""
    etag: str = ""
    sha256: str = ""


def plugin_dir(data_dir: str) -> str:
    return os.path.join(data_dir, "streamlink_plugins")


def plugin_file(data_dir: str) -> str:
    return os.path.join(plugin_dir(data_dir), "twitch.py")


def _state_file(data_dir: str) -> str:
    return os.path.join(plugin_dir(data_dir), ".state.json")


def _load_state(data_dir: str) -> dict:
    p = _state_file(data_dir)
    if not os.path.exists(p):
        return {}
    try:
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:
        return {}


def _save_state(data_dir: str, state: dict) -> None:
    p = _state_file(data_dir)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f)
    os.replace(tmp, p)


def _sha256_bytes(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def _sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(128 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _latest_release_asset() -> dict[str, str | int]:
    """Return the newest stable twitch.py asset and GitHub's SHA-256 digest."""
    resp = requests.get(
        RELEASES_API,
        headers={"Accept": "application/vnd.github+json", "User-Agent": "Twitcharr"},
        timeout=DEFAULT_TIMEOUT,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"Latest-release check failed ({resp.status_code}): {resp.text[:200]}")
    try:
        release = resp.json()
    except ValueError as exc:
        raise RuntimeError("Latest-release check returned invalid JSON") from exc

    tag = str(release.get("tag_name") or "").strip()
    if not tag or release.get("draft") or release.get("prerelease"):
        raise RuntimeError("GitHub did not return a stable tagged ttv.lol release")
    asset = next(
        (item for item in release.get("assets") or [] if item.get("name") == ASSET_NAME),
        None,
    )
    if not asset:
        raise RuntimeError(f"Latest ttv.lol release {tag} has no {ASSET_NAME} asset")

    url = str(asset.get("browser_download_url") or "").strip()
    digest = str(asset.get("digest") or "").strip().lower()
    size = int(asset.get("size") or 0)
    if not url.startswith(f"{EXPECTED_DOWNLOAD_PREFIX}{tag}/"):
        raise RuntimeError("Latest ttv.lol release returned an unexpected download URL")
    if not digest.startswith("sha256:") or len(digest) != len("sha256:") + 64:
        raise RuntimeError("Latest ttv.lol asset has no usable SHA-256 digest")
    expected_sha256 = digest.removeprefix("sha256:")
    if any(ch not in "0123456789abcdef" for ch in expected_sha256):
        raise RuntimeError("Latest ttv.lol asset returned an invalid SHA-256 digest")
    if not MIN_PLUGIN_BYTES <= size <= MAX_PLUGIN_BYTES:
        raise RuntimeError(f"Latest ttv.lol asset has an unexpected size ({size} bytes)")
    return {
        "release_tag": tag,
        "download_url": url,
        "sha256": expected_sha256,
        "size": size,
    }


def update_ttvlol(data_dir: str, *, force: bool = False) -> TtvlolUpdateResult:
    """Install the newest stable upstream release after SHA-256 verification.

    `force=True` reinstalls the newest asset; verification remains mandatory.
    """
    target = plugin_file(data_dir)
    target_dir = os.path.dirname(target)
    os.makedirs(target_dir, exist_ok=True)

    release = _latest_release_asset()
    release_tag = str(release["release_tag"])
    expected_sha256 = str(release["sha256"])
    expected_size = int(release["size"])
    state = _load_state(data_dir)
    if os.path.exists(target) and not force:
        installed_sha256 = _sha256_file(target)
        if installed_sha256 == expected_sha256:
            state.update({
                "last_check": int(time.time()),
                "release_tag": release_tag,
                "sha256": installed_sha256,
                "size": os.path.getsize(target),
            })
            _save_state(data_dir, state)
            return TtvlolUpdateResult(
                updated=False,
                skipped_reason="newest verified release already installed",
                target_path=target,
                release_tag=release_tag,
                etag=state.get("etag", ""),
                sha256=installed_sha256,
            )

    resp = requests.get(
        str(release["download_url"]),
        headers={"User-Agent": "Twitcharr"},
        timeout=DEFAULT_TIMEOUT,
        allow_redirects=True,
    )

    if resp.status_code != 200:
        raise RuntimeError(f"Download failed ({resp.status_code}): {resp.text[:200]}")

    body = resp.content
    if len(body) != expected_size:
        raise RuntimeError(
            f"Downloaded plugin size does not match GitHub release metadata "
            f"(expected {expected_size}, got {len(body)})"
        )
    if not MIN_PLUGIN_BYTES <= len(body) <= MAX_PLUGIN_BYTES:
        raise RuntimeError(
            f"Downloaded plugin has an unexpected size ({len(body)} bytes); refusing to install"
        )
    actual_sha256 = _sha256_bytes(body)
    if actual_sha256 != expected_sha256:
        raise RuntimeError(
            "Downloaded plugin failed SHA-256 verification; refusing to install "
            f"(expected {expected_sha256}, got {actual_sha256})"
        )
    # Sanity-check that what we got actually looks like the streamlink twitch plugin.
    head = body[:4096].decode("utf-8", errors="ignore")
    if "streamlink" not in head.lower() or "twitch" not in head.lower():
        raise RuntimeError("Downloaded file does not look like a streamlink Twitch plugin; refusing to install")
    try:
        compile(body, target, "exec")
    except SyntaxError as exc:
        raise RuntimeError(f"Downloaded Twitch plugin is not valid Python: {exc}") from exc

    # Atomic write
    fd, tmp_path = tempfile.mkstemp(prefix="twitch.py.", dir=target_dir)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(body)
        os.replace(tmp_path, target)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise

    new_etag = resp.headers.get("ETag", "") or ""
    new_state = {
        "etag": new_etag,
        "last_check": int(time.time()),
        "last_update": int(time.time()),
        "release_tag": release_tag,
        "size": len(body),
        "sha256": actual_sha256,
    }
    _save_state(data_dir, new_state)
    logger.info("streamlink-ttvlol updated: %s bytes, tag=%s", len(body), release_tag)

    return TtvlolUpdateResult(
        updated=True,
        bytes_written=len(body),
        target_path=target,
        release_tag=new_state["release_tag"],
        etag=new_etag,
        sha256=actual_sha256,
    )


def info(data_dir: str) -> dict:
    """Return current state for UI/logging — never raises."""
    state = _load_state(data_dir)
    target = plugin_file(data_dir)
    return {
        "installed": os.path.exists(target),
        "path": target,
        "release_tag": state.get("release_tag", ""),
        "last_update": state.get("last_update"),
        "last_check": state.get("last_check"),
        "size": state.get("size"),
        "sha256": state.get("sha256"),
    }


def needs_check(data_dir: str, *, max_age_hours: int = 24) -> bool:
    """True if no plugin file or the last successful check was > max_age_hours ago."""
    target = plugin_file(data_dir)
    if not os.path.exists(target):
        return True
    state = _load_state(data_dir)
    last = state.get("last_check") or 0
    return (time.time() - last) >= max_age_hours * 3600
