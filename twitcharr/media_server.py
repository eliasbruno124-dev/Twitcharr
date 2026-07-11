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

import json
import logging
import time
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import requests

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 15
TUNER_HOST_TIMEOUT = 6
IMAGE_WARM_TIMEOUT = 6
IMAGE_WARM_LIMIT = 80
# Hard wall-clock budget for the whole warmup. Without it, a slow or
# unreachable media server turns the warmup into up to ~80 sequential
# timeouts (several minutes) — and this code also runs inside a Dispatcharr
# web request when the user clicks a sync action.
IMAGE_WARM_BUDGET_S = 25
GUIDE_TASK_KEY = "RefreshGuide"
TWITCH_TUNER_TAG = "twitch"


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


def _provider_options(host: dict[str, Any]) -> dict[str, Any]:
    options = host.get("ProviderOptions") or {}
    if isinstance(options, str):
        try:
            parsed = json.loads(options)
            return parsed if isinstance(parsed, dict) else {}
        except ValueError:
            return {}
    return options if isinstance(options, dict) else {}


def _is_twitcharr_tuner_host(host: dict[str, Any]) -> bool:
    if str(host.get("Type") or "").lower() != "m3u":
        return False
    options = _provider_options(host)
    raw_tags = options.get("RequiredTags") or []
    if isinstance(raw_tags, str):
        raw_tags = [part.strip() for part in raw_tags.split(",") if part.strip()]
    required_tags = [str(tag).strip().lower() for tag in raw_tags]
    return TWITCH_TUNER_TAG in required_tags


def _safe_m3u_url(existing_url: str, safe_m3u_path: str) -> str:
    current = urlsplit((existing_url or "").strip())
    safe = urlsplit((safe_m3u_path or "").strip())
    if not current.scheme or not current.netloc or not safe.path:
        return ""
    return urlunsplit((current.scheme, current.netloc, safe.path, safe.query, safe.fragment))


def ensure_twitcharr_m3u_tuner(*, base_url: str, api_key: str, safe_m3u_path: str) -> dict[str, Any]:
    """Update Emby/Jellyfin M3U tuner hosts tagged for Twitcharr to the safe URL."""
    base = _normalize_url(base_url)
    key = (api_key or "").strip()
    path = (safe_m3u_path or "").strip()
    if not base or not key or not path:
        return {"status": "skipped", "message": "Media-server URL, API key or M3U path not configured"}

    headers = _headers(key)
    headers["Content-Type"] = "application/json"

    try:
        resp = requests.get(f"{base}/LiveTv/TunerHosts", headers=headers, timeout=TUNER_HOST_TIMEOUT)
    except requests.RequestException as exc:
        return {"status": "error", "message": f"Cannot list tuner hosts: {exc}"}

    if resp.status_code == 404:
        return {"status": "skipped", "message": "Server does not expose /LiveTv/TunerHosts"}
    if resp.status_code == 401:
        return {"status": "error", "message": "Authentication rejected while checking tuner hosts"}
    if resp.status_code != 200:
        return {"status": "error", "message": f"GET /LiveTv/TunerHosts failed ({resp.status_code})"}

    try:
        payload = resp.json()
    except ValueError:
        return {"status": "error", "message": "Server returned non-JSON for /LiveTv/TunerHosts"}
    if isinstance(payload, dict):
        hosts = _extract_items(payload)
    elif isinstance(payload, list):
        hosts = payload
    else:
        return {"status": "error", "message": "Unexpected /LiveTv/TunerHosts response shape"}

    checked = 0
    updated: list[dict[str, str]] = []
    unchanged: list[str] = []
    errors: list[str] = []
    for host in hosts:
        if not isinstance(host, dict) or not _is_twitcharr_tuner_host(host):
            continue
        checked += 1
        host_id = str(host.get("Id") or "")
        old_url = str(host.get("Url") or "")
        new_url = _safe_m3u_url(old_url, path)
        if not new_url:
            errors.append(f"{host_id or 'unknown'}: cannot build safe tuner URL")
            continue
        if old_url == new_url:
            unchanged.append(host_id)
            continue

        payload = dict(host)
        payload["Url"] = new_url
        update_error = ""
        for method in ("POST", "PUT"):
            try:
                update_resp = requests.request(
                    method,
                    f"{base}/LiveTv/TunerHosts",
                    headers=headers,
                    data=json.dumps(payload),
                    timeout=TUNER_HOST_TIMEOUT,
                )
            except requests.RequestException as exc:
                update_error = str(exc)
                continue
            if update_resp.status_code in (200, 204):
                updated.append({"id": host_id, "old_url": old_url, "new_url": new_url})
                update_error = ""
                break
            update_error = f"{method} returned {update_resp.status_code}: {update_resp.text[:160]}"
        if update_error:
            errors.append(f"{host_id or 'unknown'}: {update_error}")

    if errors:
        return {
            "status": "error",
            "message": "Could not update every Twitcharr tuner host",
            "checked": checked,
            "updated": updated,
            "unchanged": unchanged,
            "errors": errors,
        }
    if updated:
        return {
            "status": "ok",
            "message": f"Updated {len(updated)} Twitcharr tuner host(s) to the safe M3U URL",
            "checked": checked,
            "updated": updated,
            "unchanged": unchanged,
        }
    if checked:
        return {
            "status": "ok",
            "message": "Twitcharr tuner host already uses the safe M3U URL",
            "checked": checked,
            "updated": [],
            "unchanged": unchanged,
        }
    return {"status": "skipped", "message": "No M3U tuner host with RequiredTags including Twitch found"}


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
    deadline = time.monotonic() + IMAGE_WARM_BUDGET_S
    budget_exhausted = False

    endpoints = [
        ("/LiveTv/Channels", {"EnableImages": "true", "ImageTypeLimit": "1", "Limit": str(max_images)}),
        ("/LiveTv/Programs", {"EnableImages": "true", "ImageTypeLimit": "1", "Limit": str(max_images)}),
    ]
    item_ids: list[str] = []
    for path, params in endpoints:
        if time.monotonic() >= deadline:
            budget_exhausted = True
            break
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
        if time.monotonic() >= deadline:
            budget_exhausted = True
            break
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
    message = f"Warmed {warmed} Live TV image cache entries"
    if budget_exhausted:
        message += f" (stopped at {IMAGE_WARM_BUDGET_S}s budget)"
    return {
        "status": status,
        "message": message,
        "images_warmed": warmed,
        "failures": failures,
        "budget_exhausted": budget_exhausted,
    }


def trigger_guide_refresh(
    *,
    base_url: str,
    api_key: str,
    safe_m3u_path: str = "",
    ensure_tuner: bool = True,
    warm_images: bool = True,
) -> dict[str, Any]:
    """Trigger the Live TV guide refresh on Emby or Jellyfin.

    Returns a dict with `status` ("ok" / "skipped" / "error") and a `message`.
    Never raises — failures are non-fatal for the plugin cycle.
    """
    base = _normalize_url(base_url)
    key = (api_key or "").strip()
    if not base or not key:
        return {"status": "skipped", "message": "Emby/Jellyfin URL or API key not configured"}

    headers = _headers(key)
    tuner_update = ensure_twitcharr_m3u_tuner(
        base_url=base,
        api_key=key,
        safe_m3u_path=safe_m3u_path,
    ) if ensure_tuner and safe_m3u_path else {
        "status": "skipped",
        "message": "Tuner host check skipped",
    }

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
        warmup = (
            warm_live_tv_image_cache(base_url=base, api_key=key)
            if warm_images
            else {"status": "skipped", "message": "Image cache warmup skipped"}
        )
        logger.info("Triggered Emby/Jellyfin guide refresh on %s (task=%s)", base, task_id)
        status = "partial" if tuner_update.get("status") == "error" else "ok"
        tuner_message = ""
        if tuner_update.get("status") == "error":
            tuner_message = f" Tuner update failed: {tuner_update.get('message', 'unknown error')}."
        return {
            "status": status,
            "message": (
                f"Triggered '{target.get('Name', 'Refresh Guide')}' on {base}; "
                f"{warmup.get('message', 'image cache warmup checked')}."
                f"{tuner_message}"
            ),
            "task_id": task_id,
            "task_name": target.get("Name", ""),
            "tuner_update": tuner_update,
            "image_cache": warmup,
        }

    return {
        "status": "error",
        "message": f"POST /ScheduledTasks/Running/{task_id} returned {run_resp.status_code}",
    }
