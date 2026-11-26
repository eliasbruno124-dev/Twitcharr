"""Twitcharr — Twitch live-TV plugin for Dispatcharr.

Combines:
  * an auto-updated streamlink-ttvlol twitch.py for proxy-aware low-latency playback
  * a continuously refreshed XMLTV guide (twitch2tuner-style)
  * direct Channel/Stream/EPGData rows in Dispatcharr — no manual M3U/EPG setup
  * Twitch channel discovery (game/top/search) directly inside the lineup field
  * coalesced Emby/Jellyfin guide refreshes on meaningful EPG changes
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import logging
import os
import sys
import threading
import time
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Manifest / defaults
# ---------------------------------------------------------------------------

PLUGIN_KEY = "twitcharr"
GITHUB_REPO = "eliasbruno124-dev/Twitcharr"
GITHUB_REPO_URL = f"https://github.com/{GITHUB_REPO}"
GITHUB_RAW_URL = f"https://raw.githubusercontent.com/{GITHUB_REPO}/main"
DONATE_URL = "https://paypal.me/eliasbruno123"
OFFLINE_ARTWORK_URL = f"{GITHUB_RAW_URL}/twitcharr/assets/offline.png"
# Fallback chains per maximum quality. 60 fps variants first: some streamers
# only offer HD at 60 fps. Lower variants follow so a stream without the
# exact variant still starts; "best" is the last resort for streams that only
# offer their source quality.
MAX_QUALITY_CHAINS = {
    "1080p": "1080p60,1080p,1080p30,720p60,720p,720p30,480p30,480p,360p30,360p,160p30,160p,best",
    "720p": "720p60,720p,720p30,480p30,480p,360p30,360p,160p30,160p,best",
    "480p": "480p30,480p,360p30,360p,160p30,160p,best",
}
BANDWIDTH_RECHECK_SECONDS = 24 * 3600


# Built-in starting list; the project's public list is added at runtime.
DEFAULT_TTVLOL_PROXY_SERVERS = (
    "https://eu.luminous.dev,"
    "https://eu2.luminous.dev,"
    "https://lb-eu.cdn-perfprod.com,"
    "https://lb-eu2.cdn-perfprod.com"
)
# Headroom above Twitch's advertised bitrate for adaptive quality.
BANDWIDTH_SAFETY_MARGIN_PCT = 50
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
    "programme_artwork": "category",
    "epg_refresh_interval_minutes": 2,
    "adblock": True,
    "max_quality": "auto",
    "connection_bandwidth_mbps": 0,
    "media_server_url": "",
    "media_server_api_key": "",
}

MEDIA_SERVER_REFRESH_MAX_AGE_SECONDS = 15 * 60


def _load_manifest() -> dict:
    try:
        with open(os.path.join(os.path.dirname(__file__), "plugin.json"), "r", encoding="utf-8") as f:
            return json.loads(f.read())
    except Exception:
        logger.exception("Could not load plugin.json")
        return {}


_MANIFEST = _load_manifest()
try:
    # Fingerprint of the running code, recorded with every scheduled refresh
    # so a stale module in a long-lived worker process is easy to spot.
    with open(__file__, "rb") as _source:
        _CODE_ID = hashlib.sha256(_source.read()).hexdigest()[:12]
except OSError:
    _CODE_ID = "unknown"


def _merge_defaults(settings: dict | None) -> dict:
    merged: dict[str, Any] = dict(DEFAULT_SETTINGS)
    for field in _MANIFEST.get("fields", []):
        field_id = field.get("id")
        if field_id and "default" in field:
            merged[field_id] = field.get("default")
    merged.update(settings or {})
    if _legacy_adblock_off(settings or {}):
        merged["adblock"] = False
    legacy_quality = _legacy_max_quality(settings or {})
    if legacy_quality:
        merged["max_quality"] = legacy_quality
    return merged


def _legacy_max_quality(raw: dict) -> str:
    """Maximum quality for a "Stream quality" value saved before v1.3.4."""
    if "max_quality" in raw:
        return ""
    value = str(raw.get("stream_quality") or "").strip().lower()
    if not value:
        return ""
    if value == "adaptive":
        return "auto"
    first = value.split(",", 1)[0].strip()
    if first in {"best", "source"} or first.startswith("1080"):
        return "1080p"
    if first.startswith("720"):
        return "720p"
    if first.startswith(("480", "360", "160", "worst", "audio")):
        return "480p"
    return "auto"


def _legacy_adblock_off(raw: dict) -> bool:
    """True when ad blocking was turned off with a setting of an earlier version.

    v1.3.4 had an on/off ttv.lol switch, earlier versions an emptied proxy
    field to turn proxies off; both "off" choices stay off.
    """
    if "adblock" in raw:
        return False
    if "ad_blocking" in raw:
        return str(raw.get("ad_blocking")).strip().lower() == "off"
    if "use_ttvlol" in raw:
        return str(raw.get("use_ttvlol")).strip().lower() in {"false", "0", "off", "no", ""}
    return str(raw.get("ttvlol_proxy_servers", "x")).strip() == ""


def _migrate_legacy_settings() -> bool:
    """Store settings that replace pre-1.3.4 fields, once.

    Dispatcharr fills missing fields with manifest defaults before actions
    run, so only the stored settings show whether the switch was ever set.
    Saving it makes the settings page, actions and scheduler agree.
    """
    try:
        from apps.plugins.models import PluginConfig

        cfg = PluginConfig.objects.filter(key=PLUGIN_KEY).first()
        if not cfg or not isinstance(cfg.settings, dict):
            return False
        changed = False
        legacy_quality = _legacy_max_quality(cfg.settings)
        if legacy_quality:
            _save_setting("max_quality", legacy_quality)
            logger.info("Twitcharr: stream quality from an earlier version became maximum quality %s", legacy_quality)
            changed = True
        if _legacy_adblock_off(cfg.settings):
            _save_setting("adblock", False)
            logger.info("Twitcharr: ad blocking switched off in an earlier version stays off")
            changed = True
        return changed
    except Exception:
        logger.debug("Twitcharr could not migrate legacy settings", exc_info=True)
    return False


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


def _adblock(settings: dict) -> bool:
    return _bool_setting(settings, "adblock", True)


def _proxies_enabled(settings: dict) -> bool:
    # Playback starts through the ttv.lol proxies; the runner switches to the
    # embedded player once Streamlink reports an ad anyway.
    return _adblock(settings)


def _player_type(settings: dict) -> str:
    # Used for direct requests: the embed-player switch and ttv.lol's own
    # fallback. Twitch plays far fewer ads for embedded players.
    return "embed" if _adblock(settings) else "site"


def _base_proxies(settings: dict) -> list[str]:
    """Built-in proxies plus any list saved by earlier versions' proxy field."""
    proxies = _proxy_list(DEFAULT_TTVLOL_PROXY_SERVERS)
    for url in _proxy_list(str(settings.get("ttvlol_proxy_servers") or "")):
        if url not in proxies:
            proxies.append(url)
    return proxies


def _proxy_servers(settings: dict) -> str:
    """Proxies used before the first measurement (empty: ttv.lol is off)."""
    return ",".join(_base_proxies(settings)) if _proxies_enabled(settings) else ""


PROXY_RANK_INTERVAL_SECONDS = 10 * 60
MAX_RANKED_PROXIES = 6


def _proxy_list(csv: str) -> list[str]:
    return [u.strip() for u in (csv or "").split(",") if u.strip()]


def _is_http_proxy(url: str) -> bool:
    return url.lower().startswith("httpproxy://")


def _proxy_rank_key(settings: dict) -> str:
    # The measured order belongs to these inputs; changing them invalidates it.
    return "auto|" + ",".join(_base_proxies(settings))


def _effective_proxy_servers(settings: dict) -> str:
    """The ttv.lol proxies Streamlink gets: the last measured order, if any.

    With ttv.lol turned off Streamlink gets no proxy and plays from Twitch
    directly. Until a measurement exists the built-in list applies.
    """
    if not _proxies_enabled(settings):
        return ""
    fallback = _proxy_servers(settings)
    try:
        state = _load_schedule_state(settings)
    except Exception:
        return fallback
    ranked = state.get("proxy_order")
    if state.get("proxy_rank_key") != _proxy_rank_key(settings) or not isinstance(ranked, list) or not ranked:
        return fallback
    return ",".join(str(url) for url in ranked)


def _proxy_rank_due(settings: dict, state: dict, now: float) -> bool:
    if not _proxies_enabled(settings):
        return False
    if state.get("proxy_rank_key") != _proxy_rank_key(settings):
        return True
    try:
        last = float(state.get("last_proxy_rank") or 0)
    except (TypeError, ValueError):
        last = 0.0
    return now - last >= PROXY_RANK_INTERVAL_SECONDS


def _proxy_order(base: list[str], results: list[dict]) -> list[str]:
    """Best-first proxy list for Streamlink from a ranking.

    streamlink-ttvlol walks the list until one proxy returns a playlist and
    then falls back to Twitch itself. Playlist proxies (https://) fail with an
    error it catches, so dead ones only cost time. An HTTP proxy that fails
    raises an error it does not catch: the stream start fails and the fallback
    never runs. HTTP proxies are therefore only listed when no playlist proxy
    works, as the one ad-free option left.
    """
    ok = [r["url"] for r in results if r.get("status") == "ok"]
    playlist_ok = [u for u in ok if not _is_http_proxy(u)][:MAX_RANKED_PROXIES]
    untested = [r["url"] for r in results if r.get("status") == "untested" and r["url"] in base]
    if playlist_ok:
        head = playlist_ok + untested
    else:
        head = [u for u in ok if _is_http_proxy(u)][:2] + untested
    # Built-in playlist proxies that failed stay at the end: they come back on
    # their own once they work again and cost nothing while others work.
    tail = [u for u in base if u not in head and not _is_http_proxy(u)]
    return head + tail


def _candidate_proxies(settings: dict) -> list[str]:
    from . import ttvlol

    if not _proxies_enabled(settings):
        return []
    candidates = _base_proxies(settings)
    for url in ttvlol.project_proxies(_data_dir(settings)):
        if url not in candidates:
            candidates.append(url)
    return candidates


def _rank_proxies(settings: dict, live_logins: list[str]) -> dict:
    """Measure the ttv.lol proxies against a live channel, best first.

    Candidates are the built-in proxies plus the streamlink-ttvlol project's
    public list (downloaded daily). Rewrites only the Streamlink config file,
    which is read at every stream start; running streams keep their proxy.
    Without a live channel, or when no proxy works (network trouble), the
    order stays as it is.
    """
    from . import diagnostics, streamlink_setup, ttvlol

    if not live_logins:
        return {"status": "skipped", "reason": "no live channel to probe"}
    base = _base_proxies(settings)
    candidates = _candidate_proxies(settings)
    before = _effective_proxy_servers(settings)
    results = diagnostics.rank_playlist_proxies(",".join(candidates), live_logins[0], timeout=5.0)
    for r in results:
        r["source"] = "built-in" if r["url"] in base else "ttv.lol list"
    state = _load_schedule_state(settings)
    state.update({"last_proxy_rank": int(time.time()), "last_proxy_rank_result": results})
    if any(r.get("status") == "ok" for r in results):
        state["proxy_order"] = _proxy_order(base, results)
        state["proxy_rank_key"] = _proxy_rank_key(settings)
    _save_schedule_state(settings, state)
    after = _effective_proxy_servers(settings)
    if after != before:
        data_dir = _data_dir(settings)
        streamlink_setup._write_streamlink_config(
            data_dir=data_dir,
            plugin_dirs=ttvlol.plugin_dir(data_dir),
            proxy_servers=after,
            low_latency=False,
            fast_startup=False,
            player_type=_player_type(settings),
        )
        logger.info("Twitcharr ttv.lol proxy order: %s", after)
    return {"status": "ok", "order": _proxy_list(after), "changed": after != before}


def _offline_icon_url(settings: dict, *, cache_bust: int | None = None) -> str:
    version = str(_MANIFEST.get("version") or "").strip() or int(time.time())
    return f"{OFFLINE_ARTWORK_URL}?v={version}"


def _offline_program_icon_url(settings: dict, *, cache_bust: int | None = None) -> str:
    version = str(_MANIFEST.get("version") or "").strip() or int(time.time())
    return f"{OFFLINE_ARTWORK_URL}?v={version}"


# ---------------------------------------------------------------------------
# Settings helpers
# ---------------------------------------------------------------------------


def _stored_settings() -> dict:
    try:
        from apps.plugins.models import PluginConfig

        cfg = PluginConfig.objects.filter(key=PLUGIN_KEY).first()
        return dict(cfg.settings) if cfg and isinstance(cfg.settings, dict) else {}
    except Exception:
        return {}


def _load_settings() -> dict:
    try:
        from apps.plugins.models import PluginConfig

        cfg = PluginConfig.objects.filter(key=PLUGIN_KEY).first()
        if cfg and isinstance(cfg.settings, dict):
            if _legacy_adblock_off(cfg.settings) or _legacy_max_quality(cfg.settings):
                if _migrate_legacy_settings():
                    cfg.refresh_from_db()
            return _merge_defaults(cfg.settings)
    except Exception:
        logger.exception("Failed to load plugin settings from DB")
    return _merge_defaults({})


def _save_setting(key: str, value: Any) -> None:
    from django.db import transaction

    from apps.plugins.models import PluginConfig

    # Lock the row like Dispatcharr's own settings save, so a settings form
    # saved at the same moment cannot overwrite this value or vice versa.
    with transaction.atomic():
        cfg = PluginConfig.objects.select_for_update().get(key=PLUGIN_KEY)
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


def _max_quality(settings: dict) -> str:
    mode = _text_setting(settings, "max_quality", "auto", fallback_on_empty=True).strip().lower()
    return mode if mode == "auto" or mode in MAX_QUALITY_CHAINS else "auto"


def _resolve_stream_quality(settings: dict) -> str:
    """Return the quality chain passed to Streamlink.

    "auto" picks the highest Twitch variant the measured connection can
    carry (re-measured daily); a fixed maximum uses its fallback chain.
    Streamlink takes the first variant of the chain the stream offers.
    """
    mode = _max_quality(settings)
    if mode != "auto":
        return MAX_QUALITY_CHAINS[mode]

    from . import bandwidth

    mbps = _float_setting(settings, "connection_bandwidth_mbps", 0)
    if mbps <= 0:
        try:
            measured = float(_load_schedule_state(settings).get("last_bandwidth_mbps") or 0)
            if measured > 0:
                mbps = measured
        except Exception as exc:
            logger.debug("Could not load the last measured bandwidth: %s", exc)
    return bandwidth.quality_chain_for_bandwidth(mbps, safety_margin_pct=BANDWIDTH_SAFETY_MARGIN_PCT)


def _twitch_channel_playing() -> bool:
    """True while any Twitcharr channel is being played through Dispatcharr."""
    try:
        from apps.channels.models import Channel
        from apps.proxy.live_proxy.redis_keys import RedisKeys
        from core.utils import RedisClient

        client = RedisClient.get_client()
        uuids = Channel.objects.filter(streams__custom_properties__owner="twitcharr").values_list("uuid", flat=True)
        return any(client.exists(RedisKeys.channel_metadata(str(uuid))) for uuid in uuids.distinct())
    except Exception:
        logger.debug("Could not check for running Twitch channels", exc_info=True)
        return False


def _bandwidth_check_due(settings: dict, state: dict, now: float) -> bool:
    if _max_quality(settings) != "auto":
        return False
    try:
        last = float(state.get("last_bandwidth_at") or 0)
    except (TypeError, ValueError):
        last = 0.0
    if now - last < BANDWIDTH_RECHECK_SECONDS:
        return False
    # A running stream would share the line with the speed test and make the
    # measurement (the slowest of several probes) too low: try again later.
    return not _twitch_channel_playing()


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
    # Changing image URLs make Emby/Jellyfin download every image again. Box
    # art and avatars have their own URL per image, so a daily value is enough
    # to pick up replaced images; the live preview frame changes constantly.
    now = int(time.time())
    cache_bust = now if _programme_artwork(settings) == "live" else now - now % 86400
    entries = epg.build_entries(
        client,
        logins,
        include_offline=_bool_setting(settings, "include_offline", True),
        offline_icon_url=_offline_icon_url(settings, cache_bust=cache_bust),
        offline_program_icon_url=_offline_program_icon_url(settings, cache_bust=cache_bust),
        use_live_thumbnails=_programme_artwork(settings) == "live",
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


def _programme_artwork(settings: dict) -> str:
    mode = _text_setting(
        settings, "programme_artwork", DEFAULT_SETTINGS["programme_artwork"], fallback_on_empty=True
    ).strip().lower()
    return mode if mode in {"live", "category"} else DEFAULT_SETTINGS["programme_artwork"]


def _write_epg(settings: dict, entries: list[dict], *, prune: bool = True) -> dict:
    from . import epg

    data_dir = _data_dir(settings)
    channels, programmes = epg.write_xmltv(entries, epg.xmltv_path(data_dir))
    db_result = epg.upsert_db(entries, data_dir, prune=prune)
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


def _write_lineup_guide(settings: dict, entries: list[dict], *, prune: bool = True) -> dict:
    return _write_epg(settings, _lineup_epg_entries(settings, entries), prune=prune)


def _lookup_complete(client) -> bool:
    """False when Twitch did not answer for some logins in this cycle."""
    failed = getattr(client, "lookup_failed", None)
    if failed:
        logger.warning("Twitch did not answer for %s; keeping their channels this cycle", ", ".join(sorted(failed)))
    return not failed


def _sync_channels_from_entries(settings: dict, entries: list[dict], *, prune: bool = True) -> dict:
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
        proxy_servers=_effective_proxy_servers(settings),
        quality=_resolve_stream_quality(settings),
        low_latency=False,
        fast_startup=False,
        player_type=_player_type(settings),
        offline_icon_url=_offline_icon_url(settings),
        offline_program_icon_url=_offline_program_icon_url(settings),
        profile_names=_profile_names(settings),
        prune=prune,
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

    write_result = _write_lineup_guide(settings, entries, prune=_lookup_complete(client))

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


def _run_sync_channels(
    settings: dict,
    *,
    prebuilt=None,
    warm_images: bool = True,
    refresh_media_server: bool = True,
) -> dict:
    from . import epg

    if prebuilt is None:
        client, logins, entries = _gather_entries(settings)
    else:
        client, logins, entries = prebuilt
    if not logins:
        return {"status": "error", "message": "No Twitch channels configured."}
    if not entries and not getattr(client, "_users", {}):
        return {"status": "error", "message": "No matching Twitch channels found."}
    prune = _lookup_complete(client)
    guide_result = _write_lineup_guide(settings, entries, prune=prune)
    result = _sync_channels_from_entries(settings, entries, prune=prune)
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
    should_refresh_media_server = refresh_media_server or changed_channels
    media_server_refresh = (
        _trigger_media_server(
            settings,
            warm_images=warm_images and not changed_channels,
        )
        if should_refresh_media_server
        else {
            "status": "skipped",
            "message": "Guide content is unchanged; media-server refresh coalesced.",
        }
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
        proxy_servers=_effective_proxy_servers(settings),
        quality=_resolve_stream_quality(settings),
        low_latency=False,
        fast_startup=False,
        player_type=_player_type(settings),
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


MEDIA_LOGO_RECHECK_SECONDS = 30 * 60


def _current_channel_logos() -> dict[str, str]:
    from apps.channels.models import Channel

    channels = (
        Channel.objects.filter(streams__custom_properties__owner="twitcharr")
        .select_related("logo")
        .distinct()
    )
    return {c.name: c.logo.url for c in channels if c.logo_id and c.logo and c.logo.url}


def _sync_media_logos(settings: dict, *, force: bool = False) -> dict:
    """Keep the Emby/Jellyfin channel logos equal to Dispatcharr's.

    Uploads only when a logo changed, plus a full check every 30 minutes
    (media-server channel items can be recreated). After a failure the next
    attempt waits 30 minutes instead of retrying on every scheduler tick.
    """
    from . import media_server

    url = _text_setting(settings, "media_server_url")
    key = _text_setting(settings, "media_server_api_key")
    if not url or not key:
        return {"status": "skipped", "message": "Emby/Jellyfin URL or API key not configured"}
    logos = _current_channel_logos()
    state = _load_schedule_state(settings)
    previous = state.get("media_logos") or {}
    # Channels the media server could not match or whose logo failed: they
    # wait for the 30-minute check instead of triggering a full run per tick.
    pending = state.get("media_logos_pending") or {}
    now = time.time()

    def settled(name: str, logo: str) -> bool:
        done = previous.get(name)
        return (isinstance(done, dict) and done.get("url") == logo) or pending.get(name) == logo

    unchanged = all(settled(name, logo) for name, logo in logos.items())
    checked = float(state.get("media_logos_checked") or 0)
    if not force and unchanged and now - checked < MEDIA_LOGO_RECHECK_SECONDS:
        return {"status": "unchanged", "message": "Channel logos are up to date"}
    result = media_server.sync_channel_logos(base_url=url, api_key=key, logos=logos, uploaded=previous)
    state = _load_schedule_state(settings)
    state["media_logos"] = result.pop("uploaded", previous)
    state["media_logos_pending"] = {
        name: logos[name] for name in (result.get("failed") or []) + (result.get("skipped") or []) if name in logos
    }
    if result.get("status") == "error" and not result.get("changed"):
        # Media server unreachable or refusing: every channel waits for the next check.
        state["media_logos_pending"] = dict(logos)
    state["media_logos_checked"] = int(now)
    state.pop("media_logos_retry_after", None)
    _save_schedule_state(settings, state)
    return result


def _run_refresh_media_server(settings: dict) -> dict:
    trigger = _trigger_media_server(settings)
    try:
        trigger["logos"] = _sync_media_logos(settings, force=True)
    except Exception as exc:
        logger.exception("Twitcharr could not update media-server channel logos")
        trigger["logos"] = {"status": "error", "message": type(exc).__name__}
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

    bandwidth_mbps = _float_setting(settings, "connection_bandwidth_mbps", 0)
    if bandwidth_mbps < 0:
        errors.append("Connection bandwidth must be 0 or higher.")

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

    csv = ",".join(_candidate_proxies(settings))
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
        "active_order": _proxy_list(_effective_proxy_servers(settings)),
        "next": (
            "Twitcharr orders the proxies automatically and skips dead ones."
            if total
            else "The ad blocker is turned off."
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
            proxy_servers=_effective_proxy_servers(effective_settings),
            quality=_resolve_stream_quality(effective_settings),
            low_latency=False,
            fast_startup=False,
            player_type=_player_type(effective_settings),
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

    margin = BANDWIDTH_SAFETY_MARGIN_PCT
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
        "active_now": _max_quality(settings) == "auto",
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
    # Several processes save this file; a shared temp name could interleave.
    tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f)
    os.replace(tmp, path)


_LOCK_FDS: dict[str, int] = {}


def _job_lock(settings: dict, name: str, ttl_seconds: int = 3600) -> str:
    """Take a cross-process job lock, or return "" when another process has it.

    flock() on a file in the data directory: the kernel releases it when the
    holding process ends, so a lock left by a crash or container restart can
    never block the job (the old create-exclusive lock file survived those
    and blocked until its TTL; taking over a stale one was racy).
    `ttl_seconds` is kept for callers; a hung holder is ended by Dispatcharr.
    """
    os.makedirs(_data_dir(settings), exist_ok=True)
    path = os.path.join(_data_dir(settings), f".{name}.lock")
    if path in _LOCK_FDS:
        return ""  # already held by this process
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return ""
    os.ftruncate(fd, 0)
    os.write(fd, f"{os.getpid()} {int(time.time())}\n".encode())
    _LOCK_FDS[path] = fd
    return path


def _release_job_lock(path: str) -> None:
    fd = _LOCK_FDS.pop(path, None)
    if fd is not None:
        try:
            os.close(fd)  # closing releases the flock
        except OSError:
            pass


def _interval_minutes(settings: dict) -> int:
    return _int_setting(settings, "epg_refresh_interval_minutes", 2, min_value=1)


def _stable_artwork_url(value: str | None) -> str:
    """Remove cache-busters that do not represent a guide-content change."""
    if not value:
        return ""
    parsed = urlsplit(str(value))
    query = [
        (key, item)
        for key, item in parse_qsl(parsed.query, keep_blank_values=True)
        if key.lower() not in {"twarr_ts", "cache", "cache_bust", "cb", "_"}
    ]
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), parsed.fragment))


def _guide_semantic_fingerprint(entries: list[dict]) -> str:
    """Hash only changes that a media-server client can meaningfully display."""
    fields = []
    for entry in sorted(entries, key=lambda item: str(item.get("login") or "").lower()):
        fields.append({
            "login": str(entry.get("login") or "").lower(),
            "display_name": str(entry.get("display_name") or ""),
            "channel_name": str(entry.get("channel_name") or ""),
            "live": bool(entry.get("live")),
            # The programme title ends with the live viewer count, which changes
            # every minute; hashing it made Emby re-read the whole guide (all
            # tuners) on every 2-minute tick. Stream title and game are what a
            # viewer notices; the count is refreshed by the max-age rule.
            "stream_title": str(entry.get("stream_title") or ""),
            "game_name": str(entry.get("game_name") or ""),
            "channel_art": _stable_artwork_url(
                entry.get("icon_url_stable") or entry.get("icon_url")
            ),
            "programme_art": _stable_artwork_url(entry.get("program_icon_url")),
        })
    payload = json.dumps(fields, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _scheduled_media_refresh_due(state: dict, fingerprint: str, now: float) -> bool:
    last = float(state.get("last_media_server_refresh_at") or 0)
    return (
        state.get("last_guide_semantic_fingerprint") != fingerprint
        or now - last >= MEDIA_SERVER_REFRESH_MAX_AGE_SECONDS
    )


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
                    fingerprint = _guide_semantic_fingerprint(prebuilt[2])
                    refresh_media_server = _scheduled_media_refresh_due(
                        state, fingerprint, now
                    )
                    sync_result = _run_sync_channels(
                        settings,
                        prebuilt=prebuilt,
                        warm_images=False,
                        refresh_media_server=refresh_media_server,
                    )
                    state = _load_schedule_state(settings)
                    state.pop("last_epg_error", None)
                    state.update({
                        "last_epg_refresh": int(time.time()),
                        "last_epg_status": "ok",
                        "last_epg_pid": os.getpid(),
                        "last_epg_code": _CODE_ID,
                        "last_sync_result": sync_result,
                        "last_epg_result": sync_result.get("guide", {}),
                        "last_media_server_refresh": sync_result.get("media_server_refresh", {}),
                    })
                    if sync_result.get("media_server_status") in {"ok", "partial"}:
                        state.update({
                            "last_guide_semantic_fingerprint": fingerprint,
                            "last_media_server_refresh_at": int(time.time()),
                        })
                    _save_schedule_state(settings, state)
                    try:
                        _sync_media_logos(settings)
                    except Exception:
                        logger.exception("Twitcharr media-server logo update failed")
                    if _proxy_rank_due(settings, state, time.time()):
                        live_logins = [e.get("login") for e in prebuilt[2] if e.get("live") and e.get("login")]
                        try:
                            _rank_proxies(settings, live_logins)
                        except Exception:
                            logger.exception("Twitcharr ttv.lol proxy ranking failed")
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

    if _bandwidth_check_due(settings, state, time.time()):
        # Automatic quality: re-measure the connection once a day and update
        # the StreamProfile; running streams keep their quality.
        lock = _job_lock(settings, "bandwidth", ttl_seconds=10 * 60)
        if lock:
            try:
                result = _run_measure_bandwidth(settings)
                if result.get("status") != "ok":
                    # Retry tomorrow rather than every tick.
                    state = _load_schedule_state(settings)
                    state["last_bandwidth_at"] = int(time.time())
                    _save_schedule_state(settings, state)
            except Exception:
                logger.exception("Scheduled bandwidth check failed")
            finally:
                _release_job_lock(lock)


def _follow_plugin_reload() -> bool:
    """Apply a plugin reload that another Dispatcharr process announced.

    The scheduler runs in Celery worker processes that never execute plugin
    actions. Dispatcharr's plugin-leader-election thread normally follows the
    shared reload token there, but forked children do not inherit that thread
    and a long-running parent was seen keeping the old module for days: the
    guide kept being refreshed with old code until Dispatcharr restarted.
    Without a live leader thread in this process, run the same cached
    discovery it would, which reloads only when the token is newer. With one,
    leave it to Dispatcharr so two reloads never run concurrently.
    """
    if any(t.name == "plugin-leader-election" and t.is_alive() for t in threading.enumerate()):
        return False
    try:
        from apps.plugins.loader import PluginManager

        manager = PluginManager.get()
        get_token = getattr(manager, "_get_reload_token", None)
        seen = getattr(manager, "_last_reload_token", None)
        if not callable(get_token) or not isinstance(seen, (int, float)) or get_token() <= seen:
            return False
        manager.discover_plugins(sync_db=False, use_cache=True)
        return True
    except Exception:
        logger.debug("Twitcharr could not follow a plugin reload", exc_info=True)
        return False


def _scheduler_loop() -> None:
    # Self-update / plugin reload swaps the module behind the running thread.
    # When that happens, every relative import in this loop raises
    # ModuleNotFoundError ('_dispatcharr_plugin_twitcharr' is gone from
    # sys.modules) and we'd spam the log forever. Bail out instead — the
    # freshly loaded module starts its own scheduler in Plugin.__init__.
    own_module = __name__
    own = sys.modules.get(own_module)
    logger.info("Twitcharr self-scheduler started")
    while not _scheduler_stop.is_set():
        if _follow_plugin_reload() and _scheduler_stop.is_set():
            return
        current = sys.modules.get(own_module)
        if current is None or (own is not None and current is not own):
            logger.info("Twitcharr scheduler exiting: module %s was unloaded or replaced", own_module)
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
            from .output_guard import install
            if install():
                logger.info("Twitcharr output lifecycle guard enabled (pid=%s)", os.getpid())
        except Exception:
            logger.warning("Twitcharr output lifecycle guard unavailable")
        try:
            _stop_superseded_schedulers()
            _start_scheduler()
        except Exception:
            logger.exception("Could not start persisted Twitcharr scheduler")

    def run(self, action: str, params: dict, context: dict):
        settings = _merge_defaults((context or {}).get("settings") or {})
        if _migrate_legacy_settings():
            settings = _merge_defaults(_stored_settings() or (context or {}).get("settings") or {})
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
