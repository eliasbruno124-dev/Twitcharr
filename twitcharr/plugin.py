"""Twitcharr — Twitch live-TV plugin for Dispatcharr.

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
GITHUB_REPO = "eliasbruno124-dev/Dispatcharr-Twitch-EPG"
GITHUB_REPO_URL = f"https://github.com/{GITHUB_REPO}"
DONATE_URL = "https://paypal.me/eliasbruno124"

# Bundled offline tile. Full-bleed deep-violet background with a pixel-style
# game controller (D-pad + 4 face buttons) on a Twitch-purple body and a bold
# OFFLINE wordmark — the twitch2tuner-flavored "no stream" placeholder. Kept
# under the 500-char limit Dispatcharr enforces on EPG icon URLs.
_BUILTIN_OFFLINE_ICON_DATA_URL = (
    "data:image/svg+xml,"
    "%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 640 360'%3E"
    "%3Crect width='640' height='360' fill='%231a0d33'/%3E"
    "%3Crect x='195' y='125' width='250' height='90' rx='22' fill='%239146ff'/%3E"
    "%3Cg fill='%23fff'%3E"
    "%3Cpath d='M230 165h30v10h-30M240 155h10v30h-10'/%3E"
    "%3Cpath d='M388 162h12v12h-12M408 162h12v12h-12M388 180h12v12h-12M408 180h12v12h-12'/%3E"
    "%3Ctext x='320' y='285' font-size='46' font-weight='900' text-anchor='middle'%3EOFFLINE%3C/text%3E"
    "%3C/g%3E%3C/svg%3E"
)
DEFAULT_OFFLINE_ICON_URL = _BUILTIN_OFFLINE_ICON_DATA_URL


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
    "use_profile_pic_when_just_chatting": True,
    "epg_refresh_interval_minutes": 2,
    "ttvlol_update_time": "04:30",
    "ttvlol_proxy_servers": DEFAULT_TTVLOL_PROXY_SERVERS,
    "stream_quality": "adaptive",
    "connection_bandwidth_mbps": 0,
    "bandwidth_safety_margin_pct": 50,
    "enable_low_latency": True,
    "media_server_url": "",
    "media_server_api_key": "",
    "keep_placeholder": True,
    "auto_check_updates": True,
    "auto_apply_updates": True,
    "use_live_thumbnails": False,
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


def _text_setting(
    settings: dict,
    key: str,
    default: str = "",
    *,
    fallback_on_empty: bool = False,
) -> str:
    if key not in settings or settings.get(key) is None:
        return default
    value = str(settings.get(key)).strip()
    if fallback_on_empty and not value:
        return default
    return value


def _bool_setting(settings: dict, key: str, default: bool = False) -> bool:
    value = settings.get(key, default)
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "y", "on", "enabled"}:
            return True
        if normalized in {"0", "false", "no", "n", "off", "disabled", ""}:
            return False
    return bool(value)


def _int_setting(
    settings: dict,
    key: str,
    default: int,
    *,
    min_value: int | None = None,
    max_value: int | None = None,
) -> int:
    try:
        value = int(float(settings.get(key, default)))
    except (TypeError, ValueError):
        value = default
    if min_value is not None:
        value = max(min_value, value)
    if max_value is not None:
        value = min(max_value, value)
    return value


def _float_setting(settings: dict, key: str, default: float) -> float:
    try:
        return float(settings.get(key, default))
    except (TypeError, ValueError):
        return default


def _proxy_servers(settings: dict) -> str:
    # Missing value means "use defaults"; an explicitly empty field means
    # "disable proxy", matching the settings description.
    return _text_setting(settings, "ttvlol_proxy_servers", DEFAULT_TTVLOL_PROXY_SERVERS)


def _offline_icon_url(_settings: dict) -> str:
    """Always return the bundled offline tile.

    The user-facing setting that used to override this was removed in v1.2.4
    so every install ships the same, designed offline card. The argument is
    kept to preserve the existing call signatures across the codebase.
    """
    return DEFAULT_OFFLINE_ICON_URL


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
    top_count = _int_setting(settings, "discovery_top_count", 0, min_value=0, max_value=100)
    languages_raw = (settings.get("discovery_top_languages") or "").strip()
    if top_count and languages_raw:
        for code in (c.strip().lower() for c in languages_raw.replace(";", ",").split(",")):
            if code and code.replace("-", "").isalpha():
                items.append({"type": "top", "languages": [code], "limit": top_count})

    trending_count = _int_setting(settings, "discovery_trending_count", 0, min_value=0, max_value=100)
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
    quality = _text_setting(settings, "stream_quality", "adaptive", fallback_on_empty=True)
    if quality != "adaptive":
        return quality

    from . import bandwidth

    margin = _int_setting(settings, "bandwidth_safety_margin_pct", 50, min_value=0, max_value=200)
    mbps = _float_setting(settings, "connection_bandwidth_mbps", 0)
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
    # Cache-bust the optional live preview URL once per minute so Dispatcharr /
    # Emby actually re-fetches it instead of serving stale CDN cache.
    cache_bust = int(time.time() // 60)
    entries = epg.build_entries(
        client,
        logins,
        use_profile_pic_when_just_chatting=_bool_setting(settings, "use_profile_pic_when_just_chatting", True),
        include_offline=_bool_setting(settings, "include_offline", True),
        offline_icon_url=_offline_icon_url(settings),
        use_live_thumbnails=_bool_setting(settings, "use_live_thumbnails", False),
        cache_bust=cache_bust,
    )
    return client, logins, entries


def _write_epg(settings: dict, entries: list[dict]) -> dict:
    from . import epg

    data_dir = _data_dir(settings)
    channels, programmes = epg.write_xmltv(entries, epg.xmltv_path(data_dir))
    db_result = epg.upsert_db(entries, data_dir)
    return {
        "status": "ok",
        "message": f"Wrote guide for {channels} channels and {programmes} programmes.",
        "channels": channels,
        "programmes": programmes,
        "xmltv_path": epg.xmltv_path(data_dir),
        **db_result,
    }


def _lineup_epg_entries(settings: dict, entries: list[dict]) -> list[dict]:
    """Return the exact guide rows that should exist for the current lineup."""
    has_live = any(e.get("live") for e in entries)
    if not has_live and _bool_setting(settings, "keep_placeholder", True):
        from . import streamlink_setup

        return [streamlink_setup._placeholder_entry(
            offline_icon_url=_offline_icon_url(settings),
        )]
    return list(entries)


def _write_lineup_guide(settings: dict, entries: list[dict]) -> dict:
    return _write_epg(settings, _lineup_epg_entries(settings, entries))


def _sync_channels_from_entries(settings: dict, entries: list[dict]) -> dict:
    from . import streamlink_setup

    return streamlink_setup.sync_channels(
        entries,
        data_dir=_data_dir(settings),
        group_name=_text_setting(
            settings,
            "channel_group_name",
            DEFAULT_SETTINGS["channel_group_name"],
            fallback_on_empty=True,
        ),
        starting_channel_number=_int_setting(
            settings,
            "starting_channel_number",
            DEFAULT_SETTINGS["starting_channel_number"],
            min_value=1,
        ),
        proxy_servers=_proxy_servers(settings),
        quality=_resolve_stream_quality(settings),
        low_latency=_bool_setting(settings, "enable_low_latency", True),
        fast_startup=_bool_setting(settings, "fast_startup", True),
        keep_placeholder=_bool_setting(settings, "keep_placeholder", True),
        offline_icon_url=_offline_icon_url(settings),
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
        "message": (
            f"streamlink-ttvlol updated ({result.bytes_written} bytes)."
            if result.updated
            else f"streamlink-ttvlol checked: {result.skipped_reason or 'already current'}."
        ),
        "updated": result.updated,
        "release_tag": result.release_tag,
        "path": result.target_path,
        "bytes": result.bytes_written,
        "skipped_reason": result.skipped_reason,
    }


def _run_check_ttvlol(settings: dict) -> dict:
    """Quick freshness check for streamlink-ttvlol.

    Mirrors the plugin self-updater's check action: hits GitHub for the
    latest release tag, compares it to the locally cached one, and reports
    whether the user should force a re-download. Never writes files itself.
    """
    from . import ttvlol

    data_dir = _data_dir(settings)
    info = ttvlol.info(data_dir)
    installed = bool(info.get("installed"))
    current_tag = (info.get("release_tag") or "").strip()

    latest_tag = ""
    fetch_error = ""
    try:
        import requests

        resp = requests.get(
            ttvlol.RELEASES_API,
            timeout=10,
            headers={"Accept": "application/vnd.github+json", "User-Agent": "Twitcharr"},
        )
        if resp.status_code == 200:
            latest_tag = (resp.json().get("tag_name") or "").strip()
        else:
            fetch_error = f"GitHub returned HTTP {resp.status_code}"
    except Exception as exc:
        fetch_error = str(exc)

    if not installed:
        message = "streamlink-ttvlol is not installed yet — press 'Force-update ttv.lol' to download it."
        update_available = True
    elif fetch_error:
        message = f"Could not reach GitHub to check ttv.lol ({fetch_error}). Local version: {current_tag or 'unknown'}."
        update_available = False
    elif not latest_tag:
        message = f"GitHub did not return a release tag. Local version: {current_tag or 'unknown'}."
        update_available = False
    elif current_tag and current_tag == latest_tag:
        message = f"streamlink-ttvlol is up to date (tag {current_tag})."
        update_available = False
    else:
        message = (
            f"A newer streamlink-ttvlol release is available "
            f"(local: {current_tag or 'unknown'} → latest: {latest_tag}). "
            f"Press 'Force-update ttv.lol' to apply it."
        )
        update_available = True

    return {
        "status": "ok",
        "message": message,
        "installed": installed,
        "current_release_tag": current_tag,
        "latest_release_tag": latest_tag,
        "update_available": update_available,
        "last_check": info.get("last_check"),
        "last_update": info.get("last_update"),
        "path": info.get("path"),
        "fetch_error": fetch_error,
    }


def _run_refresh_epg(settings: dict, *, prebuilt=None) -> dict:
    """Refresh EPG, then trigger the configured Emby/Jellyfin server."""
    from . import ttvlol

    if prebuilt is None:
        client, logins, entries = _gather_entries(settings)
    else:
        client, logins, entries = prebuilt

    if not logins:
        return {"status": "error", "message": "No Twitch channel names configured"}
    if not entries and not getattr(client, "_users", {}):
        return {"status": "error", "message": "No matching Twitch channels found for the configured input"}

    write_result = _write_lineup_guide(settings, entries)

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
        **write_result,
        "message": (
            f"Guide refreshed for {write_result.get('channels', 0)} channels; "
            f"media server refresh: {refresh_status.get('status')}."
        ),
        "channels_resolved": len(logins),
        "channels_written": write_result.get("channels", 0),
        "programmes_written": write_result.get("programmes", 0),
        "media_server_status": refresh_status.get("status"),
        "media_server_message": refresh_status.get("message", ""),
        "discord_status": discord_status.get("status"),
        "discord_posted": discord_status.get("posted", 0),
        "media_server_refresh": refresh_status,
        "discord_notifications": discord_status,
    }


def _run_sync_channels(settings: dict, *, prebuilt=None) -> dict:
    if prebuilt is None:
        client, logins, entries = _gather_entries(settings)
    else:
        client, logins, entries = prebuilt
    if not logins:
        return {"status": "error", "message": "No Twitch channel names configured"}
    if not entries and not getattr(client, "_users", {}):
        return {"status": "error", "message": "No matching Twitch channels found for the configured input"}
    guide_result = _write_lineup_guide(settings, entries)
    result = _sync_channels_from_entries(settings, entries)
    if not entries:
        result["message"] = (
            "No configured Twitch channels are live right now. Managed offline channels were pruned."
        )
    return {
        "status": "ok",
        "channels_synced": len(result.get("channel_names") or []),
        "guide": guide_result,
        **result,
    }


def _run_setup(settings: dict) -> dict:
    """Idempotent one-click setup. Order matters:
        1. ttv.lol so playback works
        2. Channels / Streams so the lineup exists
        3. EPG rows so channels get guide data
        4. Media-server refresh after both channel and guide writes
    """
    from . import streamlink_setup, ttvlol

    data_dir = _data_dir(settings)
    os.makedirs(data_dir, exist_ok=True)

    ttv_result = ttvlol.update_ttvlol(data_dir, force=False)
    profile = streamlink_setup.get_or_create_stream_profile(
        data_dir=data_dir,
        proxy_servers=_proxy_servers(settings),
        quality=_resolve_stream_quality(settings),
        low_latency=_bool_setting(settings, "enable_low_latency", True),
        fast_startup=_bool_setting(settings, "fast_startup", True),
    )

    from . import epg

    source = epg.get_or_create_epg_source(data_dir)

    result: dict[str, Any] = {
        "status": "ok",
        "message": "Base setup complete. Scheduler is enabled.",
        "data_dir": data_dir,
        "ttvlol_release_tag": ttv_result.release_tag,
        "ttvlol_path": ttv_result.target_path,
        "stream_profile_id": profile.id,
        "epg_source_id": source.id,
        "schedule": _ensure_schedule_running(),
    }

    if _settings_have_twitch_inputs(settings):
        prebuilt = _gather_entries(settings)
        client, logins, entries = prebuilt
        if not logins:
            result["next"] = (
                "No Twitch channels could be resolved from the configured input. Check the "
                "channel names / discovery tokens in the channels field."
            )
            return result
        result["sync"] = _run_sync_channels(settings, prebuilt=prebuilt)
        result["epg"] = result["sync"].get("guide", {})
        result["media_server_refresh"] = _trigger_media_server(settings)
        result["discord_notifications"] = _trigger_discord_go_live(settings, entries)
        result["message"] = "Setup complete: channels, guide, scheduler and integrations were refreshed."
        result["next"] = "Done. Channels, guide and the auto-updater are active."
    else:
        result["next"] = (
            "Base setup is done and the daily ttv.lol updater is active. Add Twitch channel names "
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
            sync_result = _run_sync_channels(settings, prebuilt=prebuilt)
            out["steps"]["sync_channels"] = sync_result
            out["steps"]["refresh_epg"] = sync_result.get("guide", {})
        except Exception as e:
            logger.exception("sync_channels failed")
            out["steps"]["sync_channels"] = {"status": "error", "message": str(e)}

        try:
            out["steps"]["media_server"] = _trigger_media_server(settings)
            _client, _logins, entries = prebuilt
            out["steps"]["discord"] = _trigger_discord_go_live(settings, entries)
        except Exception as e:
            logger.exception("post-sync integrations failed")
            out["steps"]["integrations"] = {"status": "error", "message": str(e)}

    has_error = any(s.get("status") == "error" for s in out["steps"].values())
    out["status"] = "partial" if has_error else "ok"
    out["message"] = "Full refresh finished." if not has_error else "Full refresh finished with errors."
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
    webhook = _text_setting(settings, "discord_webhook_url")
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
    url = _text_setting(settings, "media_server_url")
    key = _text_setting(settings, "media_server_api_key")
    if not url or not key:
        return {"status": "skipped", "message": "No Emby/Jellyfin URL or API key configured"}
    try:
        from . import media_server

        return media_server.trigger_guide_refresh(base_url=url, api_key=key)
    except Exception as exc:
        logger.exception("Media-server guide refresh failed")
        return {"status": "error", "message": str(exc)}


def _run_refresh_media_server(settings: dict) -> dict:
    trigger = _trigger_media_server(settings)
    return {
        "status": trigger.get("status", "ok"),
        "message": trigger.get("message", "Media-server refresh checked."),
        "media_server_status": trigger.get("status"),
        "task_id": trigger.get("task_id"),
        "task_name": trigger.get("task_name"),
        "trigger": trigger,
    }


def _validate_settings(settings: dict) -> dict:
    """Cheap local settings validation for the action results.

    It intentionally avoids network calls; `test_connection` and `status` add
    the live checks separately.
    """
    errors: list[str] = []
    warnings: list[str] = []

    channels = _text_setting(settings, "channels")
    for line in channels.replace("\r", "\n").split("\n"):
        token = line.strip()
        if not token:
            continue
        low = token.lower()
        if "oauth" in low or low.startswith(("client-id", "client_id", "client secret", "client-secret")):
            errors.append(
                "Remove Twitch OAuth / Client-ID text from 'Twitch channels / discovery'. "
                "Twitcharr only needs channel names or discovery tokens."
            )
            break

    try:
        raw_start = int(float(settings.get("starting_channel_number", 9000)))
    except (TypeError, ValueError):
        raw_start = 9000
    if raw_start < 1:
        errors.append("Starting channel number must be 1 or higher.")

    quality = _text_setting(settings, "stream_quality", "adaptive", fallback_on_empty=True)
    valid_qualities = {
        "adaptive", "best", "1080p60", "1080p", "720p60", "720p",
        "480p", "360p", "160p", "worst",
    }
    if quality not in valid_qualities:
        warnings.append(f"Unknown stream quality '{quality}' will be passed to Streamlink as-is.")

    bandwidth_mbps = _float_setting(settings, "connection_bandwidth_mbps", 0)
    if bandwidth_mbps < 0:
        errors.append("Connection bandwidth must be 0 or higher.")

    margin = _int_setting(settings, "bandwidth_safety_margin_pct", 50, min_value=0, max_value=200)
    if margin != _int_setting(settings, "bandwidth_safety_margin_pct", 50):
        warnings.append("Bandwidth safety margin is clamped to the supported 0-200% range.")

    time_raw = _text_setting(settings, "ttvlol_update_time", "04:30", fallback_on_empty=True)
    try:
        hour, minute = [int(part) for part in time_raw.split(":", 1)]
        if hour < 0 or hour > 23 or minute < 0 or minute > 59:
            raise ValueError
    except Exception:
        warnings.append("Daily ttv.lol update time should be HH:MM, for example 04:30.")

    proxy_servers = _proxy_servers(settings)
    for url in [u.strip() for u in proxy_servers.split(",") if u.strip()]:
        if not url.lower().startswith(("http://", "https://")):
            warnings.append(f"Proxy URL '{url}' should start with http:// or https://.")

    media_url = _text_setting(settings, "media_server_url")
    media_key = _text_setting(settings, "media_server_api_key")
    if bool(media_url) != bool(media_key):
        warnings.append("Set both Emby/Jellyfin URL and API key, or leave both empty.")

    languages_raw = _text_setting(settings, "discovery_top_languages")
    bad_languages = [
        code.strip()
        for code in languages_raw.replace(";", ",").split(",")
        if code.strip() and not code.strip().replace("-", "").isalpha()
    ]
    if bad_languages:
        warnings.append(f"Invalid language code(s): {', '.join(bad_languages)}.")

    return {
        "status": "error" if errors else ("warning" if warnings else "ok"),
        "errors": errors,
        "warnings": warnings,
    }


# ---------------------------------------------------------------------------
# Diagnostics
# ---------------------------------------------------------------------------


def _scheduler_is_running() -> bool:
    return bool(_scheduler_thread and _scheduler_thread.is_alive())


def _run_test_proxies(settings: dict) -> dict:
    """Probe every ttv.lol proxy URL and report HTTP code + latency."""
    from . import diagnostics

    csv = _proxy_servers(settings)
    results = diagnostics.test_proxies(csv, timeout=5.0)
    alive = sum(1 for r in results if r.get("status") == "ok")
    total = len(results)
    return {
        "status": "ok" if alive else ("error" if total else "skipped"),
        "message": f"{alive}/{total} configured ttv.lol proxies reachable",
        "summary": f"{alive}/{total} proxies reachable",
        "reachable": alive,
        "total": total,
        "proxies": results,
        "next": (
            "Reorder or remove dead proxies in 'ttv.lol proxy servers' to speed up channel switching."
            if alive < total
            else "All configured proxies are reachable."
        ),
    }


def _run_test_discord(settings: dict) -> dict:
    """Send a one-off test embed to the Discord webhook."""
    webhook = _text_setting(settings, "discord_webhook_url")
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
    result = notifications.post_go_live(webhook, [sample])
    return {
        "message": (
            f"Discord test sent ({result.get('posted', 0)} embed)."
            if result.get("status") == "ok"
            else "Discord test failed."
        ),
        **result,
    }


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

    measured_mbps = round(result.mbps, 2)
    saved_setting = False
    save_error = ""
    effective_settings = dict(settings)
    effective_settings["connection_bandwidth_mbps"] = measured_mbps
    try:
        _save_setting("connection_bandwidth_mbps", measured_mbps)
        saved_setting = True
    except Exception as exc:
        save_error = str(exc)
        logger.exception("Could not persist measured connection bandwidth")

    profile_update: dict[str, Any] = {"stream_profile_updated": False}
    try:
        from . import streamlink_setup

        profile = streamlink_setup.get_or_create_stream_profile(
            data_dir=_data_dir(effective_settings),
            proxy_servers=_proxy_servers(effective_settings),
            quality=_resolve_stream_quality(effective_settings),
            low_latency=_bool_setting(effective_settings, "enable_low_latency", True),
            fast_startup=_bool_setting(effective_settings, "fast_startup", True),
        )
        profile_update = {"stream_profile_updated": True, "stream_profile_id": profile.id}
    except Exception as exc:
        logger.exception("Could not update StreamProfile after bandwidth probe")
        profile_update = {"stream_profile_updated": False, "stream_profile_error": str(exc)}

    margin = _int_setting(settings, "bandwidth_safety_margin_pct", 50, min_value=0, max_value=200)
    description = bandwidth.describe_chain_for(result.mbps, safety_margin_pct=margin)

    return {
        "status": "ok",
        "message": (
            f"Measured {measured_mbps} Mbps. Preferred quality: "
            f"{description.get('preferred_quality')}; chain: {description.get('fallback_chain')}"
        ),
        **result.as_dict(),
        **description,
        "connection_bandwidth_mbps": measured_mbps,
        "saved_setting": saved_setting,
        "save_error": save_error,
        **profile_update,
        "active_now": _text_setting(settings, "stream_quality", "adaptive", fallback_on_empty=True) == "adaptive",
        "next": (
            "Measurement saved to 'Connection bandwidth (Mbps)' and the StreamProfile was updated."
            if saved_setting and profile_update.get("stream_profile_updated")
            else "Measurement completed. If settings did not update visually, reload the plugin settings and run Sync channels once."
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
    return _int_setting(settings, "epg_refresh_interval_minutes", 2, min_value=1)


def _ttvlol_update_minute(settings: dict) -> int:
    raw = _text_setting(settings, "ttvlol_update_time")
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
    if not _bool_setting(settings, "auto_check_updates", True):
        return False
    last = float(state.get("last_update_check") or 0)
    return now - last >= UPDATE_CHECK_INTERVAL_SECONDS


def _auto_apply_updates_enabled(settings: dict) -> bool:
    return _bool_setting(settings, "auto_apply_updates", True)


def _settings_have_twitch_inputs(settings: dict) -> bool:
    if _text_setting(settings, "channels"):
        return True
    if _int_setting(settings, "discovery_trending_count", 0, min_value=0) > 0:
        return True
    return bool(
        _text_setting(settings, "discovery_top_languages")
        and _int_setting(settings, "discovery_top_count", 0, min_value=0) > 0
    )


def _run_scheduled_tick() -> None:
    settings = _load_settings()

    now = time.time()
    state = _load_schedule_state(settings)

    if _ttvlol_due(settings, state, now):
        lock = _job_lock(settings, "ttvlol_update", ttl_seconds=30 * 60)
        if lock:
            try:
                result = _run_update_ttvlol(settings, force=True)
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
                    state = _load_schedule_state(settings)
                    state.update({
                        "last_epg_refresh": int(time.time()),
                        "last_epg_status": "skipped",
                        "last_epg_skip_reason": "no twitch inputs configured",
                    })
                    _save_schedule_state(settings, state)
                else:
                    prebuilt = _gather_entries(settings)
                    sync_result = _run_sync_channels(settings, prebuilt=prebuilt)
                    _client, _logins, entries = prebuilt
                    media_result = _trigger_media_server(settings)
                    discord_result = _trigger_discord_go_live(settings, entries)
                    state = _load_schedule_state(settings)
                    state.update({
                        "last_epg_refresh": int(time.time()),
                        "last_epg_status": "ok",
                        "last_sync_result": sync_result,
                        "last_epg_result": sync_result.get("guide", {}),
                        "last_media_server_refresh": media_result,
                        "last_discord_result": discord_result,
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
            apply_result = None
            latest_version = check.get("latest_version")
            previous_auto = state.get("auto_update_result") or {}
            already_applied = (
                previous_auto.get("applied")
                and latest_version
                and previous_auto.get("latest_version") == latest_version
            )
            if (
                check.get("status") == "ok"
                and check.get("update_available")
                and _auto_apply_updates_enabled(settings)
                and not already_applied
            ):
                apply_result = _run_apply_update(settings)
            state = _load_schedule_state(settings)
            state.update({
                "last_update_check": int(time.time()),
                "update_check": check,
            })
            if apply_result is not None:
                state["last_auto_update"] = int(time.time())
                state["auto_update_result"] = apply_result
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


def _ensure_schedule_running() -> dict:
    """Make sure the background scheduler is running and report its status."""
    merged = _load_settings()
    started = _start_scheduler()
    return {
        "status": "ok",
        "scheduler": "running",
        "started_now": started,
        "epg_refresh": f"every {_interval_minutes(merged)} minutes",
        "ttvlol_update": f"daily at {_ttvlol_update_time_label(merged)} server time",
        **_delete_legacy_celery_tasks(),
    }


# ---------------------------------------------------------------------------
# Plugin class
# ---------------------------------------------------------------------------

class Plugin:
    name = "Twitcharr"
    version = str(_MANIFEST.get("version") or "1.2.3")
    description = (
        "Twitch live-TV lineup for Dispatcharr. Add channel names by comma or line break, "
        "optionally use discovery tokens, and sync Channels plus XMLTV guide data "
        "without Twitch OAuth credentials or account login."
    )
    author = "eliasbruno124"
    help_url = GITHUB_REPO_URL

    fields: list[dict] = _MANIFEST.get("fields", [])
    actions: list[dict] = _MANIFEST.get("actions", [])

    def __init__(self):
        try:
            _start_scheduler()
        except Exception:
            logger.exception("Could not start persisted Twitcharr scheduler")

    def run(self, action: str, params: dict, context: dict):
        settings = _merge_defaults((context or {}).get("settings") or {})
        plugin_logger = (context or {}).get("logger") or logger
        params = params or {}

        try:
            if action == "setup":
                return _run_setup(settings)
            if action == "sync_channels":
                return _run_sync_channels(settings)
            if action == "refresh_epg":
                return _run_refresh_epg(settings)
            if action == "check_ttvlol":
                return _run_check_ttvlol(settings)
            if action == "update_ttvlol":
                return _run_update_ttvlol(settings, force=bool(params.get("force", True)))
            if action == "run_all":
                return _run_all(settings)
            if action == "refresh_media_server":
                return _run_refresh_media_server(settings)
            if action == "measure_bandwidth":
                return _run_measure_bandwidth(settings)
            if action == "test_proxies":
                return _run_test_proxies(settings)
            if action == "test_discord":
                return _run_test_discord(settings)
            if action == "check_updates":
                return _run_check_updates(settings)
            if action == "apply_update":
                return _run_apply_update(settings)
            if action == "uninstall":
                from . import streamlink_setup

                _stop_scheduler()
                return {"status": "ok", **streamlink_setup.uninstall_managed_objects()}
            return {"status": "error", "message": f"Unknown action: {action}"}
        except Exception as e:
            plugin_logger.exception("Action %s failed", action)
            return {"status": "error", "message": str(e)}

    def stop(self, context: dict):
        try:
            _stop_scheduler()
        except Exception:
            logger.exception("Failed to stop Twitcharr scheduler")
