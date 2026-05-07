"""Minimal Twitch Helix client used by the Dispatcharr Twitch EPG plugin.

Only the Client-Credentials OAuth flow is used: no user account, no redirect,
no interactive consent. The token is cached in memory for ~50 minutes so the
plugin doesn't hit /token on every refresh tick.

Endpoints used:
    POST https://id.twitch.tv/oauth2/token        (client_credentials)
    GET  https://api.twitch.tv/helix/users        (login -> id, profile)
    GET  https://api.twitch.tv/helix/streams      (live state, title, game_id)
    GET  https://api.twitch.tv/helix/games        (game_id -> name, box art)
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Iterable, Sequence

import requests

logger = logging.getLogger(__name__)

OAUTH_TOKEN_URL = "https://id.twitch.tv/oauth2/token"
HELIX_BASE = "https://api.twitch.tv/helix"
HELIX_BATCH = 100  # Twitch hard limit per call for users/streams/games

DEFAULT_TIMEOUT = 15


class TwitchAuthError(RuntimeError):
    pass


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
    box_art_url: str = ""  # has {width}x{height} placeholders


@dataclass
class _Token:
    access_token: str = ""
    expires_at: float = 0.0  # epoch seconds


class TwitchClient:
    """Thread-safe wrapper around the public Helix endpoints we need."""

    def __init__(self, client_id: str, client_secret: str, *, session: requests.Session | None = None):
        if not client_id or not client_secret:
            raise TwitchAuthError("client_id and client_secret are required")
        self.client_id = client_id.strip()
        self.client_secret = client_secret.strip()
        self._session = session or requests.Session()
        self._token = _Token()
        self._lock = threading.Lock()

    # --- auth -----------------------------------------------------------------

    def _ensure_token(self) -> str:
        with self._lock:
            now = time.time()
            if self._token.access_token and self._token.expires_at - 60 > now:
                return self._token.access_token
            resp = self._session.post(
                OAUTH_TOKEN_URL,
                data={
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                    "grant_type": "client_credentials",
                },
                timeout=DEFAULT_TIMEOUT,
            )
            if resp.status_code != 200:
                raise TwitchAuthError(
                    f"Twitch token request failed ({resp.status_code}): {resp.text[:200]}"
                )
            payload = resp.json()
            self._token = _Token(
                access_token=payload["access_token"],
                expires_at=now + int(payload.get("expires_in", 3600)),
            )
            return self._token.access_token

    def _headers(self) -> dict:
        return {
            "Client-Id": self.client_id,
            "Authorization": f"Bearer {self._ensure_token()}",
        }

    # --- low-level get with retry --------------------------------------------

    def _get(self, path: str, params: list[tuple[str, str]]) -> dict:
        url = f"{HELIX_BASE}{path}"
        for attempt in range(3):
            resp = self._session.get(url, headers=self._headers(), params=params, timeout=DEFAULT_TIMEOUT)
            if resp.status_code == 401 and attempt == 0:
                # Token might have been revoked early — force re-auth and retry
                with self._lock:
                    self._token = _Token()
                continue
            if resp.status_code == 429:
                # Rate limited. Twitch returns Ratelimit-Reset (epoch s).
                reset = float(resp.headers.get("Ratelimit-Reset", time.time() + 5))
                wait = max(1.0, reset - time.time())
                logger.warning("Twitch rate-limit hit, sleeping %.1fs", wait)
                time.sleep(min(wait, 30))
                continue
            if resp.status_code >= 500 and attempt < 2:
                time.sleep(1.5 * (attempt + 1))
                continue
            if resp.status_code != 200:
                raise RuntimeError(
                    f"Helix GET {path} failed ({resp.status_code}): {resp.text[:200]}"
                )
            return resp.json()
        raise RuntimeError(f"Helix GET {path} failed after retries")

    # --- public api -----------------------------------------------------------

    def get_users(self, logins: Sequence[str]) -> dict[str, TwitchUser]:
        """Return {lower_login: TwitchUser} for every login that exists."""
        out: dict[str, TwitchUser] = {}
        clean = [l.strip().lower() for l in logins if l and l.strip()]
        for batch in _chunks(clean, HELIX_BATCH):
            payload = self._get("/users", [("login", login) for login in batch])
            for item in payload.get("data", []):
                u = TwitchUser(
                    id=str(item["id"]),
                    login=item["login"],
                    display_name=item.get("display_name") or item["login"],
                    description=item.get("description") or "",
                    profile_image_url=item.get("profile_image_url") or "",
                )
                out[u.login.lower()] = u
        return out

    def get_streams(self, user_logins: Sequence[str]) -> dict[str, TwitchStream]:
        """Return {lower_login: TwitchStream} for every login currently live."""
        out: dict[str, TwitchStream] = {}
        clean = [l.strip().lower() for l in user_logins if l and l.strip()]
        for batch in _chunks(clean, HELIX_BATCH):
            payload = self._get("/streams", [("user_login", login) for login in batch])
            for item in payload.get("data", []):
                s = TwitchStream(
                    user_id=str(item["user_id"]),
                    user_login=item["user_login"],
                    user_name=item.get("user_name") or item["user_login"],
                    title=item.get("title") or "",
                    game_id=str(item.get("game_id") or ""),
                    game_name=item.get("game_name") or "",
                    started_at=item.get("started_at") or "",
                    viewer_count=int(item.get("viewer_count") or 0),
                    thumbnail_url=item.get("thumbnail_url") or "",
                    is_live=True,
                )
                out[s.user_login.lower()] = s
        return out

    def get_games(self, ids: Iterable[str]) -> dict[str, TwitchGame]:
        out: dict[str, TwitchGame] = {}
        clean = [g for g in {str(i) for i in ids if i}]
        for batch in _chunks(clean, HELIX_BATCH):
            payload = self._get("/games", [("id", gid) for gid in batch])
            for item in payload.get("data", []):
                out[str(item["id"])] = TwitchGame(
                    id=str(item["id"]),
                    name=item.get("name") or "",
                    box_art_url=item.get("box_art_url") or "",
                )
        return out


def _chunks(seq: Sequence[str], size: int):
    for i in range(0, len(seq), size):
        yield seq[i : i + size]


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
