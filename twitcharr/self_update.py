"""In-Dispatcharr self-updater.

Pulls the latest plugin release from GitHub and applies it over the running
plugin directory. The flow:

    1. GET /repos/.../releases/latest -> tag_name + zipball_url
    2. compare against the installed version
    3. download zip, extract to a temp dir
    4. atomically copy every file under that single root folder into the
       installed plugin directory, including plugin.json so fields/actions and
       version metadata stay in sync with the release)
    5. mark a sentinel so the user knows a Dispatcharr 'Reload plugins'
       (or container restart) is required to pick up the new code

The updater never raises out of the action handler — every error is mapped
into a structured response.
"""

from __future__ import annotations

import io
import json
import logging
import os
import shutil
import tempfile
import time
import zipfile
from typing import Any

import requests

logger = logging.getLogger(__name__)

GITHUB_REPO = "eliasbruno124-dev/Dispatcharr-Twitch-EPG"
RELEASES_API = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
ARCHIVE_FALLBACK = f"https://github.com/{GITHUB_REPO}/archive/refs/heads/main.zip"
DEFAULT_TIMEOUT = 30

# Files that come from the upstream release but should never be written into
# the live plugin directory (or whose presence is irrelevant at runtime).
SKIP_FILES = {".gitignore", ".gitattributes", "LICENSE", "README.md"}


def _plugin_dir() -> str:
    return os.path.dirname(os.path.abspath(__file__))


def _data_state_path(data_dir: str) -> str:
    return os.path.join(data_dir, ".self_update_state.json")


def _read_state(data_dir: str) -> dict:
    p = _data_state_path(data_dir)
    if not os.path.exists(p):
        return {}
    try:
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:
        return {}


def _write_state(data_dir: str, state: dict) -> None:
    p = _data_state_path(data_dir)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, p)


def _normalize(version: str) -> str:
    return (version or "").lstrip("vV").strip()


def _is_newer(latest: str, current: str) -> bool:
    """Best-effort semver comparison; falls back to string inequality."""
    a = _normalize(latest).split(".")
    b = _normalize(current).split(".")
    try:
        for ax, bx in zip(a + ["0"] * 3, b + ["0"] * 3):
            ai, bi = int(ax.split("-", 1)[0] or 0), int(bx.split("-", 1)[0] or 0)
            if ai != bi:
                return ai > bi
        return False
    except ValueError:
        return _normalize(latest) != _normalize(current)


def _fetch_release() -> dict:
    resp = requests.get(
        RELEASES_API,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "Twitcharr-SelfUpdate",
        },
        timeout=DEFAULT_TIMEOUT,
    )
    if resp.status_code == 404:
        return {"tag_name": "main", "zipball_url": ARCHIVE_FALLBACK, "_fallback": True}
    if resp.status_code != 200:
        raise RuntimeError(f"GitHub release lookup failed ({resp.status_code})")
    return resp.json() or {}


def check_for_update(*, current_version: str, data_dir: str) -> dict[str, Any]:
    """Look up the latest release on GitHub and report whether an update exists."""
    try:
        info = _fetch_release()
    except Exception as exc:
        return {"status": "error", "message": str(exc)}

    latest = _normalize(info.get("tag_name") or "")
    state = _read_state(data_dir)
    state.update({
        "last_check": int(time.time()),
        "latest_release": latest,
        "github_repo": GITHUB_REPO,
    })
    _write_state(data_dir, state)

    available = bool(latest) and not bool(info.get("_fallback")) and _is_newer(latest, current_version)
    return {
        "status": "ok",
        "message": (
            f"Update available: {latest}."
            if available
            else f"No plugin update available (current {_normalize(current_version)}, latest {latest or 'unknown'})."
        ),
        "current_version": _normalize(current_version),
        "latest_version": latest,
        "update_available": available,
        "release_url": info.get("html_url") or f"https://github.com/{GITHUB_REPO}/releases/latest",
        "release_notes": (info.get("body") or "")[:2000],
        "is_fallback_main": bool(info.get("_fallback")),
    }


def _extract_release_zip(zip_bytes: bytes, dest_dir: str) -> str:
    """Extract a GitHub release zipball and return the inner top-level dir."""
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        zf.extractall(dest_dir)
    entries = [e for e in os.listdir(dest_dir) if not e.startswith(".")]
    if len(entries) == 1 and os.path.isdir(os.path.join(dest_dir, entries[0])):
        return os.path.join(dest_dir, entries[0])
    return dest_dir


def _find_plugin_subdir(extracted_root: str) -> str:
    candidate = os.path.join(extracted_root, "twitcharr")
    if os.path.isdir(candidate):
        return candidate
    return extracted_root


def apply_update(*, current_version: str, data_dir: str) -> dict[str, Any]:
    """Download the latest release and overwrite the active plugin directory.

    Saved plugin settings live in the Dispatcharr database, so replacing
    plugin.json is safe and keeps the UI manifest in sync with plugin.py.
    """
    try:
        info = _fetch_release()
    except Exception as exc:
        return {"status": "error", "message": f"Cannot reach GitHub: {exc}"}

    latest = _normalize(info.get("tag_name") or "")
    zip_url = info.get("zipball_url") or ARCHIVE_FALLBACK
    if not zip_url:
        return {"status": "error", "message": "Release does not expose a zipball_url"}

    if latest and not _is_newer(latest, current_version):
        return {
            "status": "ok",
            "applied": False,
            "message": f"Already on {_normalize(current_version)} (latest {latest}).",
            "latest_version": latest,
            "current_version": _normalize(current_version),
        }

    try:
        zip_resp = requests.get(
            zip_url,
            timeout=DEFAULT_TIMEOUT * 2,
            headers={"User-Agent": "Twitcharr-SelfUpdate"},
            allow_redirects=True,
        )
        if zip_resp.status_code != 200:
            return {"status": "error", "message": f"Download failed ({zip_resp.status_code})"}
        payload = zip_resp.content
    except Exception as exc:
        return {"status": "error", "message": f"Download error: {exc}"}

    plugin_dir = _plugin_dir()
    files_written = 0

    with tempfile.TemporaryDirectory(prefix="twitch-epg-update-") as tmp:
        try:
            extracted = _extract_release_zip(payload, tmp)
            src_root = _find_plugin_subdir(extracted)
        except zipfile.BadZipFile:
            return {"status": "error", "message": "Downloaded archive is not a valid zip"}

        for root, _dirs, files in os.walk(src_root):
            rel_root = os.path.relpath(root, src_root)
            for fname in files:
                if fname in SKIP_FILES:
                    continue
                src = os.path.join(root, fname)
                dest_rel = fname if rel_root == "." else os.path.join(rel_root, fname)
                dest = os.path.join(plugin_dir, dest_rel)
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                shutil.copy2(src, dest)
                files_written += 1

    state = _read_state(data_dir)
    state.update({
        "last_update": int(time.time()),
        "applied_version": latest or "main",
        "files_written": files_written,
    })
    _write_state(data_dir, state)

    logger.info("Twitcharr plugin self-updated to %s (%d files)", latest, files_written)

    return {
        "status": "ok",
        "applied": True,
        "message": f"Plugin update applied: {_normalize(current_version)} -> {latest or 'main'}. Reload plugins or restart Dispatcharr.",
        "files_written": files_written,
        "latest_version": latest,
        "previous_version": _normalize(current_version),
        "next": (
            "Update applied. Reload Dispatcharr's plugin list (or restart the container) "
            "for the new code to take effect."
        ),
    }
