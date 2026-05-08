"""Auto-updater for the streamlink-ttvlol twitch.py plugin.

Source: https://github.com/2bc4/streamlink-ttvlol/releases/latest/download/twitch.py

The downloaded file lives in <data_dir>/streamlink_plugins/twitch.py and is
fed to streamlink via --plugin-dir (so the system-wide streamlink install is
never modified). Updates are throttled by an ETag and a per-day stamp so the
daily Celery beat job is cheap to run.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from dataclasses import dataclass
from typing import Optional

import requests

logger = logging.getLogger(__name__)

LATEST_URL = "https://github.com/2bc4/streamlink-ttvlol/releases/latest/download/twitch.py"
RELEASES_API = "https://api.github.com/repos/2bc4/streamlink-ttvlol/releases/latest"
DEFAULT_TIMEOUT = 30


@dataclass
class TtvlolUpdateResult:
    updated: bool
    skipped_reason: str = ""
    bytes_written: int = 0
    target_path: str = ""
    release_tag: str = ""
    etag: str = ""


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


def update_ttvlol(data_dir: str, *, force: bool = False) -> TtvlolUpdateResult:
    """Idempotent download. Uses ETag/If-None-Match to avoid pointless writes.

    `force=True` bypasses the conditional request header (still atomic write).
    """
    target = plugin_file(data_dir)
    target_dir = os.path.dirname(target)
    os.makedirs(target_dir, exist_ok=True)

    state = _load_state(data_dir)

    # Best-effort: ask GitHub which release tag we're getting (purely cosmetic
    # for logging — failures are non-fatal).
    release_tag = ""
    try:
        api_resp = requests.get(RELEASES_API, timeout=DEFAULT_TIMEOUT, headers={"Accept": "application/vnd.github+json"})
        if api_resp.status_code == 200:
            release_tag = api_resp.json().get("tag_name", "") or ""
    except Exception:
        pass

    headers = {"User-Agent": "Dispatcharr-TwitchEPG-Plugin"}
    if not force and state.get("etag") and os.path.exists(target):
        headers["If-None-Match"] = state["etag"]

    resp = requests.get(LATEST_URL, headers=headers, timeout=DEFAULT_TIMEOUT, allow_redirects=True)

    if resp.status_code == 304:
        state["last_check"] = int(time.time())
        state["release_tag"] = release_tag or state.get("release_tag", "")
        _save_state(data_dir, state)
        return TtvlolUpdateResult(
            updated=False,
            skipped_reason="not modified (ETag match)",
            target_path=target,
            release_tag=state.get("release_tag", ""),
            etag=state.get("etag", ""),
        )

    if resp.status_code != 200:
        raise RuntimeError(f"Download failed ({resp.status_code}): {resp.text[:200]}")

    body = resp.content
    # Sanity-check that what we got actually looks like the streamlink twitch plugin.
    head = body[:4096].decode("utf-8", errors="ignore")
    if "streamlink" not in head.lower() or "twitch" not in head.lower():
        raise RuntimeError("Downloaded file does not look like a streamlink Twitch plugin; refusing to install")

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
        "release_tag": release_tag or state.get("release_tag", ""),
        "size": len(body),
    }
    _save_state(data_dir, new_state)
    logger.info("streamlink-ttvlol updated: %s bytes, tag=%s", len(body), release_tag or "?")

    return TtvlolUpdateResult(
        updated=True,
        bytes_written=len(body),
        target_path=target,
        release_tag=new_state["release_tag"],
        etag=new_etag,
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
    }


def needs_check(data_dir: str, *, max_age_hours: int = 24) -> bool:
    """True if no plugin file or the last successful check was > max_age_hours ago."""
    target = plugin_file(data_dir)
    if not os.path.exists(target):
        return True
    state = _load_state(data_dir)
    last = state.get("last_check") or 0
    return (time.time() - last) >= max_age_hours * 3600
