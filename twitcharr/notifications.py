"""Discord webhook notifications for go-live transitions.

Posts a rich embed to a Discord channel when a configured Twitch user goes
live (offline → live edge). The webhook URL is the only configuration —
Discord handles delivery, retries, and rate-limiting itself.
"""

from __future__ import annotations

import logging
from typing import Any

import requests

logger = logging.getLogger(__name__)

TWITCH_PURPLE = 0x9146FF
DEFAULT_TIMEOUT = 10


def _preview_image(login: str) -> str:
    return f"https://static-cdn.jtvnw.net/previews-ttv/live_user_{login}-1280x720.jpg"


def go_live_embed(entry: dict) -> dict[str, Any]:
    """Build the Discord embed payload for a single go-live event."""
    login = entry["login"]
    display_name = entry.get("display_name") or login
    game_name = (entry.get("game_name") or "").strip()
    viewers = int(entry.get("viewer_count") or 0)
    title_text = ""

    # entry["description"] starts with the actual stream title (newline-separated
    # from the supplemental "Playing: ..." / viewers lines added in epg.build_entries).
    desc = (entry.get("description") or "").strip()
    if desc:
        title_text = desc.split("\n", 1)[0].strip()

    embed: dict[str, Any] = {
        "title": f"🔴 {display_name} is now live!",
        "url": f"https://twitch.tv/{login}",
        "color": TWITCH_PURPLE,
        "timestamp": entry.get("started_at") or None,
        "footer": {"text": "Twitcharr"},
        "fields": [],
    }
    if title_text:
        embed["description"] = title_text[:400]
    if game_name:
        embed["fields"].append({"name": "Playing", "value": game_name, "inline": True})
    if viewers:
        embed["fields"].append({
            "name": "Viewers",
            "value": f"{viewers:,}",
            "inline": True,
        })
    if entry.get("profile_image_url"):
        embed["thumbnail"] = {"url": entry["profile_image_url"]}

    embed["image"] = {"url": _preview_image(login)}
    return embed


def post_go_live(webhook_url: str, entries: list[dict]) -> dict:
    """POST a Discord embed for every entry in `entries`. Up to 10 per request
    (Discord's hard limit); the function batches automatically.
    """
    if not webhook_url or not entries:
        return {"status": "skipped", "posted": 0}

    posted = 0
    errors: list[str] = []
    for chunk_start in range(0, len(entries), 10):
        chunk = entries[chunk_start : chunk_start + 10]
        embeds = [go_live_embed(e) for e in chunk]
        try:
            resp = requests.post(
                webhook_url,
                json={"embeds": embeds, "username": "Twitch", "content": None},
                timeout=DEFAULT_TIMEOUT,
            )
            if resp.status_code in (200, 204):
                posted += len(embeds)
            else:
                errors.append(f"HTTP {resp.status_code}: {resp.text[:160]}")
        except Exception as exc:
            errors.append(str(exc))

    result: dict[str, Any] = {"status": "ok" if posted else "error", "posted": posted}
    if errors:
        result["errors"] = errors
    return result
