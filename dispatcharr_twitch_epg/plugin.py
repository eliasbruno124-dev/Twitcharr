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
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Celery tasks
# ---------------------------------------------------------------------------
# Both tasks are exported under stable names. Celery autodiscovers tasks
# decorated with @shared_task on plugin import; periodic tasks reference them
# via the `name=` we set here, so the path stays valid even when Dispatcharr
# imports plugins under a synthetic package.

try:  # pragma: no cover - celery is part of Dispatcharr's runtime
    from celery import shared_task

    @shared_task(name="dispatcharr_twitch_epg.refresh_epg")
    def task_refresh_epg() -> dict:
        return _run_refresh_epg(_load_settings())

    @shared_task(name="dispatcharr_twitch_epg.update_ttvlol")
    def task_update_ttvlol() -> dict:
        return _run_update_ttvlol(_load_settings(), force=False)

    @shared_task(name="dispatcharr_twitch_epg.run_all")
    def task_run_all() -> dict:
        return _run_all(_load_settings())

except Exception:  # pragma: no cover - dev/test without celery
    task_refresh_epg = task_update_ttvlol = task_run_all = None
    logger.warning("Celery not available; periodic scheduling disabled.")


# ---------------------------------------------------------------------------
# Settings helpers
# ---------------------------------------------------------------------------

PLUGIN_KEY = "dispatcharr_twitch_epg"


def _load_settings() -> dict:
    """Load this plugin's persisted settings directly from the DB. Used by
    Celery tasks where no `context` argument is available.
    """
    try:
        from apps.plugins.models import PluginConfig

        cfg = PluginConfig.objects.filter(key=PLUGIN_KEY).first()
        if cfg and isinstance(cfg.settings, dict):
            return cfg.settings
    except Exception:
        logger.exception("Failed to load plugin settings from DB")
    return {}


def _data_dir(settings: dict) -> str:
    raw = (settings.get("data_dir") or "").strip()
    if raw:
        return raw
    return "/app/data/plugins/dispatcharr_twitch_epg"


def _logins(settings: dict) -> list[str]:
    from .twitch_api import parse_login_list

    return parse_login_list(settings.get("channels") or "")


def _twitch_client(settings: dict):
    from .twitch_api import TwitchClient, TwitchAuthError

    cid = (settings.get("client_id") or "").strip()
    csec = (settings.get("client_secret") or "").strip()
    if not cid or not csec:
        raise TwitchAuthError("Twitch Client ID and Client Secret must be set in plugin settings")
    return TwitchClient(cid, csec)


# ---------------------------------------------------------------------------
# Action implementations (also called from Celery tasks)
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
        group_name=(settings.get("channel_group_name") or "Twitch"),
        starting_channel_number=int(settings.get("starting_channel_number") or 9000),
        proxy_servers=(settings.get("ttvlol_proxy_servers") or ""),
        quality=(settings.get("stream_quality") or "best"),
        low_latency=bool(settings.get("enable_low_latency", True)),
    )
    return {"status": "ok", **result}


def _run_setup(settings: dict) -> dict:
    """Idempotent first-run: download ttv.lol, create stream profile + EPG source."""
    from . import epg, streamlink_setup, ttvlol

    data_dir = _data_dir(settings)
    os.makedirs(data_dir, exist_ok=True)

    ttv_result = ttvlol.update_ttvlol(data_dir, force=False)
    profile = streamlink_setup.get_or_create_stream_profile(
        data_dir=data_dir,
        proxy_servers=(settings.get("ttvlol_proxy_servers") or ""),
        quality=(settings.get("stream_quality") or "best"),
        low_latency=bool(settings.get("enable_low_latency", True)),
    )
    source = epg.get_or_create_epg_source(data_dir)

    return {
        "status": "ok",
        "data_dir": data_dir,
        "ttvlol_release_tag": ttv_result.release_tag,
        "ttvlol_path": ttv_result.target_path,
        "stream_profile_id": profile.id,
        "epg_source_id": source.id,
        "next": "Configure Twitch logins, then click 'Sync channels' and 'Refresh EPG now'.",
    }


def _run_all(settings: dict) -> dict:
    """Used by both the manual button and the periodic Celery task."""
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

EPG_TASK_NAME = "dispatcharr_twitch_epg__refresh_epg"
TTVLOL_TASK_NAME = "dispatcharr_twitch_epg__update_ttvlol"


def _enable_schedule(settings: dict) -> dict:
    from core.scheduling import create_or_update_periodic_task

    interval = max(1, int(settings.get("epg_refresh_interval_minutes") or 15))
    cron = (settings.get("ttvlol_update_cron") or "30 4 * * *").strip()

    # EPG refresh: cron-with-minute-step is more accurate than IntervalSchedule
    # for sub-hour intervals. Fall back to interval when interval is hour-aligned.
    epg_cron = f"*/{interval} * * * *" if interval < 60 else ""
    epg_interval_hours = (interval // 60) if interval >= 60 else 0

    epg_task = create_or_update_periodic_task(
        task_name=EPG_TASK_NAME,
        celery_task_path="dispatcharr_twitch_epg.refresh_epg",
        interval_hours=epg_interval_hours,
        cron_expression=epg_cron,
        enabled=True,
    )
    ttv_task = create_or_update_periodic_task(
        task_name=TTVLOL_TASK_NAME,
        celery_task_path="dispatcharr_twitch_epg.update_ttvlol",
        cron_expression=cron,
        enabled=True,
    )
    return {
        "status": "ok",
        "epg_task": epg_task.name,
        "epg_schedule": epg_cron or f"every {epg_interval_hours}h",
        "ttvlol_task": ttv_task.name,
        "ttvlol_schedule": cron,
    }


def _disable_schedule() -> dict:
    from core.scheduling import delete_periodic_task

    epg = delete_periodic_task(EPG_TASK_NAME)
    ttv = delete_periodic_task(TTVLOL_TASK_NAME)
    return {"status": "ok", "removed_epg_task": bool(epg), "removed_ttvlol_task": bool(ttv)}


# ---------------------------------------------------------------------------
# Plugin class
# ---------------------------------------------------------------------------

class Plugin:
    name = "Twitch EPG"
    version = "0.1.0"
    description = (
        "Twitch live-TV lineup for Dispatcharr: auto-updated streamlink-ttvlol "
        "for ad-bypass low-latency playback + a continuously refreshed XMLTV guide."
    )
    author = "Dispatcharr Twitch EPG"
    help_url = "https://github.com/eliasbruno124-dev/Dispatcharr-Twitch-EPG"

    # Mirrored from plugin.json so the plugin still loads if plugin.json is
    # missing from a manual install (legacy mode).
    try:
        with open(os.path.join(os.path.dirname(__file__), "plugin.json"), "r", encoding="utf-8") as _mf:
            _manifest = json.loads(_mf.read())
    except Exception:
        _manifest = {}

    fields: list[dict] = _manifest.get("fields", [])
    actions: list[dict] = _manifest.get("actions", [])

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
            _disable_schedule()
        except Exception:
            logger.exception("Failed to clean up periodic tasks during stop()")
