"""EPG generation: produces both an XMLTV file (twitch2tuner-compatible) and
direct rows in apps.epg.models.{EPGData,ProgramData} so Dispatcharr can match
guide data without a separate parsing step.

Channel id convention (used as tvg_id everywhere): "twitch.<login>".
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

EPG_SOURCE_NAME = "Twitch (managed by Twitch EPG plugin)"
TVG_ID_PREFIX = "twitch."
LIVE_PROGRAMME_HOURS = 24  # mirrors twitch2tuner: live now -> ends in 24h
OFFLINE_PROGRAMME_HOURS = 24


def channel_tvg_id(login: str) -> str:
    return f"{TVG_ID_PREFIX}{login.lower()}"


def xmltv_path(data_dir: str) -> str:
    return os.path.join(data_dir, "twitch.xmltv")


# ---------------------------------------------------------------------------
# EPGSource bookkeeping
# ---------------------------------------------------------------------------

def get_or_create_epg_source(data_dir: str):
    """Always returns an active EPGSource that points at our XMLTV file.

    We deliberately set source_type='xmltv' so the rest of Dispatcharr treats
    it like any other guide. The url field is left blank because EPG data is
    written directly via EPGData/ProgramData (skipping the parser); the
    file_path is still set so admins can inspect / re-parse the file by hand.
    """
    from apps.epg.models import EPGSource

    source, _ = EPGSource.objects.update_or_create(
        name=EPG_SOURCE_NAME,
        defaults={
            "source_type": "xmltv",
            "url": "",
            "file_path": xmltv_path(data_dir),
            "is_active": True,
            "refresh_interval": 0,  # we own the refresh schedule, not Dispatcharr
            "status": "success",
            "last_message": "Managed by Twitch EPG plugin",
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
    use_profile_pic_when_just_chatting: bool = True,
    include_offline: bool = True,
) -> list[dict]:
    """Returns a list of dicts, one per channel. Each dict has:
        login, display_name, profile_image_url, icon_url, description,
        live (bool), title, game_name, started_at, viewer_count.
    """
    users = client.get_users(logins)
    streams = client.get_streams(logins)

    game_ids = {s.game_id for s in streams.values() if s.game_id}
    games = client.get_games(game_ids) if game_ids else {}

    entries: list[dict] = []
    for login in logins:
        u = users.get(login)
        if not u:
            logger.warning("Twitch login not found: %s", login)
            continue

        s = streams.get(login)
        live = s is not None
        if not live and not include_offline:
            continue

        if live:
            game = games.get(s.game_id) if s.game_id else None
            game_name = (s.game_name or (game.name if game else "")).strip()
            if game_name.lower() == "just chatting" and use_profile_pic_when_just_chatting:
                icon_url = u.profile_image_url
            elif game and game.box_art_url:
                icon_url = tw.render_box_art(game.box_art_url)
            else:
                icon_url = u.profile_image_url
            title = f"• {game_name}" if game_name else f"• Live: {u.display_name}"
            description = s.title or ""
            started_at = s.started_at
            viewers = s.viewer_count
        else:
            icon_url = u.profile_image_url
            title = f"{u.display_name} (offline)"
            description = u.description or ""
            game_name = ""
            started_at = ""
            viewers = 0

        entries.append({
            "login": login,
            "display_name": u.display_name,
            "profile_image_url": u.profile_image_url,
            "icon_url": icon_url,
            "description": description,
            "live": live,
            "title": title,
            "game_name": game_name,
            "started_at": started_at,
            "viewer_count": viewers,
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
        "generator-info-name": "Dispatcharr Twitch EPG plugin",
        "generator-info-url": "https://github.com/eliasbruno124-dev/Dispatcharr-Twitch-EPG",
    })

    for e in entries:
        ch = ET.SubElement(tv, "channel", {"id": channel_tvg_id(e["login"])})
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
            start = now
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
        if e["icon_url"]:
            ET.SubElement(prog, "icon", {"src": e["icon_url"]})
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
# Direct DB upserts (so Dispatcharr's UI shows guide data without re-parsing)
# ---------------------------------------------------------------------------

@transaction.atomic
def upsert_db(entries: list[dict], data_dir: str) -> dict:
    from apps.epg.models import EPGData, ProgramData

    source = get_or_create_epg_source(data_dir)

    now = djtz.now()
    seen_tvg_ids: set[str] = set()
    epgdata_count = 0
    program_count = 0

    for e in entries:
        tvg = channel_tvg_id(e["login"])
        seen_tvg_ids.add(tvg)
        epg, _ = EPGData.objects.update_or_create(
            tvg_id=tvg,
            epg_source=source,
            defaults={
                "name": e["display_name"],
                "icon_url": e["icon_url"] or None,
            },
        )
        epgdata_count += 1

        # Replace programmes for this channel each refresh — guide is small (1
        # programme per channel) and we always want the freshest title/game.
        ProgramData.objects.filter(epg=epg).delete()

        if e["live"]:
            started = _parse_iso(e["started_at"]) or now
            end = now + timedelta(hours=LIVE_PROGRAMME_HOURS)
            if started.tzinfo is None:
                started = started.replace(tzinfo=timezone.utc)
        else:
            started = now
            end = now + timedelta(hours=OFFLINE_PROGRAMME_HOURS)

        ProgramData.objects.create(
            epg=epg,
            tvg_id=tvg,
            start_time=started,
            end_time=end,
            title=e["title"],
            sub_title=e["game_name"] or "",
            description=e["description"] or "",
            custom_properties={
                "twitch_login": e["login"],
                "twitch_live": e["live"],
                "twitch_viewers": e["viewer_count"],
            },
        )
        program_count += 1

    # Drop guide rows for channels that are no longer in the list
    stale = EPGData.objects.filter(epg_source=source).exclude(tvg_id__in=seen_tvg_ids)
    stale_count = stale.count()
    stale.delete()

    source.status = "success"
    source.last_message = (
        f"Refreshed {epgdata_count} channels, {program_count} programmes"
        + (f", removed {stale_count} stale" if stale_count else "")
    )
    source.updated_at = djtz.now()
    source.save(update_fields=["status", "last_message", "updated_at"])

    return {
        "channels": epgdata_count,
        "programmes": program_count,
        "removed_stale": stale_count,
    }
