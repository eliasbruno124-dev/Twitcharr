"""No-login Twitch metadata client for the Twitcharr plugin.

Uses the public Twitch web GraphQL endpoint (the same one Streamlink's Twitch
plugin uses for anonymous metadata lookups). No Client ID, no OAuth.

Provides three layers:
  * `TwitchClient.get_users / get_streams / get_games` — channel metadata used
    by the EPG builder.
  * `discover_logins(spec)` — turns user-friendly tokens like
    `game:Just Chatting:10` or `top:de:25` into a deduplicated list of logins.
  * `parse_login_list(text)` — accepts plain logins, full URLs, and discovery
    tokens mixed together in one textarea.
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
query Twitcharr($login: String!) {
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

GAME_STREAMS_QUERY = """
query TwitcharrGameStreams($name: String!, $limit: Int!) {
  game(name: $name) {
    streams(first: $limit, options: {sort: VIEWER_COUNT}) {
      edges {
        node {
          broadcaster { login }
          viewersCount
          title
        }
      }
    }
  }
}
"""

TOP_STREAMS_QUERY = """
query TwitcharrTopStreams($limit: Int!, $languages: [Language!]) {
  streams(first: $limit, options: {sort: VIEWER_COUNT, languages: $languages}) {
    edges {
      node {
        broadcaster { login }
        viewersCount
        title
      }
    }
  }
}
"""

SEARCH_CHANNELS_QUERY = """
query TwitcharrSearchChannels($query: String!, $limit: Int!) {
  searchFor(userQuery: $query, platform: "web", target: {limit: $limit}) {
    channels {
      edges {
        item {
          login
          displayName
          followers { totalCount }
        }
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
            "User-Agent": "Mozilla/5.0 Twitcharr",
        }

    def _post_gql(self, payload) -> list[dict]:
        single = isinstance(payload, dict)
        body = [payload] if single else payload
        for attempt in range(3):
            resp = self._session.post(
                GQL_URL,
                headers=self._headers(),
                json=body,
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

    # ------------------------------------------------------------------ users
    def _fetch_channels(self, logins: Sequence[str]) -> None:
        clean = [l.strip().lower() for l in logins if l and l.strip()]
        missing = [login for login in clean if login not in self._fetched_logins]
        for batch in _chunks(missing, GQL_BATCH):
            payload = [
                {
                    "operationName": "Twitcharr",
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

    # ------------------------------------------------------------------ discovery
    def streams_in_game(self, name: str, *, limit: int = 10) -> list[str]:
        """Return logins of the top-`limit` live streams in a Twitch category."""
        if not name:
            return []
        try:
            data = self._post_gql({
                "operationName": "TwitcharrGameStreams",
                "variables": {"name": name, "limit": int(max(1, min(100, limit)))},
                "query": GAME_STREAMS_QUERY,
            })
        except Exception:
            logger.exception("Twitch game-streams discovery failed for %r", name)
            return []
        edges = (((data or [{}])[0].get("data") or {}).get("game") or {}).get("streams") or {}
        out: list[str] = []
        for edge in edges.get("edges", []) or []:
            login = (((edge or {}).get("node") or {}).get("broadcaster") or {}).get("login")
            if login:
                out.append(str(login).lower())
        return out

    def top_streams(self, *, languages: Sequence[str] = (), limit: int = 25) -> list[str]:
        """Top live streams globally, optionally filtered by language code (de/en/...)."""
        try:
            variables: dict = {"limit": int(max(1, min(100, limit)))}
            if languages:
                variables["languages"] = [l.upper() for l in languages if l]
            else:
                variables["languages"] = None
            data = self._post_gql({
                "operationName": "TwitcharrTopStreams",
                "variables": variables,
                "query": TOP_STREAMS_QUERY,
            })
        except Exception:
            logger.exception("Twitch top-streams discovery failed (lang=%r)", languages)
            return []
        edges = (((data or [{}])[0].get("data") or {}).get("streams") or {}).get("edges") or []
        out: list[str] = []
        for edge in edges:
            login = (((edge or {}).get("node") or {}).get("broadcaster") or {}).get("login")
            if login:
                out.append(str(login).lower())
        return out

    def search_channels(self, query: str, *, limit: int = 10) -> list[str]:
        """Smooth-typing search: maps a free-text query to the best matching logins."""
        if not query:
            return []
        try:
            data = self._post_gql({
                "operationName": "TwitcharrSearchChannels",
                "variables": {"query": query, "limit": int(max(1, min(50, limit)))},
                "query": SEARCH_CHANNELS_QUERY,
            })
        except Exception:
            logger.exception("Twitch channel search failed for %r", query)
            return []
        channels = (((data or [{}])[0].get("data") or {}).get("searchFor") or {}).get("channels") or {}
        out: list[str] = []
        for edge in channels.get("edges", []) or []:
            login = ((edge or {}).get("item") or {}).get("login")
            if login:
                out.append(str(login).lower())
        return out


# ---------------------------------------------------------------------------
# Free-text input parsing
# ---------------------------------------------------------------------------

def _chunks(seq: Sequence[str], size: int):
    for i in range(0, len(seq), size):
        yield seq[i : i + size]


def _lower_existing(logins: Iterable[str], values: dict) -> list[str]:
    return [login.strip().lower() for login in logins if login and login.strip().lower() in values]


def parse_login_list(raw: str) -> list[dict]:
    """Parse the textarea content into a list of resolution items.

    Returns a list of dicts. Each dict is one of:
        {"type": "login", "value": "<login>"}
        {"type": "game",  "value": "<category name>", "limit": int}
        {"type": "top",   "languages": ["de"], "limit": int}
        {"type": "search","value": "<free text>", "limit": int}

    Discovery tokens accepted (case-insensitive on the prefix):
        game:Just Chatting           -> top 10 streams in that category
        game:Just Chatting:25        -> top 25
        top                          -> 10 globally
        top:25                       -> 25 globally
        top:de:25                    -> top 25 German
        search:gronkh                -> first 10 search hits
        search:gronkh:5              -> first 5 hits
    """
    if not raw:
        return []
    items: list[dict] = []
    for token in raw.replace("\r", "\n").split("\n"):
        # Allow both newline- and comma-separated *plain* logins, but never
        # split tokens that start with a discovery prefix (categories may
        # contain commas, e.g. "game:Grand Theft Auto V, RP").
        sub_tokens = [token] if _is_discovery(token) else token.split(",")
        for raw_part in sub_tokens:
            t = raw_part.strip()
            if not t:
                continue
            parsed = _parse_single_token(t)
            if parsed is not None:
                items.append(parsed)
    return _dedup_items(items)


_DISCOVERY_PREFIXES = ("game:", "top", "search:")


def _is_discovery(t: str) -> bool:
    s = t.strip().lower()
    return s.startswith("game:") or s == "top" or s.startswith("top:") or s.startswith("search:")


def _parse_single_token(t: str) -> dict | None:
    low = t.lower()
    if low.startswith("game:"):
        rest = t[5:].strip()
        if not rest:
            return None
        # last `:N` is interpreted as limit if it's a number
        name, limit = _split_trailing_limit(rest, default=10)
        return {"type": "game", "value": name, "limit": limit}
    if low == "top":
        return {"type": "top", "languages": [], "limit": 10}
    if low.startswith("top:"):
        rest = t[4:].strip()
        # forms: "25", "de", "de:25"
        if not rest:
            return {"type": "top", "languages": [], "limit": 10}
        parts = [p.strip() for p in rest.split(":") if p.strip()]
        languages: list[str] = []
        limit = 10
        for p in parts:
            if p.isdigit():
                limit = int(p)
            else:
                languages.extend(s.strip().lower() for s in p.split(",") if s.strip())
        return {"type": "top", "languages": languages, "limit": limit}
    if low.startswith("search:"):
        rest = t[7:].strip()
        if not rest:
            return None
        name, limit = _split_trailing_limit(rest, default=10)
        return {"type": "search", "value": name, "limit": limit}

    if "/" in t:
        t = t.rstrip("/").rsplit("/", 1)[-1]
    login = t.lower()
    if not login.replace("_", "").isalnum():
        return None
    return {"type": "login", "value": login}


def _split_trailing_limit(text: str, *, default: int) -> tuple[str, int]:
    if ":" in text:
        head, tail = text.rsplit(":", 1)
        tail = tail.strip()
        if tail.isdigit():
            return head.strip(), max(1, min(100, int(tail)))
    return text.strip(), default


def _dedup_items(items: list[dict]) -> list[dict]:
    seen: set = set()
    out: list[dict] = []
    for item in items:
        if item["type"] == "login":
            key = ("login", item["value"])
        elif item["type"] == "game":
            key = ("game", item["value"].lower(), item["limit"])
        elif item["type"] == "top":
            key = ("top", tuple(sorted(item["languages"])), item["limit"])
        elif item["type"] == "search":
            key = ("search", item["value"].lower(), item["limit"])
        else:
            continue
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def resolve_logins(client: TwitchClient, items: list[dict]) -> list[str]:
    """Turn parsed items (logins + discovery tokens) into a flat unique login list."""
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        candidates: Iterable[str] = ()
        if item["type"] == "login":
            candidates = [item["value"]]
        elif item["type"] == "game":
            candidates = client.streams_in_game(item["value"], limit=item["limit"])
        elif item["type"] == "top":
            candidates = client.top_streams(
                languages=item.get("languages") or (),
                limit=item["limit"],
            )
        elif item["type"] == "search":
            candidates = client.search_channels(item["value"], limit=item["limit"])

        for login in candidates:
            login = (login or "").strip().lower()
            if not login or login in seen:
                continue
            if not login.replace("_", "").isalnum():
                continue
            seen.add(login)
            out.append(login)
    return out


def render_box_art(url: str, width: int = 272, height: int = 380) -> str:
    if not url:
        return ""
    return url.replace("{width}", str(width)).replace("{height}", str(height))
