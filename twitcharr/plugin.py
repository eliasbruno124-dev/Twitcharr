"""Twitcharr — premium Twitch live-TV plugin for Dispatcharr.

Combines:
  * an auto-updated streamlink-ttvlol twitch.py for ad-bypass low-latency playback
  * a continuously refreshed XMLTV guide (twitch2tuner-style)
  * direct Channel/Stream/EPGData rows in Dispatcharr — no manual M3U/EPG setup
  * Twitch channel discovery (game/top/search) directly inside the lineup field
  * an always-present "no streams online" placeholder so Emby/Jellyfin Live TV
    never collapses to an empty section
  * Emby/Jellyfin guide refresh on every EPG cycle
  * a built-in self-updater that pulls new releases from GitHub
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Manifest / defaults
# ---------------------------------------------------------------------------

PLUGIN_KEY = "twitcharr"
DONATE_URL = "https://github.com/sponsors/eliasbruno124"
GITHUB_REPO_URL = "https://github.com/eliasbruno124/Twitcharr"
DEFAULT_TTVLOL_PROXY_SERVERS = (
    "https://eu.luminous.dev,"
    "https://eu2.luminous.dev,"
    "https://lb-eu.cdn-perfprod.com,"
    "https://lb-eu2.cdn-perfprod.com"
)
DEFAULT_SETTINGS: dict[str, Any] = {
    "channel_group_name": "Twitch",
    "starting_channel_number": 9000,
    "data_dir": "/app/data/plugins/twitcharr",
    "include_offline": True,
    "offline_icon_url": "",
    "use_profile_pic_when_just_chatting": True,
    "epg_refresh_interval_minutes": 2,
    "ttvlol_update_time": "04:30",
    "schedule_enabled": True,
    "ttvlol_proxy_servers": DEFAULT_TTVLOL_PROXY_SERVERS,
    "stream_quality": "adaptive",
    "connection_bandwidth_mbps": 0,
    "bandwidth_safety_margin_pct": 50,
    "enable_low_latency": True,
    "media_server_url": "",
    "media_server_api_key": "",
    "keep_placeholder": True,
    "auto_check_updates": True,
    "use_live_thumbnails": True,
    "fast_startup": True,
    "discord_webhook_url": "",
}


def _load_manifest() -> dict:
    try:
        with open(os.path.join(os.path.dirname(__file__), "plugin.json"), "r", encoding="utf-8") as f:
            return json.loads(f.read())
    except Exception:
        logger.exception("Could not load plugin.json")
        return {}


_MANIFEST = _load_manifest()


def _merge_defaults(settings: dict | None) -> dict:
    merged: dict[str, Any] = dict(DEFAULT_SETTINGS)
    for field in _MANIFEST.get("fields", []):
        field_id = field.get("id")
        if field_id and "default" in field:
            merged[field_id] = field.get("default")
    merged.update(settings or {})
    return merged


# ---------------------------------------------------------------------------
# Settings helpers
# ---------------------------------------------------------------------------


def _load_settings() -> dict:
    try:
        from apps.plugins.models import PluginConfig

        cfg = PluginConfig.objects.filter(key=PLUGIN_KEY).first()
        if cfg and isinstance(cfg.settings, dict):
            return _merge_defaults(cfg.settings)
    except Exception:
        logger.exception("Failed to load plugin settings from DB")
    return _merge_defaults({})


def _save_setting(key: str, value: Any) -> None:
    from apps.plugins.models import PluginConfig

    cfg = PluginConfig.objects.get(key=PLUGIN_KEY)
    settings = dict(cfg.settings or {})
    settings[key] = value
    cfg.settings = settings
    cfg.save(update_fields=["settings", "updated_at"])


def _data_dir(settings: dict) -> str:
    raw = (settings.get("data_dir") or "").strip()
    if raw:
        return raw
    return DEFAULT_SETTINGS["data_dir"]


def _twitch_client(_settings: dict):
    from .twitch_api import TwitchClient

    return TwitchClient()


def _resolve_logins(settings: dict, client) -> list[str]:
    """Parse the channels textarea + the convenience country/trending fields,
    then expand every discovery token to a flat login list."""
    from . import twitch_api as tw

    items = tw.parse_login_list(settings.get("channels") or "")

    # Convenience: dedicated UI fields synthesize discovery tokens so users
    # don't need to know the `top:de:25` syntax.
    top_count = max(0, int(settings.get("discovery_top_count") or 0))
    languages_raw = (settings.get("discovery_top_languages") or "").strip()
    if top_count and languages_raw:
        for code in (c.strip().lower() for c in languages_raw.replace(";", ",").split(",")):
            if code and code.replace("-", "").isalpha():
                items.append({"type": "top", "languages": [code], "limit": top_count})

    trending_count = max(0, int(settings.get("discovery_trending_count") or 0))
    if trending_count:
        items.append({"type": "top", "languages": [], "limit": trending_count})

    if not items:
        return []
    return tw.resolve_logins(client, items)


def _resolve_stream_quality(settings: dict) -> str:
    """Return the quality string passed to streamlink.

    When `stream_quality == "adaptive"` we build a left-to-right fallback
    chain matched to the user's actual or configured bandwidth. Streamlink
    then picks the highest available variant for each stream — and falls
    back gracefully if the streamer doesn't expose that exact one.
    """
    quality = (settings.get("stream_quality") or "best").strip()
    if quality != "adaptive":
        return quality

    from . import bandwidth

    margin = int(settings.get("bandwidth_safety_margin_pct") or 50)
    mbps = float(settings.get("connection_bandwidth_mbps") or 0)
    if mbps <= 0:
        # Use the most recent measured value if the user hasn't pinned one.
        try:
            state = _load_schedule_state(settings)
            measured = float(state.get("last_bandwidth_mbps") or 0)
            if measured > 0:
                mbps = measured
        except Exception:
            pass
    return bandwidth.quality_chain_for_bandwidth(mbps, safety_margin_pct=margin)


# ---------------------------------------------------------------------------
# Internal action helpers (entry-aware, so we never call Twitch twice per cycle)
# ---------------------------------------------------------------------------


def _gather_entries(settings: dict, *, client=None):
    from . import epg

    client = client or _twitch_client(settings)
    logins = _resolve_logins(settings, client)
    if not logins:
        return client, [], []
    # Cache-bust the live preview URL once per minute so Dispatcharr / Emby
    # actually re-fetches the thumbnail instead of serving stale CDN cache.
    cache_bust = int(time.time() // 60)
    entries = epg.build_entries(
        client,
        logins,
        use_profile_pic_when_just_chatting=bool(settings.get("use_profile_pic_when_just_chatting", True)),
        include_offline=bool(settings.get("include_offline", True)),
        offline_icon_url=(settings.get("offline_icon_url") or "").strip(),
        use_live_thumbnails=bool(settings.get("use_live_thumbnails", True)),
        cache_bust=cache_bust,
    )
    return client, logins, entries


def _write_epg(settings: dict, entries: list[dict]) -> dict:
    from . import epg

    data_dir = _data_dir(settings)
    channels, programmes = epg.write_xmltv(entries, epg.xmltv_path(data_dir))
    db_result = epg.upsert_db(entries, data_dir)
    return {
        "channels": channels,
        "programmes": programmes,
        "xmltv_path": epg.xmltv_path(data_dir),
        **db_result,
    }


def _sync_channels_from_entries(settings: dict, entries: list[dict]) -> dict:
    from . import streamlink_setup

    return streamlink_setup.sync_channels(
        entries,
        data_dir=_data_dir(settings),
        group_name=(settings.get("channel_group_name") or DEFAULT_SETTINGS["channel_group_name"]),
        starting_channel_number=int(
            settings.get("starting_channel_number") or DEFAULT_SETTINGS["starting_channel_number"]
        ),
        proxy_servers=(settings.get("ttvlol_proxy_servers") or DEFAULT_TTVLOL_PROXY_SERVERS),
        quality=_resolve_stream_quality(settings),
        low_latency=bool(settings.get("enable_low_latency", True)),
        fast_startup=bool(settings.get("fast_startup", True)),
        keep_placeholder=bool(settings.get("keep_placeholder", True)),
        offline_icon_url=(settings.get("offline_icon_url") or "").strip(),
    )


# ---------------------------------------------------------------------------
# Action implementations
# ---------------------------------------------------------------------------

def _run_update_ttvlol(settings: dict, *, force: bool = False) -> dict:
    from . import ttvlol

    data_dir = _data_dir(settings)
    result = ttvlol.update_ttvlol(data_dir, force=force)
    return {
        "status": "ok",
        "updated": result.updated,
        "release_tag": result.release_tag,
        "path": result.target_path,
        "bytes": result.bytes_written,
        "skipped_reason": result.skipped_reason,
    }


def _run_refresh_epg(settings: dict, *, prebuilt=None) -> dict:
    """Refresh EPG, then trigger the configured Emby/Jellyfin server."""
    from . import ttvlol

    if prebuilt is None:
        client, logins, entries = _gather_entries(settings)
    else:
        client, logins, entries = prebuilt

    if not logins:
        return {"status": "error", "message": "No Twitch logins configured"}

    # Make sure offline placeholders show up in the guide too — write_xmltv
    # mirrors whatever we feed it, but the placeholder channel is created in
    # streamlink_setup, so for guide consistency we always include it here too
    # if there are no live entries.
    epg_entries = list(entries)
    if not any(e.get("live") for e in epg_entries) and bool(settings.get("keep_placeholder", True)):
        from . import streamlink_setup

        epg_entries.append(streamlink_setup._placeholder_entry(
            offline_icon_url=(settings.get("offline_icon_url") or "").strip(),
        ))

    write_result = _write_epg(settings, epg_entries)

    # Opportunistic ttv.lol freshness check (cheap when not due).
    data_dir = _data_dir(settings)
    if ttvlol.needs_check(data_dir, max_age_hours=24):
        try:
            ttvlol.update_ttvlol(data_dir, force=False)
        except Exception:
            logger.exception("Background ttv.lol check failed (non-fatal)")

    refresh_status = _trigger_media_server(settings)
    discord_status = _trigger_discord_go_live(settings, entries)

    return {
        "status": "ok",
        "logins_resolved": len(logins),
        **write_result,
        "media_server_refresh": refresh_status,
        "discord_notifications": discord_status,
    }


def _run_sync_channels(settings: dict, *, prebuilt=None) -> dict:
    if prebuilt is None:
        client, logins, entries = _gather_entries(settings)
    else:
        client, logins, entries = prebuilt
    if not logins:
        return {"status": "error", "message": "No Twitch logins configured"}
    if not entries and not bool(settings.get("keep_placeholder", True)):
        return {"status": "error", "message": "No matching Twitch users found for the configured logins"}
    result = _sync_channels_from_entries(settings, entries)
    return {"status": "ok", **result}


def _run_setup(settings: dict) -> dict:
    """Idempotent one-click setup. Order matters:
        1. ttv.lol so playback works
        2. EPG so EPGData rows exist
        3. Channels so they get linked to those EPGData rows immediately
        4. Media-server refresh so Emby/Jellyfin picks up the lineup at once
    """
    from . import streamlink_setup, ttvlol

    data_dir = _data_dir(settings)
    os.makedirs(data_dir, exist_ok=True)

    ttv_result = ttvlol.update_ttvlol(data_dir, force=False)
    profile = streamlink_setup.get_or_create_stream_profile(
        data_dir=data_dir,
        proxy_servers=(settings.get("ttvlol_proxy_servers") or DEFAULT_TTVLOL_PROXY_SERVERS),
        quality=_resolve_stream_quality(settings),
        low_latency=bool(settings.get("enable_low_latency", True)),
        fast_startup=bool(settings.get("fast_startup", True)),
    )

    from . import epg

    source = epg.get_or_create_epg_source(data_dir)

    result: dict[str, Any] = {
        "status": "ok",
        "data_dir": data_dir,
        "ttvlol_release_tag": ttv_result.release_tag,
        "ttvlol_path": ttv_result.target_path,
        "stream_profile_id": profile.id,
        "epg_source_id": source.id,
        "schedule": _enable_schedule(settings),
    }

    if _settings_have_twitch_inputs(settings):
        prebuilt = _gather_entries(settings)
        client, logins, entries = prebuilt
        if not logins:
            result["next"] = (
                "No Twitch users could be resolved from the configured input. Check the "
                "logins / discovery tokens in the channels field."
            )
            return result
        result["epg"] = _run_refresh_epg(settings, prebuilt=prebuilt)
        result["sync"] = _run_sync_channels(settings, prebuilt=prebuilt)
        result["next"] = "Done. Channels, guide and the auto-updater are active."
    else:
        result["next"] = (
            "Base setup is done and the daily ttv.lol updater is active. Add Twitch logins "
            "(or a discovery token like 'top:de:25'), then run setup again or wait for the "
            "next automatic refresh."
        )
    return result


def _run_all(settings: dict) -> dict:
    """Manual full refresh: ttv.lol + channel sync + EPG. Twitch fetched once."""
    out: dict[str, Any] = {"status": "ok", "steps": {}}
    try:
        out["steps"]["ttvlol"] = _run_update_ttvlol(settings, force=False)
    except Exception as e:
        logger.exception("ttv.lol update failed")
        out["steps"]["ttvlol"] = {"status": "error", "message": str(e)}

    prebuilt = None
    try:
        prebuilt = _gather_entries(settings)
    except Exception as e:
        logger.exception("Twitch fetch failed during run_all")
        out["steps"]["twitch"] = {"status": "error", "message": str(e)}

    if prebuilt is not None:
        try:
            out["steps"]["refresh_epg"] = _run_refresh_epg(settings, prebuilt=prebuilt)
        except Exception as e:
            logger.exception("refresh_epg failed")
            out["steps"]["refresh_epg"] = {"status": "error", "message": str(e)}

        try:
            out["steps"]["sync_channels"] = _run_sync_channels(settings, prebuilt=prebuilt)
        except Exception as e:
            logger.exception("sync_channels failed")
            out["steps"]["sync_channels"] = {"status": "error", "message": str(e)}

    has_error = any(s.get("status") == "error" for s in out["steps"].values())
    out["status"] = "partial" if has_error else "ok"
    return out


# ---------------------------------------------------------------------------
# Media server (Emby / Jellyfin) refresh
# ---------------------------------------------------------------------------


def _trigger_discord_go_live(settings: dict, entries: list[dict]) -> dict:
    """Post a Discord embed for every login that just transitioned to live.

    Compares the current live set against the one persisted in scheduler
    state from the previous cycle. Updates the persisted set after posting,
    so subsequent cycles only post genuinely *new* go-lives.
    """
    webhook = (settings.get("discord_webhook_url") or "").strip()
    if not webhook:
        return {"status": "skipped", "message": "no webhook configured"}

    state = _load_schedule_state(settings)
    previously_live: set[str] = set(state.get("previous_live_logins") or [])
    currently_live: set[str] = {e["login"] for e in entries if e.get("live")}
    newly_live = currently_live - previously_live

    state["previous_live_logins"] = sorted(currently_live)
    _save_schedule_state(settings, state)

    if not newly_live:
        return {"status": "ok", "posted": 0, "newly_live": []}

    new_entries = [e for e in entries if e["login"] in newly_live]
    try:
        from . import notifications

        result = notifications.post_go_live(webhook, new_entries)
        result["newly_live"] = sorted(newly_live)
        return result
    except Exception as exc:
        logger.exception("Discord go-live notification failed")
        return {"status": "error", "message": str(exc)}


def _trigger_media_server(settings: dict) -> dict:
    url = (settings.get("media_server_url") or "").strip()
    key = (settings.get("media_server_api_key") or "").strip()
    if not url or not key:
        return {"status": "skipped", "message": "No Emby/Jellyfin URL or API key configured"}
    try:
        from . import media_server

        return media_server.trigger_guide_refresh(base_url=url, api_key=key)
    except Exception as exc:
        logger.exception("Media-server guide refresh failed")
        return {"status": "error", "message": str(exc)}


def _run_refresh_media_server(settings: dict) -> dict:
    return {"status": "ok", "trigger": _trigger_media_server(settings)}


# ---------------------------------------------------------------------------
# Diagnostics
# ---------------------------------------------------------------------------


def _scheduler_is_running() -> bool:
    return bool(_scheduler_thread and _scheduler_thread.is_alive())


def _run_status(settings: dict) -> dict:
    """Top-to-bottom system check — Twitch API, streamlink, ttv.lol, proxies,
    Emby/Jellyfin, scheduler health, last refresh timestamps, channel counts."""
    from . import diagnostics

    return diagnostics.health_check(
        settings=settings,
        data_dir=_data_dir(settings),
        plugin_version=Plugin.version,
        scheduler_running=_scheduler_is_running(),
    )


def _run_test_proxies(settings: dict) -> dict:
    """Probe every ttv.lol proxy URL and report HTTP code + latency."""
    from . import diagnostics

    csv = settings.get("ttvlol_proxy_servers") or ""
    results = diagnostics.test_proxies(csv, timeout=5.0)
    alive = sum(1 for r in results if r.get("status") == "ok")
    total = len(results)
    return {
        "status": "ok" if alive else ("error" if total else "skipped"),
        "summary": f"{alive}/{total} proxies reachable",
        "proxies": results,
        "next": (
            "Reorder or remove dead proxies in 'ttv.lol proxy servers' to speed up channel switching."
            if alive < total
            else "All configured proxies are reachable."
        ),
    }


def _run_test_discord(settings: dict) -> dict:
    """Send a one-off test embed to the Discord webhook."""
    webhook = (settings.get("discord_webhook_url") or "").strip()
    if not webhook:
        return {"status": "skipped", "message": "discord_webhook_url is not set"}

    from . import notifications

    sample = {
        "login": "twitch",
        "display_name": "Twitch",
        "profile_image_url": "https://static-cdn.jtvnw.net/jtv_user_pictures/8a6381c7-d0c0-4576-b179-38bd5ce1d6af-profile_image-300x300.png",
        "description": "Discord webhook test from Twitcharr",
        "live": True,
        "title": "🔴 Twitch • Test",
        "game_name": "Test",
        "started_at": "",
        "viewer_count": 0,
    }
    return notifications.post_go_live(webhook, [sample])


# ---------------------------------------------------------------------------
# Bandwidth probe (drives the 'adaptive' quality mode)
# ---------------------------------------------------------------------------


def _run_measure_bandwidth(settings: dict) -> dict:
    """Probe Cloudflare's speedtest endpoint, persist the slowest reading,
    and report what quality chain that maps to.
    """
    from . import bandwidth

    try:
        result = bandwidth.measure_bandwidth_mbps()
    except Exception as exc:
        logger.exception("Bandwidth probe failed")
        return {"status": "error", "message": str(exc)}

    try:
        state = _load_schedule_state(settings)
        state["last_bandwidth_mbps"] = round(result.mbps, 2)
        state["last_bandwidth_at"] = int(time.time())
        state["last_bandwidth_source"] = result.source
        _save_schedule_state(settings, state)
    except Exception:
        logger.exception("Could not persist bandwidth probe result")

    margin = int(settings.get("bandwidth_safety_margin_pct") or 50)
    description = bandwidth.describe_chain_for(result.mbps, safety_margin_pct=margin)

    return {
        "status": "ok",
        **result.as_dict(),
        **description,
        "active_now": (settings.get("stream_quality") or "").strip() == "adaptive",
        "next": (
            "Set 'Stream quality' to 'adaptive' (default in v1.0+) and re-run "
            "'Sync channels' so the StreamProfile picks up this chain."
        ),
    }


# ---------------------------------------------------------------------------
# Self-update
# ---------------------------------------------------------------------------


def _current_version() -> str:
    return Plugin.version  # mirrored by the class below


def _run_check_updates(settings: dict) -> dict:
    from . import self_update

    return self_update.check_for_update(
        current_version=_current_version(),
        data_dir=_data_dir(settings),
    )


def _run_apply_update(settings: dict) -> dict:
    from . import self_update

    return self_update.apply_update(
        current_version=_current_version(),
        data_dir=_data_dir(settings),
    )


# ---------------------------------------------------------------------------
# Donate
# ---------------------------------------------------------------------------


def _run_donate(_settings: dict) -> dict:
    return {
        "status": "ok",
        "message": (
            "Thanks for using Twitcharr! If it makes your media server "
            "happy, consider supporting development."
        ),
        "donate_url": DONATE_URL,
        "github_url": GITHUB_REPO_URL,
    }


# ---------------------------------------------------------------------------
# Scheduling
# ---------------------------------------------------------------------------

LEGACY_EPG_TASK_NAME = "twitcharr__refresh_epg"
LEGACY_TTVLOL_TASK_NAME = "twitcharr__update_ttvlol"
SCHEDULER_POLL_SECONDS = 30
UPDATE_CHECK_INTERVAL_SECONDS = 6 * 3600  # check GitHub every 6h

_scheduler_lock = threading.RLock()
_scheduler_stop = threading.Event()
_scheduler_thread: threading.Thread | None = None


def _schedule_state_path(settings: dict) -> str:
    return os.path.join(_data_dir(settings), ".scheduler_state.json")


def _load_schedule_state(settings: dict) -> dict:
    path = _schedule_state_path(settings)
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:
        return {}


def _save_schedule_state(settings: dict, state: dict) -> None:
    path = _schedule_state_path(settings)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f)
    os.replace(tmp, path)


def _job_lock(settings: dict, name: str, ttl_seconds: int = 3600) -> str:
    os.makedirs(_data_dir(settings), exist_ok=True)
    path = os.path.join(_data_dir(settings), f".{name}.lock")
    try:
        if os.path.exists(path) and time.time() - os.path.getmtime(path) > ttl_seconds:
            os.unlink(path)
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(f"{os.getpid()} {int(time.time())}\n")
        return path
    except FileExistsError:
        return ""


def _release_job_lock(path: str) -> None:
    if not path:
        return
    try:
        os.unlink(path)
    except OSError:
        pass


def _interval_minutes(settings: dict) -> int:
    try:
        return max(1, int(settings.get("epg_refresh_interval_minutes") or 2))
    except (TypeError, ValueError):
        return 2


def _ttvlol_update_minute(settings: dict) -> int:
    raw = (settings.get("ttvlol_update_time") or "").strip()
    if not raw:
        cron = (settings.get("ttvlol_update_cron") or "30 4 * * *").strip().split()
        if len(cron) == 5 and cron[0].isdigit() and cron[1].isdigit():
            raw = f"{int(cron[1]):02d}:{int(cron[0]):02d}"
        else:
            raw = "04:30"
    try:
        hour, minute = [int(part) for part in raw.split(":", 1)]
        return max(0, min(23, hour)) * 60 + max(0, min(59, minute))
    except Exception:
        return 4 * 60 + 30


def _ttvlol_update_time_label(settings: dict) -> str:
    minute_of_day = _ttvlol_update_minute(settings)
    return f"{minute_of_day // 60:02d}:{minute_of_day % 60:02d}"


def _ttvlol_due(settings: dict, state: dict, now: float) -> bool:
    from . import ttvlol

    if not os.path.exists(ttvlol.plugin_file(_data_dir(settings))):
        return True
    local = time.localtime(now)
    today = time.strftime("%Y-%m-%d", local)
    now_minute = local.tm_hour * 60 + local.tm_min
    return state.get("last_ttvlol_day") != today and now_minute >= _ttvlol_update_minute(settings)


def _epg_due(settings: dict, state: dict, now: float) -> bool:
    last = float(state.get("last_epg_refresh") or 0)
    return now - last >= _interval_minutes(settings) * 60


def _update_check_due(settings: dict, state: dict, now: float) -> bool:
    if not bool(settings.get("auto_check_updates", True)):
        return False
    last = float(state.get("last_update_check") or 0)
    return now - last >= UPDATE_CHECK_INTERVAL_SECONDS


def _settings_have_twitch_inputs(settings: dict) -> bool:
    return bool((settings.get("channels") or "").strip())


def _run_scheduled_tick() -> None:
    settings = _load_settings()
    if not settings.get("schedule_enabled"):
        return

    now = time.time()
    state = _load_schedule_state(settings)

    if _ttvlol_due(settings, state, now):
        lock = _job_lock(settings, "ttvlol_update", ttl_seconds=30 * 60)
        if lock:
            try:
                result = _run_update_ttvlol(settings, force=False)
                state = _load_schedule_state(settings)
                state.update({
                    "last_ttvlol_check": int(time.time()),
                    "last_ttvlol_day": time.strftime("%Y-%m-%d", time.localtime()),
                    "last_ttvlol_status": "ok",
                    "last_ttvlol_result": result,
                })
                _save_schedule_state(settings, state)
            except Exception as e:
                logger.exception("Scheduled ttv.lol update failed")
                state = _load_schedule_state(settings)
                state.update({
                    "last_ttvlol_check": int(time.time()),
                    "last_ttvlol_status": "error",
                    "last_ttvlol_error": str(e),
                })
                _save_schedule_state(settings, state)
            finally:
                _release_job_lock(lock)

    if _epg_due(settings, state, now):
        lock = _job_lock(settings, "epg_refresh", ttl_seconds=30 * 60)
        if lock:
            try:
                if not _settings_have_twitch_inputs(settings):
                    return
                prebuilt = _gather_entries(settings)
                sync_result = _run_sync_channels(settings, prebuilt=prebuilt)
                epg_result = _run_refresh_epg(settings, prebuilt=prebuilt)
                state = _load_schedule_state(settings)
                state.update({
                    "last_epg_refresh": int(time.time()),
                    "last_epg_status": "ok",
                    "last_sync_result": sync_result,
                    "last_epg_result": epg_result,
                })
                _save_schedule_state(settings, state)
            except Exception as e:
                logger.exception("Scheduled Twitcharr refresh failed")
                state = _load_schedule_state(settings)
                state.update({
                    "last_epg_refresh": int(time.time()),
                    "last_epg_status": "error",
                    "last_epg_error": str(e),
                })
                _save_schedule_state(settings, state)
            finally:
                _release_job_lock(lock)

    if _update_check_due(settings, state, now):
        try:
            check = _run_check_updates(settings)
            state = _load_schedule_state(settings)
            state.update({
                "last_update_check": int(time.time()),
                "update_check": check,
            })
            _save_schedule_state(settings, state)
        except Exception:
            logger.exception("Periodic update check failed (non-fatal)")


def _scheduler_loop() -> None:
    logger.info("Twitcharr self-scheduler started")
    while not _scheduler_stop.is_set():
        try:
            _run_scheduled_tick()
        except Exception:
            logger.exception("Twitcharr scheduler tick failed")
        _scheduler_stop.wait(SCHEDULER_POLL_SECONDS)
    logger.info("Twitcharr self-scheduler stopped")


def _start_scheduler() -> bool:
    global _scheduler_thread
    with _scheduler_lock:
        if _scheduler_thread and _scheduler_thread.is_alive():
            return False
        _scheduler_stop.clear()
        _scheduler_thread = threading.Thread(
            target=_scheduler_loop,
            name="TwitcharrScheduler",
            daemon=True,
        )
        _scheduler_thread.start()
        return True


def _stop_scheduler() -> bool:
    global _scheduler_thread
    with _scheduler_lock:
        if not _scheduler_thread or not _scheduler_thread.is_alive():
            return False
        _scheduler_stop.set()
        _scheduler_thread.join(timeout=5)
        return True


def _delete_legacy_celery_tasks() -> dict:
    try:
        from core.scheduling import delete_periodic_task

        return {
            "legacy_epg_task_removed": bool(delete_periodic_task(LEGACY_EPG_TASK_NAME)),
            "legacy_ttvlol_task_removed": bool(delete_periodic_task(LEGACY_TTVLOL_TASK_NAME)),
        }
    except Exception:
        return {"legacy_epg_task_removed": False, "legacy_ttvlol_task_removed": False}


def _enable_schedule(settings: dict) -> dict:
    _save_setting("schedule_enabled", True)
    merged = _load_settings()
    started = _start_scheduler()
    return {
        "status": "ok",
        "scheduler": "enabled",
        "started_now": started,
        "epg_refresh": f"every {_interval_minutes(merged)} minutes",
        "ttvlol_update": f"daily at {_ttvlol_update_time_label(merged)} server time",
        **_delete_legacy_celery_tasks(),
    }


def _disable_schedule() -> dict:
    try:
        _save_setting("schedule_enabled", False)
    except Exception:
        logger.exception("Could not persist disabled scheduler setting")
    stopped = _stop_scheduler()
    return {"status": "ok", "scheduler": "disabled", "stopped_now": stopped, **_delete_legacy_celery_tasks()}


# ---------------------------------------------------------------------------
# Plugin class
# ---------------------------------------------------------------------------

class Plugin:
    name = "Twitcharr"
    version = "1.0.0"
    description = (
        "One-click Twitch live-TV lineup for Dispatcharr. Type Twitch logins or discovery "
        "tokens (top:de:25, game:Just Chatting:10, search:gronkh) and the plugin auto-creates "
        "low-latency channels, a fresh XMLTV guide, and triggers Emby/Jellyfin guide refreshes "
        "after every cycle. Self-updating."
    )
    author = "eliasbruno124"
    help_url = GITHUB_REPO_URL

    fields: list[dict] = _MANIFEST.get("fields", [])
    actions: list[dict] = _MANIFEST.get("actions", [])

    def __init__(self):
        try:
            if _load_settings().get("schedule_enabled"):
                _start_scheduler()
        except Exception:
            logger.exception("Could not start persisted Twitcharr scheduler")

    def run(self, action: str, params: dict, context: dict):
        settings = (context or {}).get("settings") or {}
        plugin_logger = (context or {}).get("logger") or logger
        params = params or {}

        try:
            if action == "setup":
                return _run_setup(settings)
            if action == "sync_channels":
                return _run_sync_channels(settings)
            if action == "refresh_epg":
                return _run_refresh_epg(settings)
            if action == "update_ttvlol":
                return _run_update_ttvlol(settings, force=bool(params.get("force", True)))
            if action == "run_all":
                return _run_all(settings)
            if action == "refresh_media_server":
                return _run_refresh_media_server(settings)
            if action == "measure_bandwidth":
                return _run_measure_bandwidth(settings)
            if action == "status":
                return _run_status(settings)
            if action == "test_proxies":
                return _run_test_proxies(settings)
            if action == "test_discord":
                return _run_test_discord(settings)
            if action == "check_updates":
                return _run_check_updates(settings)
            if action == "apply_update":
                return _run_apply_update(settings)
            if action == "donate":
                return _run_donate(settings)
            if action == "enable_schedule":
                return _enable_schedule(settings)
            if action == "disable_schedule":
                return _disable_schedule()
            if action == "uninstall":
                from . import streamlink_setup

                _disable_schedule()
                return {"status": "ok", **streamlink_setup.uninstall_managed_objects()}
            return {"status": "error", "message": f"Unknown action: {action}"}
        except Exception as e:
            plugin_logger.exception("Action %s failed", action)
            return {"status": "error", "message": str(e)}

    def stop(self, context: dict):
        try:
            _stop_scheduler()
            if (context or {}).get("reason") in {"disable", "delete"}:
                try:
                    _save_setting("schedule_enabled", False)
                except Exception:
                    logger.exception("Could not persist disabled scheduler setting")
        except Exception:
            logger.exception("Failed to stop Twitcharr scheduler")
