"""Dispatcharr Twitch EPG plugin entrypoint.

Combines:
  * an auto-updated streamlink-ttvlol twitch.py for ad-bypass low-latency playback
  * a continuously refreshed XMLTV guide (twitch2tuner-style)
  * direct Channel/Stream/EPGData rows in Dispatcharr — no manual M3U/EPG setup
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

PLUGIN_KEY = "dispatcharr_twitch_epg"
DEFAULT_TTVLOL_PROXY_SERVERS = (
    "https://eu.luminous.dev,"
    "https://eu2.luminous.dev,"
    "https://lb-eu.cdn-perfprod.com,"
    "https://lb-eu2.cdn-perfprod.com"
)
DEFAULT_SETTINGS: dict[str, Any] = {
    "channel_group_name": "Twitch",
    "starting_channel_number": 9000,
    "data_dir": "/app/data/plugins/dispatcharr_twitch_epg",
    "include_offline": True,
    "use_profile_pic_when_just_chatting": True,
    "epg_refresh_interval_minutes": 10,
    "ttvlol_update_time": "04:30",
    "schedule_enabled": True,
    "ttvlol_proxy_servers": DEFAULT_TTVLOL_PROXY_SERVERS,
    "stream_quality": "best",
    "enable_low_latency": True,
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
    """Load this plugin's persisted settings directly from the DB."""
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


def _logins(settings: dict) -> list[str]:
    from .twitch_api import parse_login_list

    return parse_login_list(settings.get("channels") or "")


def _twitch_client(settings: dict):
    from .twitch_api import TwitchClient

    return TwitchClient()


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


def _run_refresh_epg(settings: dict) -> dict:
    from . import epg

    logins = _logins(settings)
    if not logins:
        return {"status": "error", "message": "No Twitch logins configured"}

    client = _twitch_client(settings)
    entries = epg.build_entries(
        client,
        logins,
        use_profile_pic_when_just_chatting=bool(settings.get("use_profile_pic_when_just_chatting", True)),
        include_offline=bool(settings.get("include_offline", True)),
    )
    data_dir = _data_dir(settings)
    channels, programmes = epg.write_xmltv(entries, epg.xmltv_path(data_dir))
    db_result = epg.upsert_db(entries, data_dir)

    # Opportunistic daily ttv.lol check — keeps things working even if the
    # periodic task isn't enabled.
    from . import ttvlol

    if ttvlol.needs_check(data_dir, max_age_hours=24):
        try:
            ttvlol.update_ttvlol(data_dir, force=False)
        except Exception:
            logger.exception("Background ttv.lol check failed (non-fatal)")

    return {
        "status": "ok",
        "channels": channels,
        "programmes": programmes,
        "xmltv_path": epg.xmltv_path(data_dir),
        **db_result,
    }


def _run_sync_channels(settings: dict) -> dict:
    from . import epg, streamlink_setup

    logins = _logins(settings)
    if not logins:
        return {"status": "error", "message": "No Twitch logins configured"}

    client = _twitch_client(settings)
    entries = epg.build_entries(
        client,
        logins,
        use_profile_pic_when_just_chatting=bool(settings.get("use_profile_pic_when_just_chatting", True)),
        include_offline=True,  # sync_channels always wants offline channels too
    )
    if not entries:
        return {"status": "error", "message": "No matching Twitch users found for the configured logins"}

    result = streamlink_setup.sync_channels(
        entries,
        data_dir=_data_dir(settings),
        group_name=(settings.get("channel_group_name") or DEFAULT_SETTINGS["channel_group_name"]),
        starting_channel_number=int(settings.get("starting_channel_number") or DEFAULT_SETTINGS["starting_channel_number"]),
        proxy_servers=(settings.get("ttvlol_proxy_servers") or DEFAULT_TTVLOL_PROXY_SERVERS),
        quality=(settings.get("stream_quality") or DEFAULT_SETTINGS["stream_quality"]),
        low_latency=bool(settings.get("enable_low_latency", True)),
    )
    return {"status": "ok", **result}


def _run_setup(settings: dict) -> dict:
    """Idempotent one-click setup."""
    from . import epg, streamlink_setup, ttvlol

    data_dir = _data_dir(settings)
    os.makedirs(data_dir, exist_ok=True)

    ttv_result = ttvlol.update_ttvlol(data_dir, force=False)
    profile = streamlink_setup.get_or_create_stream_profile(
        data_dir=data_dir,
        proxy_servers=(settings.get("ttvlol_proxy_servers") or DEFAULT_TTVLOL_PROXY_SERVERS),
        quality=(settings.get("stream_quality") or DEFAULT_SETTINGS["stream_quality"]),
        low_latency=bool(settings.get("enable_low_latency", True)),
    )
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
        result["sync_channels"] = _run_sync_channels(settings)
        result["refresh_epg"] = _run_refresh_epg(settings)
        result["next"] = "Done. Channels, guide and daily ttv.lol auto-update are active."
    else:
        result["next"] = (
            "Base setup is done and the daily ttv.lol updater is active. Add Twitch logins, "
            "then run setup again or wait for the next automatic refresh."
        )
    return result


def _run_all(settings: dict) -> dict:
    """Manual full refresh: ttv.lol + channel sync + EPG."""
    out: dict[str, Any] = {"status": "ok", "steps": {}}
    try:
        out["steps"]["ttvlol"] = _run_update_ttvlol(settings, force=False)
    except Exception as e:
        logger.exception("ttv.lol update failed")
        out["steps"]["ttvlol"] = {"status": "error", "message": str(e)}

    try:
        out["steps"]["sync_channels"] = _run_sync_channels(settings)
    except Exception as e:
        logger.exception("sync_channels failed")
        out["steps"]["sync_channels"] = {"status": "error", "message": str(e)}
        # If sync fails, EPG refresh is unlikely to be useful — but try anyway.

    try:
        out["steps"]["refresh_epg"] = _run_refresh_epg(settings)
    except Exception as e:
        logger.exception("refresh_epg failed")
        out["steps"]["refresh_epg"] = {"status": "error", "message": str(e)}

    has_error = any(s.get("status") == "error" for s in out["steps"].values())
    out["status"] = "partial" if has_error else "ok"
    return out


# ---------------------------------------------------------------------------
# Scheduling
# ---------------------------------------------------------------------------

LEGACY_EPG_TASK_NAME = "dispatcharr_twitch_epg__refresh_epg"
LEGACY_TTVLOL_TASK_NAME = "dispatcharr_twitch_epg__update_ttvlol"
SCHEDULER_POLL_SECONDS = 60

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
        return max(1, int(settings.get("epg_refresh_interval_minutes") or 15))
    except (TypeError, ValueError):
        return 15


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


def _settings_have_twitch_inputs(settings: dict) -> bool:
    return bool(_logins(settings))


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
                sync_result = _run_sync_channels(settings)
                epg_result = _run_refresh_epg(settings)
                state = _load_schedule_state(settings)
                state.update({
                    "last_epg_refresh": int(time.time()),
                    "last_epg_status": "ok",
                    "last_sync_result": sync_result,
                    "last_epg_result": epg_result,
                })
                _save_schedule_state(settings, state)
            except Exception as e:
                logger.exception("Scheduled Twitch EPG refresh failed")
                state = _load_schedule_state(settings)
                state.update({
                    "last_epg_refresh": int(time.time()),
                    "last_epg_status": "error",
                    "last_epg_error": str(e),
                })
                _save_schedule_state(settings, state)
            finally:
                _release_job_lock(lock)


def _scheduler_loop() -> None:
    logger.info("Twitch EPG self-scheduler started")
    while not _scheduler_stop.is_set():
        try:
            _run_scheduled_tick()
        except Exception:
            logger.exception("Twitch EPG scheduler tick failed")
        _scheduler_stop.wait(SCHEDULER_POLL_SECONDS)
    logger.info("Twitch EPG self-scheduler stopped")


def _start_scheduler() -> bool:
    global _scheduler_thread
    with _scheduler_lock:
        if _scheduler_thread and _scheduler_thread.is_alive():
            return False
        _scheduler_stop.clear()
        _scheduler_thread = threading.Thread(
            target=_scheduler_loop,
            name="DispatcharrTwitchEPGScheduler",
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
    name = "Twitch EPG"
    version = "0.2.0"
    description = (
        "One-field Twitch live-TV lineup for Dispatcharr: enter Twitch logins only; "
        "ttv.lol updates, low-latency playback and XMLTV guide refresh are automatic."
    )
    author = "Dispatcharr Twitch EPG"
    help_url = "https://github.com/eliasbruno124-dev/Dispatcharr-Twitch-EPG"

    # Mirrored from plugin.json so the plugin still loads in legacy mode.
    fields: list[dict] = _MANIFEST.get("fields", [])
    actions: list[dict] = _MANIFEST.get("actions", [])

    def __init__(self):
        try:
            if _load_settings().get("schedule_enabled"):
                _start_scheduler()
        except Exception:
            logger.exception("Could not start persisted Twitch EPG scheduler")

    # ------------------------------------------------------------------ run
    def run(self, action: str, params: dict, context: dict):
        settings = (context or {}).get("settings") or {}
        plugin_logger = (context or {}).get("logger") or logger

        try:
            if action == "setup":
                return _run_setup(settings)
            if action == "sync_channels":
                return _run_sync_channels(settings)
            if action == "refresh_epg":
                return _run_refresh_epg(settings)
            if action == "update_ttvlol":
                return _run_update_ttvlol(settings, force=bool(params.get("force")))
            if action == "run_all":
                return _run_all(settings)
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

    # ------------------------------------------------------------------ stop
    def stop(self, context: dict):
        try:
            _stop_scheduler()
            if (context or {}).get("reason") in {"disable", "delete"}:
                try:
                    _save_setting("schedule_enabled", False)
                except Exception:
                    logger.exception("Could not persist disabled scheduler setting")
        except Exception:
            logger.exception("Failed to stop Twitch EPG scheduler")
