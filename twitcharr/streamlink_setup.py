"""Manages the StreamProfile / Channel / Stream / Logo / ChannelGroup rows
used to play Twitch streams via streamlink + the auto-updated streamlink-ttvlol.

The plugin marks every object it owns with a custom_properties tag so it can
clean up safely on uninstall. Channels for logins that are no longer live (and
'show offline channels' is OFF) are pruned each cycle so the lineup tracks the
actual live state.
"""

from __future__ import annotations

import logging
import shlex

from django.db import transaction

from . import ttvlol
from .epg import TVG_ID_PREFIX, channel_tvg_id

logger = logging.getLogger(__name__)

PROFILE_NAME = "Twitcharr (ad-free, low-latency)"
OWNER_TAG = "twitcharr"
LEGACY_PLACEHOLDER_TVG_ID = "twitch._placeholder_"


# ---------------------------------------------------------------------------
# Stream profile
# ---------------------------------------------------------------------------

def build_streamlink_parameters(
    *,
    plugin_dirs: str,
    proxy_servers: str,
    quality: str,
    low_latency: bool,
    fast_startup: bool = True,
) -> str:
    """Return the value for StreamProfile.parameters (shlex-quoted, single line).

    `fast_startup=True` shaves the perceptual latency between channel-switch
    and first frame by being aggressive about retries and HLS playlist reloads.
    Combined with adaptive quality (which already picks a variant your
    bandwidth can sustain), the chosen quality is conservative enough that the
    aggressive startup never causes mid-stream stutter.
    """
    base_http_timeout = "5" if fast_startup else "10"
    base_segment_timeout = "4" if fast_startup else "6"
    base_stream_timeout = "10" if fast_startup else "20"
    base_segment_attempts = "1" if fast_startup else "2"

    parts: list[str] = [
        "--loglevel", "warning",
        "--stdout",
        "--plugin-dir", plugin_dirs,
        "--http-timeout", base_http_timeout,
        "--stream-segment-attempts", base_segment_attempts,
        "--stream-segment-timeout", base_segment_timeout,
        "--stream-timeout", base_stream_timeout,
        # Dispatcharr containers usually do not have a usable browser. With
        # ttv.lol playlist proxies, Streamlink does not need client-integrity.
        "--webbrowser", "no",
        "--twitch-disable-ads",
        "--twitch-proxy-playlist-fallback",
        "--twitch-access-token-param", "playerType=site",
        "--twitch-access-token-param", "platform=web",
        "--http-header", "User-Agent={userAgent}",
        "--retry-streams", "1",
        "--retry-max", "2",
    ]
    if fast_startup:
        parts.extend([
            "--hls-playlist-reload-attempts", "2",
            "--hls-playlist-reload-time", "segment",
        ])
    if proxy_servers.strip():
        parts.extend(["--twitch-proxy-playlist", proxy_servers.strip()])
    if low_latency:
        parts.extend([
            "--twitch-low-latency",
            "--hls-live-edge", "1" if fast_startup else "2",
            "--stream-segment-threads", "4" if fast_startup else "3",
            "--hls-segment-stream-data",
        ])
    parts.append("{streamUrl}")
    parts.append(quality or "best")
    return " ".join(shlex.quote(p) for p in parts)


def get_or_create_stream_profile(
    *,
    data_dir: str,
    proxy_servers: str,
    quality: str,
    low_latency: bool,
    fast_startup: bool = True,
):
    from core.models import StreamProfile

    plugin_dirs = ttvlol.plugin_dir(data_dir)
    parameters = build_streamlink_parameters(
        plugin_dirs=plugin_dirs,
        proxy_servers=proxy_servers,
        quality=quality,
        low_latency=low_latency,
        fast_startup=fast_startup,
    )

    profile, _ = StreamProfile.objects.update_or_create(
        name=PROFILE_NAME,
        defaults={
            "command": "streamlink",
            "parameters": parameters,
            "is_active": True,
            "locked": False,
        },
    )
    return profile


# ---------------------------------------------------------------------------
# Channels & streams
# ---------------------------------------------------------------------------

def _logo_for(login: str, display_name: str, icon_url: str):
    from apps.channels.models import Logo

    if not icon_url:
        return None
    logo, _ = Logo.objects.update_or_create(
        url=icon_url,
        defaults={"name": f"Twitch: {display_name or login}"},
    )
    return logo


def _channel_group(name: str):
    from apps.channels.models import ChannelGroup

    group, _ = ChannelGroup.objects.get_or_create(name=name)
    return group


def _custom_m3u_account():
    """Dispatcharr ships a built-in 'custom' M3UAccount for user-created streams.
    Channel.get_stream() requires every Stream to be linked to one — without it,
    playback returns 'No active profiles found'. We piggyback on this account.
    """
    from apps.m3u.models import M3UAccount

    try:
        account = M3UAccount.get_custom_account()
    except M3UAccount.DoesNotExist:
        account, _ = M3UAccount.objects.get_or_create(
            name="custom",
            defaults={"is_active": True, "locked": True, "max_streams": 0},
        )

    if not account.is_active or account.max_streams != 0:
        account.is_active = True
        account.max_streams = 0
        account.save(update_fields=["is_active", "max_streams"])

    _ensure_custom_account_profile(account)
    return account


def _ensure_custom_account_profile(account):
    from apps.m3u.models import M3UAccountProfile

    profile, _ = M3UAccountProfile.objects.get_or_create(
        m3u_account=account,
        is_default=True,
        defaults={
            "name": f"{account.name} Default",
            "max_streams": 0,
            "is_active": True,
            "search_pattern": "^(.*)$",
            "replace_pattern": "$1",
        },
    )
    updates = []
    if not profile.is_active:
        profile.is_active = True
        updates.append("is_active")
    if profile.max_streams != 0:
        profile.max_streams = 0
        updates.append("max_streams")
    if updates:
        profile.save(update_fields=updates)


def _next_channel_number(starting_from: float, used: set[float]) -> float:
    n = starting_from
    while n in used:
        n += 1
    return n


def _delete_legacy_placeholder() -> tuple[int, int]:
    """Remove placeholder rows created by older Twitcharr versions."""
    from apps.channels.models import Channel, Stream

    channels = Channel.objects.filter(tvg_id=LEGACY_PLACEHOLDER_TVG_ID)
    channel_count = channels.count()
    channels.delete()

    streams = Stream.objects.filter(
        custom_properties__owner=OWNER_TAG,
        custom_properties__is_placeholder=True,
    )
    stream_count = streams.count()
    streams.delete()

    try:
        from apps.epg.models import EPGData, ProgramData

        placeholder_epg = EPGData.objects.filter(tvg_id=LEGACY_PLACEHOLDER_TVG_ID)
        ProgramData.objects.filter(epg__in=placeholder_epg).delete()
        placeholder_epg.delete()
    except Exception:
        logger.exception("Legacy placeholder EPG cleanup failed (non-fatal)")

    return channel_count, stream_count


@transaction.atomic
def sync_channels(
    entries: list[dict],
    *,
    data_dir: str,
    group_name: str,
    starting_channel_number: int,
    proxy_servers: str,
    quality: str,
    low_latency: bool,
    offline_icon_url: str = "",
    offline_program_icon_url: str = "",
    fast_startup: bool = True,
) -> dict:
    """Create / update Channel + Stream rows for every entry.

    Idempotent: re-running matches existing rows by Stream.url and Channel.tvg_id.
    Channels for logins outside the current entry list are pruned (so toggling
    'show offline' OFF actually removes those channels).

    Returns counts plus a list of synced logins (for logging / UI).
    """
    from apps.channels.models import Channel, ChannelStream, Stream

    profile = get_or_create_stream_profile(
        data_dir=data_dir,
        proxy_servers=proxy_servers,
        quality=quality,
        low_latency=low_latency,
        fast_startup=fast_startup,
    )
    group = _channel_group(group_name)
    custom_account = _custom_m3u_account()

    created_channels = 0
    updated_channels = 0
    created_streams = 0
    synced_logins: list[str] = []
    real_entries = list(entries)
    synced_tvg_ids: set[str] = {channel_tvg_id(e["login"]) for e in real_entries}
    legacy_placeholder_channels, legacy_placeholder_streams = _delete_legacy_placeholder()
    pruned_channels, pruned_streams = _prune_unmanaged(synced_tvg_ids)
    pruned_channels += legacy_placeholder_channels
    pruned_streams += legacy_placeholder_streams

    # Prefetch every EPGData row for the upcoming tvg_ids in one query so we
    # can attach `epg_data` at Channel creation time. Without this, channels
    # are created with a NULL epg_data_id and then patched a few lines later,
    # which leaves a brief window where the UI shows the channel without its
    # guide data.
    epg_by_tvg: dict[str, "object"] = {}
    try:
        from apps.epg.models import EPGData

        all_tvg_ids = [channel_tvg_id(e["login"]) for e in real_entries]
        epg_by_tvg = {
            row.tvg_id: row
            for row in EPGData.objects.filter(tvg_id__in=all_tvg_ids)
        }
    except Exception:
        logger.exception("Could not prefetch EPGData rows; channels will be linked after creation")

    existing_channels = list(Channel.objects.filter(tvg_id__in=synced_tvg_ids))
    existing_by_tvg = {channel.tvg_id: channel for channel in existing_channels}

    # Keep existing channel numbers stable across syncs. Renumbering caused
    # Emby/Jellyfin to show stale guide data on the channel that took over
    # a freed slot (e.g. with include_offline=False, B moving from 9001 to
    # 9000 inherits A's cached programme for a while). New entries get the
    # next free slot above starting_channel_number; offline streamers leave
    # a numeric gap until they come back online.
    used_numbers: set[float] = set(
        Channel.objects.exclude(tvg_id__in=synced_tvg_ids).values_list("channel_number", flat=True)
    )
    for ch in existing_channels:
        if ch.channel_number:
            used_numbers.add(float(ch.channel_number))

    next_free_cursor = float(starting_channel_number)

    for idx, e in enumerate(real_entries):
        login = e["login"]
        tvg = channel_tvg_id(login)
        channel_name = e.get("channel_name") or e["display_name"]

        twitch_url = f"https://twitch.tv/{login}"
        logo = _logo_for(login, channel_name, e["icon_url"])

        existing_for_number = existing_by_tvg.get(tvg)
        if existing_for_number and existing_for_number.channel_number:
            number = float(existing_for_number.channel_number)
        else:
            number = _next_channel_number(next_free_cursor, used_numbers)
            used_numbers.add(number)
            next_free_cursor = number + 1

        stream_defaults = {
            "name": channel_name,
            "url": twitch_url,
            "logo_url": e["icon_url"] or None,
            "tvg_id": tvg,
            "stream_profile": profile,
            "is_custom": True,
            "m3u_account": custom_account,
            "custom_properties": {
                "owner": OWNER_TAG,
                "twitch_login": login,
                "twitch_live": bool(e.get("live")),
                "twitch_viewers": int(e.get("viewer_count") or 0),
            },
        }
        stream = Stream.objects.filter(url=twitch_url, is_custom=True).first()
        if stream:
            for k, v in stream_defaults.items():
                setattr(stream, k, v)
            stream.save()
        else:
            stream = Stream.objects.create(**stream_defaults)
            created_streams += 1

        epg_row = epg_by_tvg.get(tvg)
        ch_defaults = {
            "name": channel_name,
            "channel_group": group,
            "tvg_id": tvg,
            "stream_profile": profile,
            "logo": logo,
            "channel_number": number,
        }
        if epg_row is not None:
            ch_defaults["epg_data"] = epg_row
        channel = existing_by_tvg.get(tvg)
        if channel:
            for k, v in ch_defaults.items():
                setattr(channel, k, v)
            channel.save()
            updated_channels += 1
        else:
            channel = Channel.objects.create(**ch_defaults)
            created_channels += 1

        ChannelStream.objects.update_or_create(
            channel=channel, stream=stream, defaults={"order": 0}
        )

        synced_logins.append(login)

    return {
        "message": f"Synced {len(synced_logins)} Twitch channels.",
        "channels_created": created_channels,
        "channels_updated": updated_channels,
        "channels_pruned": pruned_channels,
        "streams_created": created_streams,
        "streams_pruned": pruned_streams,
        "channel_names": synced_logins,
        "stream_profile_id": profile.id,
        "channel_group_id": group.id,
    }


def _prune_unmanaged(keep_tvg_ids: set[str]) -> tuple[int, int]:
    """Delete managed Channels and Streams whose tvg_id isn't in `keep_tvg_ids`."""
    from apps.channels.models import Channel, Stream

    stale_channels = (
        Channel.objects.filter(streams__custom_properties__owner=OWNER_TAG)
        .exclude(tvg_id__in=keep_tvg_ids)
        .distinct()
    )
    stale_tvg_ids = list(stale_channels.values_list("tvg_id", flat=True))
    channel_count = stale_channels.count()
    stale_channels.delete()

    stream_count = 0
    if stale_tvg_ids:
        stale_streams = Stream.objects.filter(
            custom_properties__owner=OWNER_TAG,
            tvg_id__in=stale_tvg_ids,
        )
        stream_count = stale_streams.count()
        stale_streams.delete()

    return channel_count, stream_count


# ---------------------------------------------------------------------------
# Uninstall
# ---------------------------------------------------------------------------

@transaction.atomic
def uninstall_managed_objects() -> dict:
    from apps.channels.models import Channel, Stream
    from apps.epg.models import EPGSource
    from core.models import StreamProfile
    from django.db.models import Q

    streams = Stream.objects.filter(
        Q(custom_properties__owner=OWNER_TAG)
        | Q(tvg_id__startswith=TVG_ID_PREFIX)
    ).distinct()
    stream_count = streams.count()

    channels = Channel.objects.filter(
        Q(streams__custom_properties__owner=OWNER_TAG)
        | Q(tvg_id__startswith=TVG_ID_PREFIX)
    ).distinct()
    channel_count = channels.count()
    channels.delete()
    streams.delete()

    profile_count = StreamProfile.objects.filter(name=PROFILE_NAME).delete()[0]

    from .epg import EPG_SOURCE_NAME

    source_count = EPGSource.objects.filter(name=EPG_SOURCE_NAME).delete()[0]

    return {
        "channels_deleted": channel_count,
        "streams_deleted": stream_count,
        "stream_profile_deleted": profile_count,
        "epg_source_deleted": source_count,
    }
