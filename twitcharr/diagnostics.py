"""Health-check + proxy diagnostics for the Twitcharr plugin.

Two pure functions, both safe to call from anywhere:

  * `health_check(...)`  — full system status: scheduler state, last refresh,
    Twitch API reachability, streamlink presence, ttv.lol plugin freshness,
    Emby/Jellyfin handshake, channel/stream counts, proxy latency.
  * `test_proxies(csv)`  — probes each ttv.lol proxy in parallel and reports
    HTTP code + latency.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

import requests

logger = logging.getLogger(__name__)

PROBE_TIMEOUT_S = 4.0


def _humanize_age(ts: float | None) -> str:
    if not ts:
        return "never"
    try:
        delta = time.time() - float(ts)
    except (TypeError, ValueError):
        return "never"
    if delta < 60:
        return f"{int(delta)}s ago"
    if delta < 3600:
        return f"{int(delta // 60)}m ago"
    if delta < 86400:
        return f"{int(delta // 3600)}h ago"
    return f"{int(delta // 86400)}d ago"


# ---------------------------------------------------------------------------
# Sub-checks
# ---------------------------------------------------------------------------


def test_proxies(proxy_servers_csv: str, *, timeout: float = PROBE_TIMEOUT_S) -> list[dict]:
    """HEAD-style probe of each proxy URL. Runs in parallel."""
    urls = [u.strip() for u in (proxy_servers_csv or "").split(",") if u.strip()]
    if not urls:
        return []

    def _one(url: str) -> dict:
        entry: dict[str, Any] = {"url": url}
        start = time.monotonic()
        try:
            resp = requests.get(
                url,
                timeout=timeout,
                allow_redirects=False,
                headers={"User-Agent": "Twitcharr-ProxyProbe"},
            )
            entry.update({
                "status": "ok" if resp.status_code < 500 else "error",
                "http_code": resp.status_code,
                "latency_ms": int((time.monotonic() - start) * 1000),
            })
        except requests.exceptions.Timeout:
            entry.update({"status": "timeout", "latency_ms": int(timeout * 1000)})
        except Exception as exc:
            entry.update({"status": "error", "message": str(exc)})
        return entry

    results: list[dict] = []
    with ThreadPoolExecutor(max_workers=min(8, len(urls))) as pool:
        for fut in as_completed([pool.submit(_one, url) for url in urls]):
            results.append(fut.result())
    # Sort: working first (by latency), then failures.
    results.sort(key=lambda r: (
        0 if r.get("status") == "ok" else (1 if r.get("status") == "timeout" else 2),
        r.get("latency_ms", 99_999),
    ))
    return results


def check_twitch_api() -> dict:
    """Quick anonymous GraphQL ping — same client ID the EPG client uses."""
    try:
        resp = requests.post(
            "https://gql.twitch.tv/gql",
            headers={
                "Client-ID": "kimne78kx3ncx6brgo4mv6wki5h1ko",
                "Content-Type": "application/json",
                "User-Agent": "Twitcharr-Healthcheck",
            },
            json=[{
                "operationName": "ChannelShell",
                "variables": {"login": "twitch"},
                "query": "query ChannelShell($login: String!) { user(login: $login) { id login } }",
            }],
            timeout=6,
        )
        if resp.status_code == 200:
            return {"status": "ok", "http_code": 200}
        return {"status": "error", "http_code": resp.status_code}
    except Exception as exc:
        return {"status": "error", "message": str(exc)}


def check_streamlink() -> dict:
    path = shutil.which("streamlink")
    if not path:
        return {
            "status": "missing",
            "message": "streamlink not on PATH. Install it inside the Dispatcharr container.",
        }
    return {"status": "ok", "path": path}


def check_ttvlol_plugin(data_dir: str) -> dict:
    target = os.path.join(data_dir, "streamlink_plugins", "twitch.py")
    if not os.path.exists(target):
        return {"status": "missing", "path": target}
    try:
        stat = os.stat(target)
    except OSError as exc:
        return {"status": "error", "path": target, "message": str(exc)}
    return {
        "status": "ok",
        "path": target,
        "size_bytes": stat.st_size,
        "age": _humanize_age(stat.st_mtime),
        "modified_at": int(stat.st_mtime),
    }


def check_media_server(*, base_url: str, api_key: str) -> dict:
    if not base_url or not api_key:
        return {"status": "skipped", "message": "Not configured"}
    try:
        from . import media_server

        base = media_server._normalize_url(base_url)
        # /System/Info/Public is unauthenticated on Emby+Jellyfin and is the
        # smallest health endpoint they share.
        resp = requests.get(
            f"{base}/System/Info/Public",
            timeout=6,
            headers={"User-Agent": "Twitcharr-Healthcheck"},
        )
        if resp.status_code != 200:
            return {"status": "error", "http_code": resp.status_code, "url": base}
        info = resp.json() if resp.content else {}
        # Then check that the API key actually works by listing scheduled tasks.
        auth_resp = requests.get(
            f"{base}/ScheduledTasks",
            headers={
                "X-Emby-Token": api_key,
                "X-MediaBrowser-Token": api_key,
                "Authorization": f'MediaBrowser Token="{api_key}"',
                "Accept": "application/json",
            },
            timeout=6,
        )
        api_key_ok = auth_resp.status_code == 200
        return {
            "status": "ok" if api_key_ok else "error",
            "url": base,
            "server_name": info.get("ServerName") or "",
            "product": info.get("ProductName") or "",
            "version": info.get("Version") or "",
            "api_key_valid": api_key_ok,
        }
    except Exception as exc:
        return {"status": "error", "message": str(exc)}


# ---------------------------------------------------------------------------
# Aggregated health check
# ---------------------------------------------------------------------------


def health_check(
    *,
    settings: dict,
    data_dir: str,
    plugin_version: str,
    scheduler_running: bool,
) -> dict[str, Any]:
    state_path = os.path.join(data_dir, ".scheduler_state.json")
    try:
        with open(state_path, "r", encoding="utf-8") as f:
            state = json.load(f) or {}
    except Exception:
        state = {}

    out: dict[str, Any] = {
        "status": "ok",
        "plugin_version": plugin_version,
        "scheduler_running": scheduler_running,
        "data_dir": data_dir,
        "twitch_api": check_twitch_api(),
        "streamlink": check_streamlink(),
        "ttvlol_plugin": check_ttvlol_plugin(data_dir),
        "media_server": check_media_server(
            base_url=(settings.get("media_server_url") or ""),
            api_key=(settings.get("media_server_api_key") or ""),
        ),
        "last_epg_refresh": _humanize_age(state.get("last_epg_refresh")),
        "last_epg_status": state.get("last_epg_status", "never"),
        "last_ttvlol_check": _humanize_age(state.get("last_ttvlol_check")),
        "last_ttvlol_status": state.get("last_ttvlol_status", "never"),
        "last_bandwidth_mbps": state.get("last_bandwidth_mbps"),
        "last_bandwidth_at": _humanize_age(state.get("last_bandwidth_at")),
        "last_update_check": _humanize_age(state.get("last_update_check")),
        "update_check": state.get("update_check"),
        "last_auto_update": _humanize_age(state.get("last_auto_update")),
        "auto_update_result": state.get("auto_update_result"),
    }

    # Channel + EPG counts (best-effort — Django models may not be importable
    # outside Dispatcharr, so wrap defensively).
    try:
        from apps.channels.models import Channel, Stream
        from apps.epg.models import EPGData, EPGSource

        from .epg import EPG_SOURCE_NAME

        out["channels_managed"] = Channel.objects.filter(
            streams__custom_properties__owner="twitcharr"
        ).distinct().count()
        out["streams_managed"] = Stream.objects.filter(
            custom_properties__owner="twitcharr"
        ).count()
        source = EPGSource.objects.filter(name=EPG_SOURCE_NAME).first()
        out["epg_source_status"] = source.status if source else "missing"
        out["epg_data_rows"] = (
            EPGData.objects.filter(epg_source=source).count() if source else 0
        )
    except Exception as exc:
        out["channel_counts_error"] = str(exc)

    out["proxies"] = test_proxies(settings.get("ttvlol_proxy_servers") or "", timeout=3.0)
    proxies_alive = sum(1 for p in out["proxies"] if p.get("status") == "ok")
    out["proxies_summary"] = f"{proxies_alive}/{len(out['proxies'])} reachable"

    failures: list[str] = []
    if out["twitch_api"]["status"] != "ok":
        failures.append("twitch_api")
    if out["streamlink"]["status"] != "ok":
        failures.append("streamlink")
    if out["ttvlol_plugin"]["status"] != "ok":
        failures.append("ttvlol_plugin")
    if out["media_server"]["status"] == "error":
        failures.append("media_server")
    if out["proxies"] and proxies_alive == 0:
        failures.append("proxies (none reachable)")

    if failures:
        out["status"] = "degraded"
        out["degraded_reasons"] = failures
    return out
