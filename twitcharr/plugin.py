"""Twitcharr — Twitch live-TV plugin for Dispatcharr.

Combines:
  * an auto-updated streamlink-ttvlol twitch.py for proxy-aware low-latency playback
  * a continuously refreshed XMLTV guide (twitch2tuner-style)
  * direct Channel/Stream/EPGData rows in Dispatcharr — no manual M3U/EPG setup
  * Twitch channel discovery (game/top/search) directly inside the lineup field
  * Emby/Jellyfin guide refresh on every EPG cycle
"""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Manifest / defaults
# ---------------------------------------------------------------------------

PLUGIN_KEY = "twitcharr"
GITHUB_REPO = "eliasbruno124-dev/Twitcharr"
GITHUB_REPO_URL = f"https://github.com/{GITHUB_REPO}"
GITHUB_RAW_URL = f"https://raw.githubusercontent.com/{GITHUB_REPO}/main"
DONATE_URL = "https://paypal.me/eliasbruno124"
OFFLINE_ARTWORK_URL = f"{GITHUB_RAW_URL}/twitcharr/assets/offline.png"
EMBY_SAFE_QUALITY_CHAIN = (
    "1080p30,1080p,720p30,720p,480p30,480p,360p30,360p,160p30,160p"
)


DEFAULT_TTVLOL_PROXY_SERVERS = (
    "https://eu.luminous.dev,"
    "https://eu2.luminous.dev,"
    "https://lb-eu.cdn-perfprod.com,"
    "https://lb-eu2.cdn-perfprod.com"
)
DEFAULT_SETTINGS: dict[str, Any] = {
    "channel_group_name": "Twitch",
    "channel_profiles": "",
    "channel_name_prefix": "",
    "channel_name_suffix": "",
    "channel_name_overrides": "",
    "starting_channel_number": 9000,
    "data_dir": "/app/data/plugins/twitcharr",
    "include_offline": True,
    "live_indicator_mode": "xmltv",
    "description_separator": r"\n",
    "channel_logo_mode": "profile",
    "epg_refresh_interval_minutes": 2,
    "ttvlol_proxy_servers": DEFAULT_TTVLOL_PROXY_SERVERS,
    "stream_quality": "adaptive",
    "connection_bandwidth_mbps": 0,
    "bandwidth_safety_margin_pct": 50,
    "enable_low_latency": True,
    "media_server_url": "",
    "media_server_api_key": "",
    "fast_startup": True,
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


def _raw_text_setting(settings: dict, key: str, default: str = "") -> str:
    """Return text without trimming meaningful prefix/suffix whitespace."""
    value = settings.get(key, default)
    return default if value is None else str(value)


def _channel_name_templates(settings: dict) -> dict[str, str]:
    """Parse `login = TTV | {name}` per-channel display templates."""
    raw = _raw_text_setting(settings, "channel_name_overrides")
    templates: dict[str, str] = {}
    for line in raw.replace("\r", "\n").split("\n"):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        separator = "=>" if "=>" in line else "="
        if separator not in line:
            continue
        login, template = line.split(separator, 1)
        login = login.strip().rstrip("/").rsplit("/", 1)[-1].lower()
        template = template.strip()
        if login.replace("_", "").isalnum() and "{name}" in template:
            templates[login] = template
    return templates


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


def _offline_icon_url(settings: dict, *, cache_bust: int | None = None) -> str:
    version = str(_MANIFEST.get("version") or "").strip() or int(time.time())
    return f"{OFFLINE_ARTWORK_URL}?v={version}"


def _offline_program_icon_url(settings: dict, *, cache_bust: int | None = None) -> str:
    version = str(_MANIFEST.get("version") or "").strip() or int(time.time())
    return f"{OFFLINE_ARTWORK_URL}?v={version}"


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


def _resolve_logins(settings: dict, client) -> tuple[list[str], dict[str, list[str]]]:
    """Parse the channels textarea and expand discovery tokens to logins.

    Returns (logins, profiles_by_login) where the mapping carries channel
    profiles requested via `name(profile1, profile2)` suffixes.
    """
    from . import twitch_api as tw

    items = tw.parse_login_list(settings.get("channels") or "")
    if not items:
        return [], {}
    return tw.resolve_logins(client, items)


def _profile_names(settings: dict) -> list[str]:
    """Global channel-profile names from settings (comma-separated, '*' = all)."""
    raw = _text_setting(settings, "channel_profiles")
    return [p.strip() for p in raw.split(",") if p.strip()]


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
        except Exception as exc:
            logger.debug("Could not load the last measured bandwidth: %s", exc)
    return bandwidth.quality_chain_for_bandwidth(mbps, safety_margin_pct=margin)


# ---------------------------------------------------------------------------
# Internal action helpers (entry-aware, so we never call Twitch twice per cycle)
# ---------------------------------------------------------------------------


def _gather_entries(settings: dict, *, client=None):
    from . import epg

    client = client or _twitch_client(settings)
    logins, profiles_by_login = _resolve_logins(settings, client)
    if not logins:
        return client, [], []
    # Cache-bust image URLs per refresh cycle so Emby/Jellyfin do not keep
    # showing stale channel/category/offline artwork after guide refreshes.
    cache_bust = int(time.time())
    entries = epg.build_entries(
        client,
        logins,
        include_offline=_bool_setting(settings, "include_offline", True),
        offline_icon_url=_offline_icon_url(settings, cache_bust=cache_bust),
        offline_program_icon_url=_offline_program_icon_url(settings, cache_bust=cache_bust),
        use_live_thumbnails=False,
        cache_bust=cache_bust,
        profiles_by_login=profiles_by_login,
        channel_name_prefix=_raw_text_setting(settings, "channel_name_prefix"),
        channel_name_suffix=_raw_text_setting(settings, "channel_name_suffix"),
        channel_name_templates=_channel_name_templates(settings),
        live_indicator_mode=_text_setting(
            settings, "live_indicator_mode", DEFAULT_SETTINGS["live_indicator_mode"], fallback_on_empty=True
        ),
        description_separator=_raw_text_setting(
            settings, "description_separator", DEFAULT_SETTINGS["description_separator"]
        ),
        channel_logo_mode=_text_setting(
            settings, "channel_logo_mode", DEFAULT_SETTINGS["channel_logo_mode"], fallback_on_empty=True
        ),
    )
    return client, logins, entries


def _write_epg(settings: dict, entries: list[dict]) -> dict:
    from . import epg

    data_dir = _data_dir(settings)
    channels, programmes = epg.write_xmltv(entries, epg.xmltv_path(data_dir))
    db_result = epg.upsert_db(entries, data_dir)
    output_cache = epg.invalidate_dispatcharr_output_cache()
    return {
        "status": "ok",
        "message": f"Wrote guide for {channels} channels and {programmes} programmes.",
        "channels": channels,
        "programmes": programmes,
        "xmltv_path": epg.xmltv_path(data_dir),
        "output_cache": output_cache,
        **db_result,
    }


def _lineup_epg_entries(settings: dict, entries: list[dict]) -> list[dict]:
    """Return the exact guide rows that should exist for the current lineup."""
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
        offline_icon_url=_offline_icon_url(settings),
        offline_program_icon_url=_offline_program_icon_url(settings),
        profile_names=_profile_names(settings),
    )


# ---------------------------------------------------------------------------
# Action implementations
# ---------------------------------------------------------------------------

def _run_update_ttvlol(settings: dict, *, force: bool = False) -> dict:
    """Install or verify the newest stable ttv.lol release.

    `force=True` reinstalls the newest release. Every download must match the
    SHA-256 digest published in GitHub's release metadata and valid Python.
    """
    from . import ttvlol

    data_dir = _data_dir(settings)
    result = ttvlol.update_ttvlol(data_dir, force=force)
    return {
        "status": "ok",
        "message": (
            f"ttv.lol updated to {result.release_tag or 'latest'} ({result.bytes_written} bytes)."
            if result.updated
            else f"ttv.lol is on the newest verified release ({result.release_tag or 'latest'})."
        ),
        "updated": result.updated,
        "release_tag": result.release_tag,
        "path": result.target_path,
        "bytes": result.bytes_written,
        "sha256": result.sha256,
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
        return {"status": "error", "message": "No Twitch channels configured."}
    if not entries and not getattr(client, "_users", {}):
        return {"status": "error", "message": "No matching Twitch channels found."}

    write_result = _write_lineup_guide(settings, entries)

    # Opportunistic ttv.lol freshness check (cheap when not due).
    data_dir = _data_dir(settings)
    if ttvlol.needs_check(data_dir, max_age_hours=24):
        try:
            ttvlol.update_ttvlol(data_dir, force=False)
        except Exception:
            logger.exception("Background ttv.lol check failed (non-fatal)")

    refresh_status = _trigger_media_server(settings)
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
        "media_server_refresh": refresh_status,
    }


def _run_sync_channels(settings: dict, *, prebuilt=None, warm_images: bool = True) -> dict:
    from . import epg

    if prebuilt is None:
        client, logins, entries = _gather_entries(settings)
    else:
        client, logins, entries = prebuilt
    if not logins:
        return {"status": "error", "message": "No Twitch channels configured."}
    if not entries and not getattr(client, "_users", {}):
        return {"status": "error", "message": "No matching Twitch channels found."}
    guide_result = _write_lineup_guide(settings, entries)
    result = _sync_channels_from_entries(settings, entries)
    initial_epg_link = epg.link_channels_to_epg(entries, _data_dir(settings))
    # Channel creation makes Dispatcharr's parse task wipe and re-parse the
    # programmes for that tvg_id; if it loses that race the channel sits
    # without guide data until the next cycle. Heal those immediately.
    initial_heal_result = epg.ensure_programs(entries, _data_dir(settings))
    changed_channels = (
        (result.get("channels_created") or 0) > 0
        or (result.get("channels_updated") or 0) > 0
    )
    first_media_server_refresh: dict[str, Any] | None = None
    media_server_refresh = _trigger_media_server(
        settings,
        warm_images=warm_images and not changed_channels,
    )

    # Dispatcharr may finish an asynchronous EPG parse after the channel was
    # created. That parse can replace the fresh EPGData row or clear its
    # programmes, leaving a newly added channel as "Not Assigned" even though
    # the guide was written correctly. Re-link and heal once more after the
    # blocking media-server refresh has given those parse tasks time to settle.
    final_epg_link = epg.link_channels_to_epg(entries, _data_dir(settings))
    final_heal_result = epg.ensure_programs(entries, _data_dir(settings))
    if changed_channels and media_server_refresh.get("status") in {"ok", "partial"}:
        # Channel saves can schedule a Dispatcharr programme re-parse that
        # finishes after the first Emby/Jellyfin refresh. Re-read the guide
        # only after the final link/heal pass so clients cannot retain the
        # previous title or description for the current programme.
        first_media_server_refresh = media_server_refresh
        media_server_refresh = _trigger_media_server(
            settings,
            ensure_tuner=False,
            warm_images=warm_images,
        )
    epg_link = {
        "checked_channels": max(
            initial_epg_link.get("checked_channels", 0),
            final_epg_link.get("checked_channels", 0),
        ),
        "linked_channels": (
            initial_epg_link.get("linked_channels", 0)
            + final_epg_link.get("linked_channels", 0)
        ),
        "initial_linked_channels": initial_epg_link.get("linked_channels", 0),
        "final_linked_channels": final_epg_link.get("linked_channels", 0),
    }
    programs_healed = (
        initial_heal_result.get("programs_healed", 0)
        + final_heal_result.get("programs_healed", 0)
    )
    if not entries:
        result["message"] = "Nothing live right now. Offline channels pruned."
    response = {
        "status": "ok",
        "channels_synced": len(result.get("channel_names") or []),
        "guide": guide_result,
        "epg_link": epg_link,
        "programs_healed": programs_healed,
        "media_server_refresh": media_server_refresh,
        "media_server_status": media_server_refresh.get("status"),
        **result,
    }
    if first_media_server_refresh:
        response["media_server_first_refresh"] = first_media_server_refresh
    return response


def _run_setup(settings: dict) -> dict:
    """Idempotent one-click setup. Order matters:
        1. ttv.lol so playback works
        2. EPG rows so channels get guide data immediately
        3. Channels / Streams so the lineup exists
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
    output_profile = streamlink_setup.get_or_create_media_server_output_profile()

    from . import epg

    source = epg.get_or_create_epg_source(data_dir)

    result: dict[str, Any] = {
        "status": "ok",
        "message": "Setup complete. Scheduler running.",
        "data_dir": data_dir,
        "ttvlol_release_tag": ttv_result.release_tag,
        "ttvlol_path": ttv_result.target_path,
        "stream_profile_id": profile.id,
        **streamlink_setup.media_server_integration_info(output_profile.id),
        "epg_source_id": source.id,
        "schedule": _ensure_schedule_running(),
    }

    if _settings_have_twitch_inputs(settings):
        prebuilt = _gather_entries(settings)
        client, logins, entries = prebuilt
        if not logins:
            result["next"] = "Could not resolve any channels. Check the channel names or discovery tokens."
            return result
        result["sync"] = _run_sync_channels(settings, prebuilt=prebuilt)
        result["epg"] = result["sync"].get("guide", {})
        result["media_server_refresh"] = result["sync"].get("media_server_refresh", {})
        result["message"] = "Setup complete. Channels, guide and media server refreshed."
        result["next"] = "Channels, guide and auto-updater are active."
    else:
        result["next"] = "Add Twitch channels (or a discovery token), then sync again."
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
            out["steps"]["media_server"] = sync_result.get("media_server_refresh", {})
        except Exception as e:
            logger.exception("sync_channels failed")
            out["steps"]["sync_channels"] = {"status": "error", "message": str(e)}

    has_error = any(s.get("status") == "error" for s in out["steps"].values())
    out["status"] = "partial" if has_error else "ok"
    out["message"] = "Full refresh done." if not has_error else "Full refresh finished with errors."
    return out


# ---------------------------------------------------------------------------
# Media server (Emby / Jellyfin) refresh
# ---------------------------------------------------------------------------


def _trigger_media_server(
    settings: dict,
    *,
    ensure_tuner: bool = True,
    warm_images: bool = True,
) -> dict:
    url = _text_setting(settings, "media_server_url")
    key = _text_setting(settings, "media_server_api_key")
    if not url or not key:
        return {"status": "skipped", "message": "No Emby/Jellyfin URL or API key configured"}
    try:
        from . import media_server, streamlink_setup

        safe_m3u_path = ""
        if ensure_tuner:
            output_profile = streamlink_setup.get_or_create_media_server_output_profile()
            safe_m3u_path = streamlink_setup.media_server_m3u_path(output_profile.id)
        return media_server.trigger_guide_refresh(
            base_url=url,
            api_key=key,
            safe_m3u_path=safe_m3u_path,
            ensure_tuner=ensure_tuner,
            warm_images=warm_images,
        )
    except Exception as exc:
        logger.exception("Media-server guide refresh failed")
        return {"status": "error", "message": str(exc)}


def _run_refresh_media_server(settings: dict) -> dict:
    trigger = _trigger_media_server(settings)
    return {
        "status": trigger.get("status", "ok"),
        "message": trigger.get("message", "Media server checked."),
        "media_server_status": trigger.get("status"),
        "task_id": trigger.get("task_id"),
        "task_name": trigger.get("task_name"),
        "trigger": trigger,
    }


def _validate_settings(settings: dict) -> dict:
    """Cheap local settings validation for the action results.

    It intentionally avoids network calls so action handlers can reuse it
    without slowing down the normal sync path.
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
        "adaptive", "best",
        "1080p60", "1080p30", "1080p",
        "720p60", "720p30", "720p",
        "480p30", "480p",
        "360p30", "360p",
        "160p30", "160p",
        "worst",
        EMBY_SAFE_QUALITY_CHAIN,
    }
    if quality not in valid_qualities:
        warnings.append(f"Unknown stream quality '{quality}' will be passed to Streamlink as-is.")

    bandwidth_mbps = _float_setting(settings, "connection_bandwidth_mbps", 0)
    if bandwidth_mbps < 0:
        errors.append("Connection bandwidth must be 0 or higher.")

    margin = _int_setting(settings, "bandwidth_safety_margin_pct", 50, min_value=0, max_value=200)
    if margin != _int_setting(settings, "bandwidth_safety_margin_pct", 50):
        warnings.append("Bandwidth safety margin is clamped to the supported 0-200% range.")

    proxy_servers = _proxy_servers(settings)
    for url in [u.strip() for u in proxy_servers.split(",") if u.strip()]:
        if not url.lower().startswith(("http://", "https://")):
            warnings.append(f"Proxy URL '{url}' should start with http:// or https://.")

    media_url = _text_setting(settings, "media_server_url")
    media_key = _text_setting(settings, "media_server_api_key")
    if bool(media_url) != bool(media_key):
        warnings.append("Set both Emby/Jellyfin URL and API key, or leave both empty.")

    indicator = _text_setting(settings, "live_indicator_mode", "xmltv", fallback_on_empty=True)
    if indicator not in {"xmltv", "emoji", "both", "none"}:
        errors.append("Live indicator must be one of: xmltv, emoji, both, none.")

    logo_mode = _text_setting(settings, "channel_logo_mode", "profile", fallback_on_empty=True)
    if logo_mode not in {"profile", "category"}:
        errors.append("Channel logo must be either profile or category.")

    for line_number, line in enumerate(
        _raw_text_setting(settings, "channel_name_overrides").replace("\r", "\n").split("\n"),
        start=1,
    ):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        separator = "=>" if "=>" in line else "="
        if separator not in line or "{name}" not in line.split(separator, 1)[1]:
            errors.append(
                f"Per-channel name template on line {line_number} must use "
                "'login = ... {name} ...'."
            )
            break

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
        "message": f"{alive}/{total} ttv.lol proxies reachable.",
        "summary": f"{alive}/{total} proxies reachable",
        "reachable": alive,
        "total": total,
        "proxies": results,
        "next": (
            "Remove or reorder dead proxies for faster channel switching."
            if alive < total
            else "All proxies reachable."
        ),
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
        output_profile = streamlink_setup.get_or_create_media_server_output_profile()
        profile_update = {
            "stream_profile_updated": True,
            "stream_profile_id": profile.id,
            **streamlink_setup.media_server_integration_info(output_profile.id),
        }
    except Exception as exc:
        logger.exception("Could not update StreamProfile after bandwidth probe")
        profile_update = {"stream_profile_updated": False, "stream_profile_error": str(exc)}

    margin = _int_setting(settings, "bandwidth_safety_margin_pct", 50, min_value=0, max_value=200)
    description = bandwidth.describe_chain_for(result.mbps, safety_margin_pct=margin)

    return {
        "status": "ok",
        "message": (
            f"Measured {measured_mbps} Mbps. Preferred quality: "
            f"{description.get('preferred_quality')} (chain: {description.get('fallback_chain')})."
        ),
        **result.as_dict(),
        **description,
        "connection_bandwidth_mbps": measured_mbps,
        "saved_setting": saved_setting,
        "save_error": save_error,
        **profile_update,
        "active_now": _text_setting(settings, "stream_quality", "adaptive", fallback_on_empty=True) == "adaptive",
        "next": (
            "Saved to 'Connection bandwidth' and StreamProfile updated."
            if saved_setting and profile_update.get("stream_profile_updated")
            else "Measured. Reload plugin settings and sync once if values look stale."
        ),
    }


def _run_uninstall(settings: dict) -> dict:
    from . import streamlink_setup

    _stop_scheduler()
    uninstall_result = streamlink_setup.uninstall_managed_objects()
    refresh = _trigger_media_server(settings, ensure_tuner=False, warm_images=False)
    return {
        "status": "ok",
        "message": (
            "Uninstall complete. Managed objects were removed; "
            f"media server refresh: {refresh.get('status', 'unknown')}."
        ),
        **uninstall_result,
        "media_server_refresh": refresh,
        "media_server_status": refresh.get("status"),
        "media_server_message": refresh.get("message", ""),
    }


# ---------------------------------------------------------------------------
# Scheduling
# ---------------------------------------------------------------------------

LEGACY_EPG_TASK_NAME = "twitcharr__refresh_epg"
LEGACY_TTVLOL_TASK_NAME = "twitcharr__update_ttvlol"
SCHEDULER_POLL_SECONDS = 30

_scheduler_lock = threading.RLock()
_scheduler_stop = threading.Event()
_scheduler_thread: threading.Thread | None = None


def _stop_superseded_schedulers() -> int:
    """Stop scheduler threads left behind by an older Twitcharr import.

    Dispatcharr can keep the previous unmanaged plugin module in ``sys.modules``
    while loading an uploaded replacement under a new private module name.  A
    disabled v1.3.1 instance would otherwise continue using the shared settings
    row and overwrite channels refreshed by v1.3.2.  The most recently
    initialised Twitcharr module becomes the sole scheduler owner in this
    process; actions remain available on every loaded instance.
    """
    current_module = sys.modules.get(__name__)
    stopped = 0
    for module_name, module in list(sys.modules.items()):
        if module is None or module is current_module:
            continue
        plugin_class = getattr(module, "Plugin", None)
        if getattr(plugin_class, "name", None) != "Twitcharr":
            continue
        stop_event = getattr(module, "_scheduler_stop", None)
        thread = getattr(module, "_scheduler_thread", None)
        if not callable(getattr(stop_event, "set", None)):
            continue
        is_alive = getattr(thread, "is_alive", None)
        if not callable(is_alive) or not is_alive():
            continue
        stop_event.set()
        if thread is not threading.current_thread():
            join = getattr(thread, "join", None)
            if callable(join):
                join(timeout=5)
        stopped += 1
        logger.info("Stopped superseded Twitcharr scheduler from %s", module_name)
    return stopped


def _is_web_server_process() -> bool:
    """True inside Dispatcharr's uWSGI web workers or the Daphne ASGI server.

    Dispatcharr's uWSGI workers run gevent with monkey-patching: a plugin
    background thread there becomes a greenlet on the very OS thread that
    serves every HTTP request, so one blocking call in the scheduler can
    freeze the whole web UI (see Dispatcharr's uwsgi.ini comments). The
    Celery worker processes also load plugins (via worker_ready) and use
    real threads, so background work runs there instead.
    """
    if "uwsgi" in sys.modules:
        return True
    try:
        import uwsgi  # type: ignore # noqa: F401  (only importable inside uWSGI)

        return True
    except ImportError:
        pass
    return any(
        server in (arg or "").lower()
        for arg in sys.argv[:2]
        for server in ("daphne", "gunicorn")
    )


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


# The newest stable ttv.lol release is checked and verified at server-local
# midnight. GitHub's published asset digest is mandatory before installation.
TTVLOL_UPDATE_MINUTE_OF_DAY = 0


def _ttvlol_update_minute(_settings: dict) -> int:
    return TTVLOL_UPDATE_MINUTE_OF_DAY


def _ttvlol_update_time_label(_settings: dict) -> str:
    return "00:00"


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
    return bool(_text_setting(settings, "channels"))


def _run_scheduled_tick() -> None:
    settings = _load_settings()

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
                    state = _load_schedule_state(settings)
                    state.update({
                        "last_epg_refresh": int(time.time()),
                        "last_epg_status": "skipped",
                        "last_epg_skip_reason": "no twitch inputs configured",
                    })
                    _save_schedule_state(settings, state)
                else:
                    prebuilt = _gather_entries(settings)
                    sync_result = _run_sync_channels(settings, prebuilt=prebuilt, warm_images=False)
                    state = _load_schedule_state(settings)
                    state.update({
                        "last_epg_refresh": int(time.time()),
                        "last_epg_status": "ok",
                        "last_sync_result": sync_result,
                        "last_epg_result": sync_result.get("guide", {}),
                        "last_media_server_refresh": sync_result.get("media_server_refresh", {}),
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

def _scheduler_loop() -> None:
    # Self-update / plugin reload swaps the module behind the running thread.
    # When that happens, every relative import in this loop raises
    # ModuleNotFoundError ('_dispatcharr_plugin_twitcharr' is gone from
    # sys.modules) and we'd spam the log forever. Bail out instead — the
    # freshly loaded module starts its own scheduler in Plugin.__init__.
    own_module = __name__
    logger.info("Twitcharr self-scheduler started")
    while not _scheduler_stop.is_set():
        import sys
        if own_module not in sys.modules:
            logger.info("Twitcharr scheduler exiting: module %s was unloaded", own_module)
            return
        try:
            from django.db import close_old_connections

            close_old_connections()
            _run_scheduled_tick()
        except ImportError as exc:
            logger.info("Twitcharr scheduler exiting after plugin reload: %s", exc)
            return
        except Exception:
            logger.exception("Twitcharr scheduler tick failed")
        finally:
            try:
                from django.db import close_old_connections

                close_old_connections()
            except Exception:
                logger.exception("Twitcharr scheduler could not close its database connection")
        _scheduler_stop.wait(SCHEDULER_POLL_SECONDS)
    logger.info("Twitcharr self-scheduler stopped")


def _start_scheduler() -> bool:
    global _scheduler_thread
    if _is_web_server_process():
        logger.info(
            "Twitcharr scheduler not started in web-server process; "
            "it runs in the Celery worker processes instead"
        )
        return False
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
        if _scheduler_thread is not threading.current_thread():
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
    if _scheduler_is_running():
        scheduler_state = "running"
    elif _is_web_server_process():
        # Actions run in web workers; the scheduler lives in Celery workers.
        scheduler_state = "running in background worker processes"
    else:
        scheduler_state = "stopped"
    return {
        "status": "ok",
        "scheduler": scheduler_state,
        "started_now": started,
        "epg_refresh": f"every {_interval_minutes(merged)} minutes",
        "ttvlol_update": "check newest verified release daily at midnight (server time)",
        **_delete_legacy_celery_tasks(),
    }


# ---------------------------------------------------------------------------
# Plugin class
# ---------------------------------------------------------------------------

class Plugin:
    name = "Twitcharr"
    version = str(_MANIFEST.get("version") or "1.3.2")
    description = (
        "Twitch Live TV for Dispatcharr with anonymous metadata, Streamlink "
        "playback, XMLTV guide data and channel sync. No Twitch sign-in required."
    )
    author = "eliasbruno124"
    help_url = GITHUB_REPO_URL
    donate_url = DONATE_URL

    fields: list[dict] = _MANIFEST.get("fields", [])
    actions: list[dict] = _MANIFEST.get("actions", [])

    def __init__(self):
        try:
            _stop_superseded_schedulers()
            _start_scheduler()
        except Exception:
            logger.exception("Could not start persisted Twitcharr scheduler")

    def run(self, action: str, params: dict, context: dict):
        settings = _merge_defaults((context or {}).get("settings") or {})
        plugin_logger = (context or {}).get("logger") or logger
        params = params or {}

        try:
            validation = _validate_settings(settings)
            if validation["errors"] and action != "uninstall":
                return {
                    "status": "error",
                    "message": "Plugin settings are invalid: " + " ".join(validation["errors"]),
                    "validation": validation,
                }

            result: dict
            if action == "setup":
                result = _run_setup(settings)
            elif action == "sync_channels":
                result = _run_sync_channels(settings)
            elif action == "refresh_epg":
                result = _run_refresh_epg(settings)
            elif action == "update_ttvlol":
                result = _run_update_ttvlol(settings, force=_bool_setting(params, "force", False))
            elif action == "run_all":
                result = _run_all(settings)
            elif action == "refresh_media_server":
                result = _run_refresh_media_server(settings)
            elif action == "measure_bandwidth":
                result = _run_measure_bandwidth(settings)
            elif action == "test_proxies":
                result = _run_test_proxies(settings)
            elif action == "uninstall":
                result = _run_uninstall(settings)
            else:
                result = {"status": "error", "message": f"Unknown action: {action}"}
            if validation["warnings"]:
                result["validation"] = validation
            return result
        except Exception as e:
            plugin_logger.exception("Action %s failed", action)
            return {"status": "error", "message": str(e)}

    def stop(self, context: dict | None = None):
        context = context or {}
        reason = str(context.get("reason") or "").strip().lower()
        uninstall_reasons = {
            "delete",
            "deleted",
            "remove",
            "removed",
            "uninstall",
            "uninstalled",
        }

        try:
            if reason in uninstall_reasons:
                settings = _merge_defaults(context.get("settings") or {})
                return _run_uninstall(settings)
            _stop_scheduler()
            return {
                "status": "ok",
                "message": "Scheduler stopped.",
                "reason": reason or "stop",
            }
        except Exception as e:
            logger.exception("Failed to stop Twitcharr plugin")
            return {"status": "error", "message": str(e)}

    def uninstall(self, context: dict | None = None):
        settings = _merge_defaults((context or {}).get("settings") or {})
        try:
            return _run_uninstall(settings)
        except Exception as e:
            logger.exception("Plugin uninstall cleanup failed")
            return {"status": "error", "message": str(e)}

    def on_uninstall(self, context: dict | None = None):
        return self.uninstall(context)
