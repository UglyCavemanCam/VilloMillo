"""A small Twitch client for Research: find categories and the channels streaming in them right now.

It uses the app's own sign-in (a Client ID and Client Secret from dev.twitch.tv, kept in the keychain), which needs no viewer
to log in. Only public information is read. The secret and the token are never written to a log or an error message.
"""

from __future__ import annotations

import time

import httpx

TOKEN_URL = "https://id.twitch.tv/oauth2/token"
API = "https://api.twitch.tv/helix"


class TwitchError(RuntimeError):
    """The message is safe to show: it never contains the key or the token."""


class Twitch:
    def __init__(self, client_id: str, client_secret: str, http: httpx.Client | None = None, clock=time.monotonic):
        self._id, self._secret = client_id, client_secret
        self.http, self._clock = http or httpx.Client(timeout=20), clock
        self._token: str | None = None
        self._expires = 0.0

    def _sign_in(self) -> str:
        try:
            r = self.http.post(TOKEN_URL, data={"client_id": self._id, "client_secret": self._secret, "grant_type": "client_credentials"})
        except httpx.HTTPError as e:
            raise TwitchError(f"could not reach Twitch ({type(e).__name__})") from None
        if r.status_code != 200:
            raise TwitchError(f"Twitch refused the Client ID and Secret (HTTP {r.status_code}). Store them again with: .\\.venv\\Scripts\\python.exe -m villomillo key twitch")
        data = r.json()
        self._token = data["access_token"]
        self._expires = self._clock() + max(60, int(data.get("expires_in", 3600)) - 300)
        return self._token

    def _get(self, path: str, **params) -> list[dict]:
        for attempt in (1, 2):
            token = self._token if self._token and self._clock() < self._expires else self._sign_in()
            try:
                r = self.http.get(f"{API}/{path}", params={k: v for k, v in params.items() if v is not None},
                                  headers={"Client-Id": self._id, "Authorization": f"Bearer {token}"})
            except httpx.HTTPError as e:
                raise TwitchError(f"{path}: could not reach Twitch ({type(e).__name__})") from None
            if r.status_code == 401 and attempt == 1:
                self._token = None  # expired early: sign in once more
                continue
            if r.status_code != 200:
                raise TwitchError(f"{path}: Twitch answered HTTP {r.status_code}")
            return r.json().get("data", [])
        raise TwitchError(f"{path}: Twitch kept refusing the sign-in")

    def categories(self, query: str, first: int = 3) -> list[dict]:
        """Categories (games and topics such as Just Chatting) whose names match: [{id, name}]."""
        return [{"id": c["id"], "name": c["name"]} for c in self._get("search/categories", query=query, first=first)]

    def top_streams(self, game_id: str | None = None, first: int = 10) -> list[dict]:
        """Channels live right now, most watched first: [{login, name, viewers, game, title}]."""
        return [{"login": s["user_login"].lower(), "name": s["user_name"], "viewers": int(s.get("viewer_count") or 0), "game": s.get("game_name", ""),
                 "title": s.get("title", "")} for s in self._get("streams", game_id=game_id, first=first)]

    def search_channels(self, query: str, first: int = 10) -> list[dict]:
        """Channels live now whose name or title matches: same shape as top_streams (viewers unknown, so 0)."""
        return [{"login": c["broadcaster_login"].lower(), "name": c["display_name"], "viewers": 0, "game": c.get("game_name", ""), "title": c.get("title", "")}
                for c in self._get("search/channels", query=query, live_only="true", first=first)]
