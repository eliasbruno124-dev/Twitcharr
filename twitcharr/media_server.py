"""Emby / Jellyfin Live TV guide refresh trigger.

Both Emby and Jellyfin expose the same `/ScheduledTasks` API. The plugin fetches
the task list, locates the "Refresh Guide" task by `Key == "RefreshGuide"` (or
by name fallback), and POSTs to `/ScheduledTasks/Running/{id}` so the server
re-reads Dispatcharr's guide immediately after each Twitcharr cycle.

The same code path works for:
  * Jellyfin 10.8+ (X-MediaBrowser-Token / X-Emby-Token)
  * Emby 4.x   (X-Emby-Token)
"""

from __future__ import annotations

import logging
import time
from typing import Any

import requests

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 15
IMAGE_WARM_TIMEOUT = 6
IMAGE_WARM_LIMIT = 80
GUIDE_TASK_KEY = "RefreshGuide"


def _headers(api_key: str) -> dict[str, str]:
    return {
        "X-Emby-Token": api_key,
        "X-MediaBrowser-Token": api_key,
        "Authorization": f'MediaBrowser Token="{api_key}"',
        "Accept": "application/json",
        "User-Agent": "Twitcharr",
    }


def _normalize_url(url: str) -> str:
    url = (url or "").strip().rstrip("/")
    if url and not url.lower().startswith(("http://", "https://")):
        url = "http://" + url
    return url


def _extract_items(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, dict):
        items = payload.get("Items")
        if isinstance(items, list):
            return [i for i in items if isinstance(i, dict)]
    if isinstance(payload, list):
        return [i for i in payload if isinstance(i, dict)]
    return []


def _wait_for_task_idle(base: str, headers: dict[str, str], task_id: str, *, max_seconds: int = 12) -> bool:
    deadline = time.time() + max_seconds
    while time.time() < deadline:
        try:
            resp = requests.get(f"{base}/ScheduledTasks", headers=headers, timeout=DEFAULT_TIMEOUT)
            if resp.status_code != 200:
                return False
            tasks = resp.json()
        except Exception:
            return False
        task = next((t for t in tasks if t.get("Id") == task_id), None)
        state = str((task or {}).get("State") or "").lower()
        if state and state not in {"running", "cancelling"}:
            return True
        time.sleep(1)
    return False


def warm_live_tv_image_cache(*, base_url: str, api_key: str, max_images: int = IMAGE_WARM_LIMIT) -> dict[str, Any]:
    """Ask Emby/Jellyfin for Live TV channel/program images so its cache fills sooner."""
    base = _normalize_url(base_url)
    key = (api_key or "").strip()
    if not base or not key:
        return {"status": "skipped", "message": "Emby/Jellyfin URL or API key not configured"}

    headers = _headers(key)
    session = requests.Session()
    warmed = 0
    failures = 0

    endpoints = [
        ("/LiveTv/Channels", {"EnableImages": "true", "ImageTypeLimit": "1", "Limit": str(max_images)}),
        ("/LiveTv/Programs", {"EnableImages": "true", "ImageTypeLimit": "1", "Limit": str(max_images)}),
    ]
    item_ids: list[str] = []
    for path, params in endpoints:
        try:
            resp = session.get(f"{base}{path}", headers=headers, params=params, timeout=DEFAULT_TIMEOUT)
            if resp.status_code != 200:
                failures += 1
                continue
            for item in _extract_items(resp.json()):
                item_id = str(item.get("Id") or "").strip()
                if item_id and item_id not in item_ids:
                    item_ids.append(item_id)
        except Exception:
            logger.exception("Could not list %s for image-cache warmup", path)
            failures += 1

    for item_id in item_ids[:max_images]:
        try:
            resp = session.get(
                f"{base}/Items/{item_id}/Images/Primary",
                headers=headers,
                params={"MaxWidth": "480", "Quality": "85"},
                timeout=IMAGE_WARM_TIMEOUT,
            )
            if resp.status_code == 200:
                warmed += 1
            elif resp.status_code not in (404, 204):
                failures += 1
        except Exception:
            failures += 1

    status = "ok" if warmed or not failures else "error"
    return {
        "status": status,
        "message": f"Warmed {warmed} Live TV image cache entries",
        "images_warmed": warmed,
        "failures": failures,
    }


def trigger_guide_refresh(*, base_url: str, api_key: str) -> dict[str, Any]:
    """Trigger the Live TV guide refresh on Emby or Jellyfin.

    Returns a dict with `status` ("ok" / "skipped" / "error") and a `message`.
    Never raises — failures are non-fatal for the plugin cycle.
    """
    base = _normalize_url(base_url)
    key = (api_key or "").strip()
    if not base or not key:
        return {"status": "skipped", "message": "Emby/Jellyfin URL or API key not configured"}

    headers = _headers(key)

    try:
        list_resp = requests.get(f"{base}/ScheduledTasks", headers=headers, timeout=DEFAULT_TIMEOUT)
    except requests.RequestException as exc:
        return {"status": "error", "message": f"Cannot reach {base}: {exc}"}

    if list_resp.status_code == 401:
        return {"status": "error", "message": "Authentication rejected (check API key)"}
    if list_resp.status_code != 200:
        return {
            "status": "error",
            "message": f"GET /ScheduledTasks failed ({list_resp.status_code}): {list_resp.text[:160]}",
        }

    try:
        tasks = list_resp.json()
    except ValueError:
        return {"status": "error", "message": "Server returned non-JSON for /ScheduledTasks"}

    target = next((t for t in tasks if t.get("Key") == GUIDE_TASK_KEY), None)
    if target is None:
        target = next(
            (
                t
                for t in tasks
                if "guide" in (t.get("Name") or "").lower()
                and "refresh" in (t.get("Name") or "").lower()
            ),
            None,
        )
    if target is None:
        return {"status": "error", "message": "No 'Refresh Guide' scheduled task found on server"}

    task_id = target.get("Id")
    if not task_id:
        return {"status": "error", "message": "Refresh Guide task is missing an Id"}

    try:
        run_resp = requests.post(
            f"{base}/ScheduledTasks/Running/{task_id}",
            headers=headers,
            timeout=DEFAULT_TIMEOUT,
        )
    except requests.RequestException as exc:
        return {"status": "error", "message": f"Trigger request failed: {exc}"}

    if run_resp.status_code in (200, 204):
        _wait_for_task_idle(base, headers, task_id)
        warmup = warm_live_tv_image_cache(base_url=base, api_key=key)
        logger.info("Triggered Emby/Jellyfin guide refresh on %s (task=%s)", base, task_id)
        return {
            "status": "ok",
            "message": (
                f"Triggered '{target.get('Name', 'Refresh Guide')}' on {base}; "
                f"{warmup.get('message', 'image cache warmup checked')}."
            ),
            "task_id": task_id,
            "task_name": target.get("Name", ""),
            "image_cache": warmup,
        }

    return {
        "status": "error",
        "message": f"POST /ScheduledTasks/Running/{task_id} returned {run_resp.status_code}",
    }
