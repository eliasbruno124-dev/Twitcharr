"""Manages the StreamProfile / Channel / Stream / Logo / ChannelGroup rows
used to play Twitch streams via streamlink + the auto-updated streamlink-ttvlol.

The plugin marks every object it owns with a custom_properties tag so it can
clean up safely on uninstall. Channels for logins that are no longer live (and
'show offline channels' is OFF) are pruned each cycle so the lineup tracks the
actual live state. A sentinel "no streams online" placeholder channel can
optionally be kept around so Emby/Jellyfin Live TV never collapses to an empty
section.
"""

from __future__ import annotations

import logging
import shlex
from typing import Iterable

from django.db import transaction

from . import ttvlol
from .epg import channel_tvg_id

logger = logging.getLogger(__name__)

PROFILE_NAME = "Twitcharr (ad-free, low-latency)"
OWNER_TAG = "twitcharr"
PLACEHOLDER_LOGIN = "_placeholder_"
PLACEHOLDER_TVG_ID = f"twitch.{PLACEHOLDER_LOGIN}"
PLACEHOLDER_NAME = "Twitch (no stream online)"


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
        "--twitch-disable-ads",
        "--twitch-proxy-playlist-fallback",
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


def _placeholder_entry(*, offline_icon_url: str = "") -> dict:
    return {
        "login": PLACEHOLDER_LOGIN,
        "display_name": PLACEHOLDER_NAME,
        "channel_name": PLACEHOLDER_NAME,
        "profile_image_url": "",
        "icon_url": offline_icon_url or "",
        "description": "Currently no streams online. Channels will appear automatically when streamers go live.",
        "live": False,
        "title": "⚫ No stream online",
        "game_name": "",
        "started_at": "",
        "viewer_count": 0,
    }


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
    keep_placeholder: bool = True,
    offline_icon_url: str = "",
    fast_startup: bool = True,
) -> dict:
    """Create / update Channel + Stream rows for every entry.

    Idempotent: re-running matches existing rows by Stream.url and Channel.tvg_id.
    Channels for logins outside the current entry list are pruned (so toggling
    'show offline' OFF actually removes those channels).

    `keep_placeholder=True` always keeps a sentinel "no streams online" channel
    in the lineup so Emby/Jellyfin Live TV never has zero channels.

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

    used_numbers: set[float] = set(Channel.objects.values_list("channel_number", flat=True))

    created_channels = 0
    updated_channels = 0
    created_streams = 0
    synced_logins: list[str] = []
    synced_tvg_ids: set[str] = set()

    # Decide whether to inject the placeholder. We add it whenever:
    #   * the user wants it AND
    #   * there are no live channels right now (real-life lineup might be empty).
    #
    # When the placeholder is active we also drop the offline entries so the
    # lineup shows ONLY the "no streams online" tile — otherwise users see
    # both the placeholder and one greyed-out tile per offline streamer.
    has_live = any(e.get("live") for e in entries)
    if keep_placeholder and not has_live:
        real_entries = [_placeholder_entry(offline_icon_url=offline_icon_url)]
    else:
        real_entries = list(entries)

    for idx, e in enumerate(real_entries):
        login = e["login"]
        tvg = channel_tvg_id(login)
        synced_tvg_ids.add(tvg)
        channel_name = e.get("channel_name") or e["display_name"]

        is_placeholder = login == PLACEHOLDER_LOGIN
        twitch_url = (
            "about:blank#twitch-epg-placeholder"
            if is_placeholder
            else f"https://twitch.tv/{login}"
        )
        logo = _logo_for(login, channel_name, e["icon_url"])

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
                "is_placeholder": is_placeholder,
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

        ch_defaults = {
            "name": channel_name,
            "channel_group": group,
            "tvg_id": tvg,
            "stream_profile": profile,
            "logo": logo,
        }
        channel = Channel.objects.filter(tvg_id=tvg).first()
        if channel:
            for k, v in ch_defaults.items():
                setattr(channel, k, v)
            channel.save()
            updated_channels += 1
        else:
            number = _next_channel_number(float(starting_channel_number) + idx, used_numbers)
            used_numbers.add(number)
            channel = Channel.objects.create(channel_number=number, **ch_defaults)
            created_channels += 1

        ChannelStream.objects.update_or_create(
            channel=channel, stream=stream, defaults={"order": 0}
        )

        # Link channel to its EPGData row right away (the EPG writer also does
        # this, but doing it here as well covers the case where sync runs
        # without an immediate EPG refresh).
        try:
            from apps.epg.models import EPGData

            epg = EPGData.objects.filter(tvg_id=tvg).first()
            if epg and channel.epg_data_id != epg.id:
                channel.epg_data = epg
                channel.save(update_fields=["epg_data"])
        except Exception:
            logger.exception("Could not link EPGData for %s", login)

        synced_logins.append(login)

    # Prune managed channels that aren't in this sync (offline streamers when
    # 'show offline' is OFF, or logins removed from the configured list).
    pruned_channels, pruned_streams = _prune_unmanaged(synced_tvg_ids)

    visible_channel_names = [l for l in synced_logins if l != PLACEHOLDER_LOGIN]
    return {
        "message": (
            "No Twitch streams are live; placeholder channel is active."
            if keep_placeholder and not has_live
            else f"Synced {len(visible_channel_names)} Twitch channels."
        ),
        "channels_created": created_channels,
        "channels_updated": updated_channels,
        "channels_pruned": pruned_channels,
        "streams_created": created_streams,
        "streams_pruned": pruned_streams,
        "placeholder_active": (keep_placeholder and not has_live),
        "channel_names": visible_channel_names,
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

    streams = Stream.objects.filter(custom_properties__owner=OWNER_TAG)
    stream_count = streams.count()

    channels = Channel.objects.filter(
        streams__custom_properties__owner=OWNER_TAG
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
