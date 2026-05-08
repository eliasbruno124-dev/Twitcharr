"""Manages the StreamProfile / Channel / Stream / Logo / ChannelGroup rows
used to play Twitch streams via streamlink + the auto-updated streamlink-ttvlol.

The plugin marks every object it owns with a custom_properties tag so it can
clean up safely on uninstall.
"""

from __future__ import annotations

import logging
import shlex
from typing import Iterable

from django.db import transaction

from . import ttvlol
from .epg import channel_tvg_id

logger = logging.getLogger(__name__)

PROFILE_NAME = "Twitch (ttv.lol low-latency)"
OWNER_TAG = "dispatcharr_twitch_epg"


# ---------------------------------------------------------------------------
# Stream profile
# ---------------------------------------------------------------------------

def build_streamlink_parameters(
    *,
    plugin_dirs: str,
    proxy_servers: str,
    quality: str,
    low_latency: bool,
) -> str:
    """Return the value for StreamProfile.parameters.

    Streamlink command line:
        streamlink --loglevel warning --stdout --plugin-dir <dir> \
            --http-timeout 10 \
            --stream-segment-attempts 2 \
            --stream-segment-timeout 6 \
            --stream-timeout 20 \
            --twitch-disable-ads \
            --twitch-proxy-playlist=<servers> \
            --twitch-proxy-playlist-fallback \
            [--twitch-low-latency --hls-live-edge 2 --stream-segment-threads 3] \
            --http-header User-Agent={userAgent} \
            {streamUrl} <quality>

    Dispatcharr's StreamProfile.build_command shlex-splits this and substitutes
    {streamUrl} / {userAgent}. Each argument lives on its own logical token —
    we use shlex.quote where values can contain commas/colons.
    """
    parts: list[str] = [
        "--loglevel", "warning",
        "--stdout",
        "--plugin-dir", plugin_dirs,
        "--http-timeout", "10",
        "--stream-segment-attempts", "2",
        "--stream-segment-timeout", "6",
        "--stream-timeout", "20",
        "--twitch-disable-ads",
        "--twitch-proxy-playlist-fallback",
        "--http-header", "User-Agent={userAgent}",
        "--retry-streams", "1",
        "--retry-max", "2",
    ]
    if proxy_servers.strip():
        parts.extend(["--twitch-proxy-playlist", proxy_servers.strip()])
    if low_latency:
        parts.extend([
            "--twitch-low-latency",
            "--hls-live-edge", "2",
            "--stream-segment-threads", "3",
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
):
    from core.models import StreamProfile

    plugin_dirs = ttvlol.plugin_dir(data_dir)
    parameters = build_streamlink_parameters(
        plugin_dirs=plugin_dirs,
        proxy_servers=proxy_servers,
        quality=quality,
        low_latency=low_latency,
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
        # Fallback for unusual installs: create one if it isn't there.
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
) -> dict:
    """Create / update Channel + Stream rows for every entry.

    Idempotent: re-running matches existing rows by Stream.url and Channel.tvg_id.
    Returns counts plus a list of synced logins (for logging / UI).
    """
    from apps.channels.models import Channel, ChannelStream, Stream

    profile = get_or_create_stream_profile(
        data_dir=data_dir,
        proxy_servers=proxy_servers,
        quality=quality,
        low_latency=low_latency,
    )
    group = _channel_group(group_name)
    custom_account = _custom_m3u_account()

    # Pre-load currently used channel numbers so we don't collide
    used_numbers: set[float] = set(Channel.objects.values_list("channel_number", flat=True))

    created_channels = 0
    updated_channels = 0
    created_streams = 0
    synced_logins: list[str] = []

    for idx, e in enumerate(entries):
        login = e["login"]
        tvg = channel_tvg_id(login)
        twitch_url = f"https://twitch.tv/{login}"
        logo = _logo_for(login, e["display_name"], e["icon_url"])

        # Stream row. Linked to the built-in 'custom' M3UAccount so Channel.get_stream
        # has an active profile to iterate through (without that link Dispatcharr
        # returns 'No active profiles found' on playback).
        stream_defaults = {
            "name": e["display_name"],
            "url": twitch_url,
            "logo_url": e["icon_url"] or None,
            "tvg_id": tvg,
            "stream_profile": profile,
            "is_custom": True,
            "m3u_account": custom_account,
            "custom_properties": {"owner": OWNER_TAG, "twitch_login": login},
        }
        stream = Stream.objects.filter(url=twitch_url, is_custom=True).first()
        if stream:
            for k, v in stream_defaults.items():
                setattr(stream, k, v)
            stream.save()
        else:
            stream = Stream.objects.create(**stream_defaults)
            created_streams += 1

        # Channel row
        ch_defaults = {
            "name": e["display_name"],
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

        # M2M link Channel <-> Stream
        ChannelStream.objects.update_or_create(
            channel=channel, stream=stream, defaults={"order": 0}
        )

        # Link to existing EPGData row (created by epg.upsert_db)
        try:
            from apps.epg.models import EPGData

            epg = EPGData.objects.filter(tvg_id=tvg).first()
            if epg and channel.epg_data_id != epg.id:
                channel.epg_data = epg
                channel.save(update_fields=["epg_data"])
        except Exception:
            logger.exception("Could not link EPGData for %s", login)

        synced_logins.append(login)

    return {
        "channels_created": created_channels,
        "channels_updated": updated_channels,
        "streams_created": created_streams,
        "logins": synced_logins,
        "stream_profile_id": profile.id,
        "channel_group_id": group.id,
    }


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

    # Channels: tvg_id starts with our prefix
    channels = Channel.objects.filter(tvg_id__startswith="twitch.")
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
