"""EPG generation: produces both an XMLTV file (twitch2tuner-compatible) and
direct rows in apps.epg.models.{EPGData,ProgramData} so Dispatcharr can match
guide data without a separate parsing step.

Channel id convention (used as tvg_id everywhere): "twitch.<login>".

The DB writer also links any pre-existing `apps.channels.models.Channel` rows
(matched by tvg_id) to the freshly-created `EPGData`, so the guide is visible
on the very first refresh — no second cycle required.
"""

from __future__ import annotations

import logging
import os
import tempfile
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from typing import Iterable

from django.db import transaction
from django.utils import timezone as djtz

from . import twitch_api as tw

logger = logging.getLogger(__name__)

EPG_SOURCE_NAME = "Twitch (managed by Twitcharr)"
TVG_ID_PREFIX = "twitch."
LIVE_PROGRAMME_HOURS = 24
OFFLINE_PROGRAMME_HOURS = 24
OFFLINE_PROGRAMME_START_BACKDATE_MINUTES = 5
DESCRIPTION_SEPARATOR = "<br />"

# Twitch's CDN preview URL for any live channel. The CDN refreshes the JPEG
# every ~30s, but downstream caches (Dispatcharr → Emby → browser) hold the URL
# longer than that, so we cache-bust by appending a per-cycle timestamp.
LIVE_PREVIEW_URL = "https://static-cdn.jtvnw.net/previews-ttv/live_user_{login}-{w}x{h}.jpg"


def live_preview_url(login: str, *, width: int = 640, height: int = 360, cache_bust: int = 0) -> str:
    url = LIVE_PREVIEW_URL.format(login=login.lower(), w=width, h=height)
    if cache_bust:
        url = f"{url}?ts={cache_bust}"
    return url


def _viewer_label(viewer_count: int) -> str:
    if not viewer_count:
        return ""
    return f"{viewer_count:,} viewers"


def _format_uptime(started_iso: str, now: datetime) -> str:
    if not started_iso:
        return ""
    parsed = _parse_iso(started_iso)
    if parsed is None:
        return ""
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    delta = now - parsed
    seconds = max(0, int(delta.total_seconds()))
    hours, remainder = divmod(seconds, 3600)
    minutes, _ = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes:02d}min"
    return f"{minutes}min"


def channel_tvg_id(login: str) -> str:
    return f"{TVG_ID_PREFIX}{login.lower()}"


def _join_description(parts: list[str]) -> str:
    cleaned = [part.strip() for part in parts if (part or "").strip()]
    return DESCRIPTION_SEPARATOR.join(cleaned)


def xmltv_path(data_dir: str) -> str:
    return os.path.join(data_dir, "twitch.xmltv")


# ---------------------------------------------------------------------------
# EPGSource bookkeeping
# ---------------------------------------------------------------------------

def get_or_create_epg_source(data_dir: str):
    """Always returns an active EPGSource that points at our XMLTV file."""
    from apps.epg.models import EPGSource

    source, _ = EPGSource.objects.update_or_create(
        name=EPG_SOURCE_NAME,
        defaults={
            "source_type": "xmltv",
            "url": "",
            "file_path": xmltv_path(data_dir),
            "is_active": True,
            "refresh_interval": 0,
            "status": "success",
            "last_message": "Managed by Twitcharr",
        },
    )
    return source


# ---------------------------------------------------------------------------
# Data assembly
# ---------------------------------------------------------------------------

def build_entries(
    client: tw.TwitchClient,
    logins: list[str],
    *,
    include_offline: bool = True,
    offline_icon_url: str = "",
    offline_program_icon_url: str = "",
    use_live_thumbnails: bool = False,
    cache_bust: int = 0,
) -> list[dict]:
    """Returns a list of dicts, one per channel.

    `icon_url` is the channel/overview logo. `program_icon_url` is the current
    programme artwork, so TV clients can use 16:9 guide art without forcing
    that same image into portrait-style channel grids.
    """
    users = client.get_users(logins)
    streams = client.get_streams(logins)

    game_ids = {s.game_id for s in streams.values() if s.game_id}
    games = client.get_games(game_ids) if game_ids else {}

    offline_icon = (offline_icon_url or "").strip()
    offline_program_icon = (offline_program_icon_url or offline_icon_url or "").strip()

    entries: list[dict] = []
    for login in logins:
        u = users.get(login)
        if not u:
            logger.warning("Twitch channel not found: %s", login)
            continue

        s = streams.get(login)
        live = s is not None
        if not live and not include_offline:
            continue

        if live:
            game = games.get(s.game_id) if s.game_id else None
            game_name = (s.game_name or (game.name if game else "")).strip()

            # Programme artwork should be stable category art, not the live
            # preview frame. This covers games as well as Twitch categories
            # like Just Chatting / IRL when Twitch exposes box art for them.
            if game and game.box_art_url:
                program_icon_url = tw.render_box_art(game.box_art_url, width=640, height=360)
            elif u.profile_image_url:
                program_icon_url = u.profile_image_url
            elif use_live_thumbnails:
                program_icon_url = live_preview_url(login, cache_bust=cache_bust)
            else:
                program_icon_url = ""

            # Channel/overview artwork follows the current category/game too.
            # Profile pictures are only a fallback when Twitch has no box art.
            if game and game.box_art_url:
                icon_url = tw.render_box_art(game.box_art_url)
            elif program_icon_url:
                icon_url = program_icon_url
            elif u.profile_image_url:
                icon_url = u.profile_image_url
            else:
                icon_url = ""

            stream_title = (s.title or "").strip()
            uptime_str = _format_uptime(s.started_at, datetime.now(timezone.utc))
            viewer_label = _viewer_label(s.viewer_count)

            # Programme title: the at-a-glance line shown in the channel grid.
            title_parts = [f"🔴 {u.display_name}", game_name or "Live"]
            if viewer_label:
                title_parts.append(viewer_label)
            title = " • ".join(title_parts)
            channel_name = u.display_name

            # Keep each metadata item on its own line so XMLTV/Dispatcharr
            # detail views do not collapse status, link and bio into a blob.
            description_parts: list[str] = [
                "Status: Online",
                f"Link: https://twitch.tv/{login}",
            ]
            if stream_title:
                description_parts.append(f"Title: {stream_title}")
            if game_name:
                description_parts.append(f"Category: {game_name}")
            if viewer_label:
                description_parts.append(f"Viewers: {viewer_label}")
            if uptime_str:
                description_parts.append(f"Live: {uptime_str}")
            if (u.description or "").strip():
                description_parts.append(f"Bio: {(u.description or '').strip()}")
            description = _join_description(description_parts)
            started_at = s.started_at
            viewers = s.viewer_count
        else:
            # Use the bundled plugin assets directly for offline artwork.
            icon_url = offline_icon
            program_icon_url = offline_program_icon or icon_url
            game_name = ""
            title = f"⚫ {u.display_name} (offline)"
            channel_name = f"{u.display_name} (offline)"
            description_parts = [
                "Status: Offline",
                f"Link: https://twitch.tv/{login}",
            ]
            if (u.description or "").strip():
                description_parts.append(f"Bio: {(u.description or '').strip()}")
            description = _join_description(description_parts)
            stream_title = ""
            viewer_label = ""
            started_at = ""
            viewers = 0

        entries.append({
            "login": login,
            "display_name": u.display_name,
            "channel_name": channel_name,
            "profile_image_url": u.profile_image_url,
            "icon_url": icon_url,
            "program_icon_url": program_icon_url,
            "description": description,
            "live": live,
            "title": title,
            "stream_title": stream_title,
            "game_name": game_name,
            "started_at": started_at,
            "viewer_count": viewers,
            "viewer_label": viewer_label,
            "twitch_url": f"https://twitch.tv/{login}",
        })
    return entries


# ---------------------------------------------------------------------------
# XMLTV writer (matches twitch2tuner schema closely)
# ---------------------------------------------------------------------------

def _parse_iso(ts: str) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def _xmltv_time(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y%m%d%H%M%S +0000")


def write_xmltv(entries: list[dict], path: str) -> tuple[int, int]:
    """Write an XMLTV document. Returns (channel_count, programme_count)."""
    now = datetime.now(timezone.utc)
    tv = ET.Element("tv", {
        "generator-info-name": "Twitcharr",
        "generator-info-url": "https://github.com/eliasbruno124-dev/Dispatcharr-Twitch-EPG",
    })

    for e in entries:
        ch = ET.SubElement(tv, "channel", {"id": channel_tvg_id(e["login"])})
        channel_name = e.get("channel_name") or e["display_name"]
        ET.SubElement(ch, "display-name").text = channel_name
        if channel_name != e["display_name"]:
            ET.SubElement(ch, "display-name").text = e["display_name"]
        ET.SubElement(ch, "display-name").text = e["login"]
        if e["icon_url"]:
            ET.SubElement(ch, "icon", {"src": e["icon_url"]})
        ET.SubElement(ch, "url").text = f"https://twitch.tv/{e['login']}"

    programme_count = 0
    for e in entries:
        if e["live"]:
            start = _parse_iso(e["started_at"]) or now
            end = now + timedelta(hours=LIVE_PROGRAMME_HOURS)
        else:
            start = now - timedelta(minutes=OFFLINE_PROGRAMME_START_BACKDATE_MINUTES)
            end = now + timedelta(hours=OFFLINE_PROGRAMME_HOURS)

        prog = ET.SubElement(tv, "programme", {
            "start": _xmltv_time(start),
            "stop": _xmltv_time(end),
            "channel": channel_tvg_id(e["login"]),
        })
        ET.SubElement(prog, "title", {"lang": "en"}).text = e["title"]
        if e["description"]:
            ET.SubElement(prog, "desc", {"lang": "en"}).text = e["description"]
        if e["game_name"]:
            ET.SubElement(prog, "category", {"lang": "en"}).text = e["game_name"]
        if e.get("live"):
            ET.SubElement(prog, "category", {"lang": "en"}).text = "Live"
        if e.get("twitch_url"):
            ET.SubElement(prog, "url").text = e["twitch_url"]
        program_icon_url = e.get("program_icon_url") or ""
        if program_icon_url:
            ET.SubElement(prog, "icon", {"src": program_icon_url})
        programme_count += 1

    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix="twitch.xmltv.", dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(b'<?xml version="1.0" encoding="UTF-8"?>\n')
            tree = ET.ElementTree(tv)
            tree.write(f, encoding="utf-8", xml_declaration=False)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise

    return len(entries), programme_count


# ---------------------------------------------------------------------------
# Direct DB upserts
# ---------------------------------------------------------------------------

@transaction.atomic
def upsert_db(entries: list[dict], data_dir: str) -> dict:
    """Write EPGData + ProgramData rows and link any pre-existing Channel.

    The Channel link step is what makes the guide appear instantly on the
    very first refresh: without it, channels created earlier in the same
    cycle have a NULL `epg_data_id` and the UI shows 'No guide'.
    """
    from apps.channels.models import Channel
    from apps.epg.models import EPGData, ProgramData

    source = get_or_create_epg_source(data_dir)
    now = djtz.now()
    seen_tvg_ids: set[str] = set()

    epg_rows: dict[str, EPGData] = {}
    for e in entries:
        tvg = channel_tvg_id(e["login"])
        seen_tvg_ids.add(tvg)
        epg, _ = EPGData.objects.update_or_create(
            tvg_id=tvg,
            epg_source=source,
            defaults={
                "name": e.get("channel_name") or e["display_name"],
                "icon_url": e["icon_url"] or None,
            },
        )
        epg_rows[tvg] = epg

    # Wipe and rebuild programmes in bulk — guide is small (1 programme per
    # channel) and we always want the freshest title/game/viewer count.
    if epg_rows:
        ProgramData.objects.filter(epg__in=epg_rows.values()).delete()

    new_programs: list[ProgramData] = []
    for e in entries:
        tvg = channel_tvg_id(e["login"])
        epg = epg_rows.get(tvg)
        if not epg:
            continue

        if e["live"]:
            started = _parse_iso(e["started_at"]) or now
            end = now + timedelta(hours=LIVE_PROGRAMME_HOURS)
            if started.tzinfo is None:
                started = started.replace(tzinfo=timezone.utc)
        else:
            started = now - timedelta(minutes=OFFLINE_PROGRAMME_START_BACKDATE_MINUTES)
            end = now + timedelta(hours=OFFLINE_PROGRAMME_HOURS)

        custom_properties = {
            "twitch_login": e["login"],
            "twitch_live": e["live"],
            "twitch_viewers": e["viewer_count"],
            "twitch_display_name": e["display_name"],
            "twitch_game_name": e["game_name"],
            "twitch_stream_title": e.get("stream_title") or "",
            "twitch_url": e.get("twitch_url") or f"https://twitch.tv/{e['login']}",
        }
        if e.get("program_icon_url"):
            custom_properties["icon"] = e["program_icon_url"]

        new_programs.append(ProgramData(
            epg=epg,
            tvg_id=tvg,
            start_time=started,
            end_time=end,
            title=e["title"],
            sub_title="",
            description=e["description"] or "",
            custom_properties=custom_properties,
        ))

    if new_programs:
        ProgramData.objects.bulk_create(new_programs, batch_size=500)

    # Link existing Channels to their EPGData (idempotent / safe to repeat).
    linked_channels = 0
    for tvg, epg in epg_rows.items():
        updated = (
            Channel.objects.filter(tvg_id=tvg)
            .exclude(epg_data_id=epg.id)
            .update(epg_data=epg)
        )
        linked_channels += updated

    # Drop guide rows for channels that are no longer in the list
    stale = EPGData.objects.filter(epg_source=source).exclude(tvg_id__in=seen_tvg_ids)
    stale_count = stale.count()
    stale.delete()

    source.status = "success"
    source.last_message = (
        f"Refreshed {len(epg_rows)} channels, {len(new_programs)} programmes"
        + (f", linked {linked_channels} channels" if linked_channels else "")
        + (f", removed {stale_count} stale" if stale_count else "")
    )
    source.updated_at = djtz.now()
    source.save(update_fields=["status", "last_message", "updated_at"])

    return {
        "channels": len(epg_rows),
        "programmes": len(new_programs),
        "linked_channels": linked_channels,
        "removed_stale": stale_count,
    }
