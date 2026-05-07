"""No-login Twitch metadata client for the Dispatcharr Twitch EPG plugin.

The plugin deliberately does not ask the user for Twitch credentials. It uses
the same public Twitch web GraphQL endpoint/client id pattern that Streamlink's
Twitch plugin uses for anonymous channel metadata lookups.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Iterable, Sequence

import requests

logger = logging.getLogger(__name__)

GQL_URL = "https://gql.twitch.tv/gql"
PUBLIC_CLIENT_ID = "kimne78kx3ncx6brgo4mv6wki5h1ko"
GQL_BATCH = 25
DEFAULT_TIMEOUT = 15

CHANNEL_QUERY = """
query DispatcharrTwitchEPG($login: String!) {
  user(login: $login) {
    id
    login
    displayName
    description
    profileImageURL(width: 300)
    stream {
      id
      title
      createdAt
      viewersCount
      type
      previewImageURL(width: 640, height: 360)
      game {
        id
        name
        boxArtURL(width: 272, height: 380)
      }
    }
  }
}
"""


@dataclass
class TwitchUser:
    id: str
    login: str
    display_name: str
    description: str = ""
    profile_image_url: str = ""


@dataclass
class TwitchStream:
    user_id: str
    user_login: str
    user_name: str
    title: str = ""
    game_id: str = ""
    game_name: str = ""
    started_at: str = ""
    viewer_count: int = 0
    thumbnail_url: str = ""
    is_live: bool = True


@dataclass
class TwitchGame:
    id: str
    name: str
    box_art_url: str = ""


class TwitchClient:
    """Small anonymous GraphQL client.

    The public methods mirror the previous Helix wrapper so the EPG builder can
    stay simple: `get_users()`, `get_streams()`, and `get_games()`.
    """

    def __init__(self, *, session: requests.Session | None = None):
        self._session = session or requests.Session()
        self._users: dict[str, TwitchUser] = {}
        self._streams: dict[str, TwitchStream] = {}
        self._games: dict[str, TwitchGame] = {}
        self._fetched_logins: set[str] = set()

    def _headers(self) -> dict:
        return {
            "Client-ID": PUBLIC_CLIENT_ID,
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "Mozilla/5.0 Dispatcharr-Twitch-EPG",
        }

    def _post_gql(self, payload: list[dict]) -> list[dict]:
        for attempt in range(3):
            resp = self._session.post(
                GQL_URL,
                headers=self._headers(),
                json=payload,
                timeout=DEFAULT_TIMEOUT,
            )
            if resp.status_code == 429:
                wait = 2.0 * (attempt + 1)
                logger.warning("Twitch GraphQL rate-limit hit, sleeping %.1fs", wait)
                time.sleep(wait)
                continue
            if resp.status_code >= 500 and attempt < 2:
                time.sleep(1.0 * (attempt + 1))
                continue
            if resp.status_code != 200:
                raise RuntimeError(
                    f"Twitch GraphQL request failed ({resp.status_code}): {resp.text[:200]}"
                )
            data = resp.json()
            if isinstance(data, dict):
                data = [data]
            if not isinstance(data, list):
                raise RuntimeError("Twitch GraphQL returned an unexpected payload")
            return data
        raise RuntimeError("Twitch GraphQL request failed after retries")

    def _fetch_channels(self, logins: Sequence[str]) -> None:
        clean = [l.strip().lower() for l in logins if l and l.strip()]
        missing = [login for login in clean if login not in self._fetched_logins]
        for batch in _chunks(missing, GQL_BATCH):
            payload = [
                {
                    "operationName": "DispatcharrTwitchEPG",
                    "variables": {"login": login},
                    "query": CHANNEL_QUERY,
                }
                for login in batch
            ]
            responses = self._post_gql(payload)
            for login, item in zip(batch, responses):
                self._fetched_logins.add(login)
                errors = item.get("errors") or []
                if errors:
                    logger.warning("Twitch GraphQL warning for %s: %s", login, errors[:1])
                    continue
                user = ((item.get("data") or {}).get("user") or None)
                if not user:
                    logger.warning("Twitch login not found: %s", login)
                    continue

                normalized_login = (user.get("login") or login).lower()
                twitch_user = TwitchUser(
                    id=str(user.get("id") or ""),
                    login=normalized_login,
                    display_name=user.get("displayName") or normalized_login,
                    description=user.get("description") or "",
                    profile_image_url=user.get("profileImageURL") or "",
                )
                self._users[normalized_login] = twitch_user

                stream = user.get("stream")
                if stream:
                    game = stream.get("game") or {}
                    game_id = str(game.get("id") or "")
                    if game_id:
                        self._games[game_id] = TwitchGame(
                            id=game_id,
                            name=game.get("name") or "",
                            box_art_url=game.get("boxArtURL") or "",
                        )
                    self._streams[normalized_login] = TwitchStream(
                        user_id=twitch_user.id,
                        user_login=normalized_login,
                        user_name=twitch_user.display_name,
                        title=stream.get("title") or "",
                        game_id=game_id,
                        game_name=game.get("name") or "",
                        started_at=stream.get("createdAt") or "",
                        viewer_count=int(stream.get("viewersCount") or 0),
                        thumbnail_url=stream.get("previewImageURL") or "",
                        is_live=True,
                    )

    def get_users(self, logins: Sequence[str]) -> dict[str, TwitchUser]:
        self._fetch_channels(logins)
        return {login: self._users[login] for login in _lower_existing(logins, self._users)}

    def get_streams(self, user_logins: Sequence[str]) -> dict[str, TwitchStream]:
        self._fetch_channels(user_logins)
        return {login: self._streams[login] for login in _lower_existing(user_logins, self._streams)}

    def get_games(self, ids: Iterable[str]) -> dict[str, TwitchGame]:
        return {str(gid): self._games[str(gid)] for gid in ids if str(gid) in self._games}


def _chunks(seq: Sequence[str], size: int):
    for i in range(0, len(seq), size):
        yield seq[i : i + size]


def _lower_existing(logins: Iterable[str], values: dict) -> list[str]:
    return [login.strip().lower() for login in logins if login and login.strip().lower() in values]


def parse_login_list(raw: str) -> list[str]:
    """Accepts comma- and/or newline-separated logins. Strips whitespace, dedupes,
    preserves first-occurrence order, lowercases.

    Allows users to paste a full URL like https://twitch.tv/gronkh — the path's
    last segment is used as the login.
    """
    if not raw:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for token in raw.replace("\r", "\n").replace(",", "\n").split("\n"):
        t = token.strip()
        if not t:
            continue
        if "/" in t:
            t = t.rstrip("/").rsplit("/", 1)[-1]
        t = t.lower()
        if t in seen or not t.replace("_", "").isalnum():
            continue
        seen.add(t)
        out.append(t)
    return out


def render_box_art(url: str, width: int = 272, height: int = 380) -> str:
    if not url:
        return ""
    return url.replace("{width}", str(width)).replace("{height}", str(height))
