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
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from django.db import transaction
from django.utils import timezone as djtz

from . import twitch_api as tw

logger = logging.getLogger(__name__)

EPG_SOURCE_NAME = "Twitch (managed by Twitcharr)"
TVG_ID_PREFIX = "twitch."
LIVE_PROGRAMME_HOURS = 24
OFFLINE_PROGRAMME_HOURS = 24
LIVE_PROGRAMME_START_BACKDATE_MINUTES = 1
OFFLINE_PROGRAMME_START_BACKDATE_MINUTES = 5
DESCRIPTION_SEPARATOR = "\n"
MAX_DB_URL_LENGTH = 500

# Twitch's CDN preview URL for any live channel. The CDN refreshes the JPEG
# every ~30s, but downstream caches (Dispatcharr → Emby → browser) hold the URL
# longer than that, so we cache-bust by appending a per-cycle timestamp.
LIVE_PREVIEW_URL = "https://static-cdn.jtvnw.net/previews-ttv/live_user_{login}-{w}x{h}.jpg"


def live_preview_url(login: str, *, width: int = 640, height: int = 360, cache_bust: int = 0) -> str:
    url = LIVE_PREVIEW_URL.format(login=login.lower(), w=width, h=height)
    if cache_bust:
        url = f"{url}?ts={cache_bust}"
    return url


def _cache_bust_image_url(url: str, cache_bust: int = 0) -> str:
    if not url or not cache_bust or url.startswith("data:"):
        return url
    parts = urlsplit(url)
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k != "twarr_ts"]
    query.append(("twarr_ts", str(int(cache_bust))))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def _db_safe_url(url: str | None) -> str | None:
    url = (url or "").strip()
    if not url:
        return None
    if len(url) > MAX_DB_URL_LENGTH:
        logger.warning("Skipping overlong image URL (%d chars, max %d)", len(url), MAX_DB_URL_LENGTH)
        return None
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


def decode_description_separator(value: str | None) -> str:
    """Decode the two useful escapes supported by the single-line setting."""
    raw = DESCRIPTION_SEPARATOR if value is None else str(value)
    return raw.replace(r"\n", "\n").replace(r"\t", "\t")


def _join_description(parts: list[str], separator: str = DESCRIPTION_SEPARATOR) -> str:
    cleaned = [part.strip() for part in parts if (part or "").strip()]
    return separator.join(cleaned)


def xmltv_path(data_dir: str) -> str:
    return os.path.join(data_dir, "twitch.xmltv")


def invalidate_dispatcharr_output_cache() -> dict[str, int | str]:
    """Drop Dispatcharr's streamed `/output/epg` chunk cache.

    Dispatcharr caches each generated XMLTV response in Redis for five
    minutes. Without invalidation, an immediate Emby/Jellyfin Refresh Guide
    can successfully complete while still importing the previous Twitcharr
    title, description, or live marker.
    """
    try:
        from django_redis import get_redis_connection

        redis = get_redis_connection("default")
        keys = list(redis.scan_iter(match="epg_content:*"))
        deleted = int(redis.delete(*keys)) if keys else 0
        return {"status": "ok", "keys_deleted": deleted}
    except Exception as exc:
        logger.exception("Could not invalidate Dispatcharr EPG output cache")
        return {"status": "error", "keys_deleted": 0, "message": str(exc)}


# ---------------------------------------------------------------------------
# EPGSource bookkeeping
# ---------------------------------------------------------------------------

def get_or_create_epg_source(data_dir: str):
    """Always returns an active EPGSource that points at our XMLTV file.

    Writes only when a field actually drifted. An unconditional
    `update_or_create` here re-saved schedule-relevant fields every cycle,
    which fires Dispatcharr's EPGSource post_save scheduling signal and
    rewrites the Celery beat tables every couple of minutes for nothing.
    """
    from apps.epg.models import EPGSource

    desired_path = xmltv_path(data_dir)
    source, created = EPGSource.objects.get_or_create(
        name=EPG_SOURCE_NAME,
        defaults={
            "source_type": "xmltv",
            "url": "",
            "file_path": desired_path,
            "is_active": True,
            "refresh_interval": 0,
            "status": "success",
            "last_message": "Managed by Twitcharr",
        },
    )
    if created:
        return source

    update_fields: list[str] = []
    if source.source_type != "xmltv":
        source.source_type = "xmltv"
        update_fields.append("source_type")
    if source.file_path != desired_path:
        source.file_path = desired_path
        update_fields.append("file_path")
    if not source.is_active:
        source.is_active = True
        update_fields.append("is_active")
    if source.refresh_interval != 0:
        source.refresh_interval = 0
        update_fields.append("refresh_interval")
    if update_fields:
        source.save(update_fields=update_fields)
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
    profiles_by_login: dict[str, list[str]] | None = None,
    channel_name_prefix: str = "",
    channel_name_suffix: str = "",
    channel_name_templates: dict[str, str] | None = None,
    live_indicator_mode: str = "xmltv",
    description_separator: str = DESCRIPTION_SEPARATOR,
    channel_logo_mode: str = "profile",
) -> list[dict]:
    """Returns a list of dicts, one per channel.

    `icon_url` is the channel/overview logo. `program_icon_url` is the current
    programme artwork, so TV clients can use 16:9 guide art without forcing
    that same image into portrait-style channel grids. `icon_url_stable` is
    the same artwork without the per-cycle cache-bust parameter — Logo and
    Stream rows must use it, otherwise every cycle mints a brand-new unique
    Logo URL and the logos table grows forever.
    """
    users = client.get_users(logins)
    streams = client.get_streams(logins)

    game_ids = {s.game_id for s in streams.values() if s.game_id}
    games = client.get_games(game_ids) if game_ids else {}

    offline_icon = (offline_icon_url or "").strip()
    offline_program_icon = (offline_program_icon_url or offline_icon_url or "").strip()
    separator = decode_description_separator(description_separator)
    indicator_mode = (live_indicator_mode or "xmltv").strip().lower()
    if indicator_mode not in {"xmltv", "emoji", "both", "none"}:
        indicator_mode = "xmltv"
    logo_mode = (channel_logo_mode or "profile").strip().lower()
    if logo_mode not in {"profile", "category"}:
        logo_mode = "profile"
    name_templates = {
        str(login).strip().lower(): str(template)
        for login, template in (channel_name_templates or {}).items()
        if str(login).strip() and "{name}" in str(template)
    }

    def channel_name_for(login: str, display_name: str) -> str:
        template = name_templates.get(login.lower())
        if template:
            return template.replace("{name}", display_name)
        return f"{channel_name_prefix}{display_name}{channel_name_suffix}"

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
            # Keep Twitch box art in its native portrait ratio (~272x380) —
            # forcing it into 640x360 stretches the artwork horizontally and
            # then portrait tiles (Emby "Läuft gerade") distort it a second
            # time when cropping back to portrait.
            if game and game.box_art_url:
                program_icon_url = tw.render_box_art(game.box_art_url)
            elif u.profile_image_url:
                program_icon_url = u.profile_image_url
            elif use_live_thumbnails:
                program_icon_url = live_preview_url(login, cache_bust=cache_bust)
            else:
                program_icon_url = ""

            # Channel logos default to the stable broadcaster avatar. Category
            # mode remains available for users who prefer dynamic artwork;
            # programme artwork always continues to represent the category.
            if logo_mode == "category" and game and game.box_art_url:
                icon_url = tw.render_box_art(game.box_art_url)
            elif u.profile_image_url:
                icon_url = u.profile_image_url
            elif game and game.box_art_url:
                icon_url = tw.render_box_art(game.box_art_url)
            elif program_icon_url:
                icon_url = program_icon_url
            else:
                icon_url = ""

            icon_url_stable = icon_url
            program_icon_url = _cache_bust_image_url(program_icon_url, cache_bust)
            icon_url = _cache_bust_image_url(icon_url, cache_bust)

            stream_title = (s.title or "").strip()
            uptime_str = _format_uptime(s.started_at, datetime.now(timezone.utc))
            viewer_label = _viewer_label(s.viewer_count)

            # Programme title: the at-a-glance line shown in the channel grid.
            display_title = (
                f"🔴 {u.display_name}"
                if indicator_mode in {"emoji", "both"}
                else u.display_name
            )
            title_parts = [display_title, game_name or "Live"]
            if viewer_label:
                title_parts.append(viewer_label)
            title = " • ".join(title_parts)
            channel_name = channel_name_for(login, u.display_name)

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
            description = _join_description(description_parts, separator)
            started_at = s.started_at
            viewers = s.viewer_count
        else:
            # Emby/Jellyfin render Twitch profile images reliably in Live TV
            # tiles. Custom offline SVG/PNG placeholders can lag or break in
            # this view, so offline channels use the streamer's profile image
            # while the programme title carries the offline state.
            icon_url_stable = u.profile_image_url or offline_icon
            icon_url = _cache_bust_image_url(icon_url_stable, cache_bust)
            program_icon_stable = u.profile_image_url or offline_program_icon
            program_icon_url = _cache_bust_image_url(program_icon_stable, cache_bust)
            game_name = ""
            title = "⚫ Offline"
            channel_name = channel_name_for(login, u.display_name)
            description_parts = [
                "Status: Offline",
                f"Link: https://twitch.tv/{login}",
            ]
            if (u.description or "").strip():
                description_parts.append(f"Bio: {(u.description or '').strip()}")
            description = _join_description(description_parts, separator)
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
            "icon_url_stable": icon_url_stable,
            "profiles": list((profiles_by_login or {}).get(login, [])),
            "program_icon_url": program_icon_url,
            "description": description,
            "live": live,
            "emit_live_tag": live and indicator_mode in {"xmltv", "both"},
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
        "generator-info-url": "https://github.com/eliasbruno124-dev/Twitcharr",
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
        start, end = _programme_window(e, now)

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
        if e.get("emit_live_tag"):
            ET.SubElement(prog, "live")
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

def _programme_window(e: dict, now: datetime) -> tuple[datetime, datetime]:
    if e["live"]:
        # Emby keys current guide items primarily by channel and start time.
        # Reusing Twitch's original stream start for hours makes Emby retain
        # an older title/description when a setting or stream metadata changes.
        # A rolling current-programme window gives every refresh a fresh guide
        # identity while the real Twitch uptime remains in the description.
        started = now - timedelta(minutes=LIVE_PROGRAMME_START_BACKDATE_MINUTES)
        end = now + timedelta(hours=LIVE_PROGRAMME_HOURS)
    else:
        started = now - timedelta(minutes=OFFLINE_PROGRAMME_START_BACKDATE_MINUTES)
        end = now + timedelta(hours=OFFLINE_PROGRAMME_HOURS)
    return started, end


def _program_for_entry(e: dict, epg, now: datetime):
    from apps.epg.models import ProgramData

    started, end = _programme_window(e, now)
    custom_properties = {
        "twitch_login": e["login"],
        "twitch_live": e["live"],
        "twitch_xmltv_live_tag": bool(e.get("emit_live_tag")),
        # Dispatcharr's XMLTV output writer emits <live /> from the standard
        # custom-properties key named "live".  Keep the Twitcharr-specific
        # key above for diagnostics, but also populate the key consumed by
        # /output/epg so Emby/Jellyfin receive the flag.
        "live": bool(e.get("emit_live_tag")),
        "twitch_viewers": e["viewer_count"],
        "twitch_display_name": e["display_name"],
        "twitch_game_name": e["game_name"],
        "twitch_stream_title": e.get("stream_title") or "",
        "twitch_url": e.get("twitch_url") or f"https://twitch.tv/{e['login']}",
    }
    if e.get("program_icon_url"):
        custom_properties["icon"] = e["program_icon_url"]

    return ProgramData(
        epg=epg,
        tvg_id=channel_tvg_id(e["login"]),
        start_time=started,
        end_time=end,
        title=e["title"],
        sub_title="",
        description=e["description"] or "",
        custom_properties=custom_properties,
    )


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
                "icon_url": _db_safe_url(e.get("icon_url")),
            },
        )
        epg_rows[tvg] = epg

    # Wipe and rebuild programmes in bulk — guide is small (1 programme per
    # channel) and we always want the freshest title/game/viewer count.
    if epg_rows:
        ProgramData.objects.filter(epg__in=epg_rows.values()).delete()

    new_programs: list[ProgramData] = []
    for e in entries:
        epg = epg_rows.get(channel_tvg_id(e["login"]))
        if not epg:
            continue
        new_programs.append(_program_for_entry(e, epg, now))

    if new_programs:
        ProgramData.objects.bulk_create(new_programs, batch_size=500)

    # Link existing Channels to their EPGData (idempotent / safe to repeat).
    linked_channels = 0
    for tvg, epg in epg_rows.items():
        updated = (
            Channel.objects.filter(
                tvg_id=tvg,
                streams__custom_properties__owner="twitcharr",
            )
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


@transaction.atomic
def link_channels_to_epg(entries: list[dict], data_dir: str) -> dict:
    """Immediately attach freshly-created Channel rows to Twitcharr EPGData."""
    from apps.channels.models import Channel
    from apps.epg.models import EPGData

    source = get_or_create_epg_source(data_dir)
    tvg_ids = [channel_tvg_id(e["login"]) for e in entries]
    if not tvg_ids:
        return {"linked_channels": 0, "checked_channels": 0}

    epg_rows = {
        row.tvg_id: row
        for row in EPGData.objects.filter(epg_source=source, tvg_id__in=tvg_ids)
    }
    linked_channels = 0
    for tvg, epg in epg_rows.items():
        linked_channels += (
            Channel.objects.filter(
                tvg_id=tvg,
                streams__custom_properties__owner="twitcharr",
            )
            .exclude(epg_data_id=epg.id)
            .update(epg_data=epg)
        )

    if linked_channels:
        source.last_message = f"Linked {linked_channels} channels to fresh Twitcharr guide data"
        source.updated_at = djtz.now()
        source.save(update_fields=["last_message", "updated_at"])

    return {
        "linked_channels": linked_channels,
        "checked_channels": len(tvg_ids),
    }


@transaction.atomic
def ensure_programs(entries: list[dict], data_dir: str) -> dict:
    """Self-heal: recreate programme rows that vanished during channel sync.

    Creating a Channel with EPG data makes Dispatcharr dispatch its
    `parse_programs_for_tvg_id` Celery task, which *deletes* every programme
    for that tvg_id before re-parsing the XMLTV file. If that task loses a
    race (file mid-rewrite, lock contention, worker error), the new channel
    sits with an empty guide until the next refresh cycle. This check runs
    after channel sync and instantly rebuilds any emptied guide from the
    entries already in memory.
    """
    from apps.epg.models import EPGData, ProgramData
    from django.db.models import Count

    by_tvg = {channel_tvg_id(e["login"]): e for e in entries}
    if not by_tvg:
        return {"programs_healed": 0}

    source = get_or_create_epg_source(data_dir)
    emptied = (
        EPGData.objects.filter(epg_source=source, tvg_id__in=list(by_tvg))
        .annotate(program_count=Count("programs"))
        .filter(program_count=0)
    )

    now = djtz.now()
    rows = [
        _program_for_entry(by_tvg[epg.tvg_id], epg, now)
        for epg in emptied
        if epg.tvg_id in by_tvg
    ]
    if rows:
        ProgramData.objects.bulk_create(rows)
        logger.info("Healed %d channels whose guide rows were wiped mid-sync", len(rows))
    return {"programs_healed": len(rows)}
