"""TikTok: sign in once per account, post a video, check what became of it, and read its numbers.

Uses TikTok's Login Kit for desktop (OAuth with PKCE, redirected to this PC) and the Content Posting API's direct
post. Until TikTok has audited your app, it only accepts private posts (privacy "SELF_ONLY"); this module asks the
account which privacy levels it may use and never picks one it was not offered. Needs your TikTok app's client key
and secret, stored with `python -m villomillo key tiktok_key` and `key tiktok_secret`.
"""

from __future__ import annotations

import hashlib
import http.server
import json
import pathlib
import secrets as token_secrets
import threading
import time
import urllib.parse
from dataclasses import asdict, dataclass

import httpx

API = "https://open.tiktokapis.com"
AUTH_URL = "https://www.tiktok.com/v2/auth/authorize/"
REDIRECT_PORT = 8788
REDIRECT_URI = f"http://127.0.0.1:{REDIRECT_PORT}/callback/"
SCOPES = "user.info.basic,video.publish,video.list"
PRIVACY = {"private": "SELF_ONLY", "unlisted": "SELF_ONLY", "public": "PUBLIC_TO_EVERYONE"}  # unlisted does not exist on TikTok
CHUNK = 10 * 1024 * 1024  # TikTok wants chunks of 5 to 64 MB; the last one may be larger
REFRESH_BEFORE_S = 300
LIMIT_CODES = ("spam_risk_too_many_posts", "reached_active_user_cap", "rate_limit_exceeded")
FINAL = ("PUBLISH_COMPLETE", "FAILED")


class TikTokError(RuntimeError):
    """TikTok refused or could not do it. The message is safe to show on the post."""


@dataclass
class Tokens:
    access_token: str
    refresh_token: str
    expires_at: float  # unix seconds
    refresh_expires_at: float
    open_id: str = ""


def new_pkce() -> tuple[str, str]:
    """(code_verifier, code_challenge). TikTok's desktop flow hex-encodes the SHA-256, unlike standard PKCE."""
    verifier = token_secrets.token_urlsafe(48)[:64]
    return verifier, hashlib.sha256(verifier.encode()).hexdigest()


def auth_url(client_key: str, challenge: str, state: str) -> str:
    query = urllib.parse.urlencode({"client_key": client_key, "scope": SCOPES, "response_type": "code", "redirect_uri": REDIRECT_URI,
                                    "state": state, "code_challenge": challenge, "code_challenge_method": "S256"})
    return f"{AUTH_URL}?{query}"


def _tokens(data: dict, now: float) -> Tokens:
    if "access_token" not in data:
        raise TikTokError(f"TikTok did not give a sign-in: {data.get('error_description') or data.get('error') or 'no reason given'}")
    return Tokens(data["access_token"], data.get("refresh_token", ""), now + float(data.get("expires_in", 86400)),
                  now + float(data.get("refresh_expires_in", 365 * 86400)), data.get("open_id", ""))


def exchange_code(http: httpx.Client, key: str, secret: str, code: str, verifier: str, *, now: float | None = None) -> Tokens:
    r = http.post(f"{API}/v2/oauth/token/", data={"client_key": key, "client_secret": secret, "code": code, "grant_type": "authorization_code",
                                                  "redirect_uri": REDIRECT_URI, "code_verifier": verifier},
                  headers={"Content-Type": "application/x-www-form-urlencoded"})
    return _tokens(r.json(), now or time.time())


def refresh(http: httpx.Client, key: str, secret: str, tokens: Tokens, *, now: float | None = None) -> Tokens:
    if (now or time.time()) >= tokens.refresh_expires_at:
        raise TikTokError("the TikTok sign-in has expired: connect the account again")
    r = http.post(f"{API}/v2/oauth/token/", data={"client_key": key, "client_secret": secret, "grant_type": "refresh_token",
                                                  "refresh_token": tokens.refresh_token},
                  headers={"Content-Type": "application/x-www-form-urlencoded"})
    fresh = _tokens(r.json(), now or time.time())
    fresh.open_id = fresh.open_id or tokens.open_id
    return fresh


def token_path(root: pathlib.Path, profile_id: str) -> pathlib.Path:
    return root / f"tiktok_token_{profile_id}.json"


def is_connected(path: pathlib.Path) -> bool:
    try:
        return bool(json.loads(path.read_text(encoding="utf-8")).get("refresh_token"))
    except (OSError, ValueError):
        return False


def sign_in(path: pathlib.Path, key: str, secret: str, *, client: httpx.Client | None = None, open_browser=None,
            timeout_s: float = 300) -> Tokens:
    """Open TikTok's sign-in in the browser, wait for the redirect to this PC, and keep the tokens for the account."""
    import webbrowser

    client = client or httpx.Client(timeout=30)
    verifier, challenge = new_pkce()
    state = token_secrets.token_urlsafe(16)
    got: dict = {}
    done = threading.Event()

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            q = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            got.update({k: v[0] for k, v in q.items()})
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"<h2>VilloMillo is connected to TikTok.</h2><p>You can close this tab.</p>")
            done.set()

        def log_message(self, *a):
            pass

    server = http.server.HTTPServer(("127.0.0.1", REDIRECT_PORT), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        (open_browser or webbrowser.open)(auth_url(key, challenge, state))
        if not done.wait(timeout_s):
            raise TikTokError("the TikTok sign-in window was not finished in time")
    finally:
        server.shutdown()
        server.server_close()
    if got.get("state") != state:
        raise TikTokError("the TikTok sign-in answered for a different request: try again")
    if "code" not in got:
        raise TikTokError(f"TikTok did not allow it: {got.get('error_description') or got.get('error') or 'no reason given'}")
    tokens = exchange_code(client, key, secret, got["code"], verifier)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(tokens)), encoding="utf-8")
    return tokens


class TikTok:
    """One TikTok account. Keeps its tokens fresh and talks to the Content Posting API."""

    def __init__(self, token_file: pathlib.Path, key: str, secret: str, *, http: httpx.Client | None = None,
                 sleep=time.sleep, clock=time.time):
        self.path, self.key, self.secret = token_file, key, secret
        self.http = http or httpx.Client(timeout=120)
        self._sleep, self._clock = sleep, clock

    # -- sign-in ----------------------------------------------------------------------------------------------

    def _load(self) -> Tokens:
        try:
            return Tokens(**json.loads(self.path.read_text(encoding="utf-8")))
        except (OSError, ValueError, TypeError):
            raise TikTokError("this channel is not connected to TikTok: connect the account first") from None

    def access_token(self) -> str:
        tokens = self._load()
        if self._clock() >= tokens.expires_at - REFRESH_BEFORE_S:
            tokens = refresh(self.http, self.key, self.secret, tokens, now=self._clock())
            self.path.write_text(json.dumps(asdict(tokens)), encoding="utf-8")
        return tokens.access_token

    def _call(self, path: str, body: dict | None = None, *, params: dict | None = None) -> dict:
        try:
            r = self.http.post(f"{API}{path}", json=body or {}, params=params,
                               headers={"Authorization": f"Bearer {self.access_token()}", "Content-Type": "application/json; charset=UTF-8"})
            data = r.json()
        except (httpx.HTTPError, ValueError) as e:
            raise TikTokError(f"could not reach TikTok ({type(e).__name__})") from None
        error = data.get("error") or {}
        code = error.get("code", "ok")
        if code != "ok":
            if code in LIMIT_CODES:
                raise TikTokError(f"TikTok's daily posting limit for this account was reached ({code}); the post waits for tomorrow")
            if code == "unaudited_client_can_only_post_to_private_accounts":
                raise TikTokError("TikTok has not audited this app yet, so the account must be private for it to post")
            if code in ("access_token_invalid", "scope_not_authorized", "scope_permission_missed"):
                raise TikTokError(f"the TikTok sign-in is not valid ({code}): reconnect the account")
            raise TikTokError(f"TikTok said {code}: {error.get('message', '')[:160]}")
        return data.get("data") or {}

    # -- posting ----------------------------------------------------------------------------------------------

    def creator_info(self) -> dict:
        return self._call("/v2/post/publish/creator_info/query/")

    def privacy_for(self, wanted: str, info: dict) -> tuple[str, str | None]:
        """The privacy level to post with and a note when it is not what was wanted. Never a level TikTok did not offer."""
        level = PRIVACY.get(wanted, "SELF_ONLY")
        options = info.get("privacy_level_options") or ["SELF_ONLY"]
        if level in options:
            return level, None
        return ("SELF_ONLY" if "SELF_ONLY" in options else options[0],
                f"TikTok only offers {', '.join(options)} to this account right now, so the post is not public")

    def post(self, path: pathlib.Path, caption: str, privacy: str = "private") -> dict:
        """Upload one video and wait for TikTok to say what became of it. Returns {publish_id, status, post_id, notes}."""
        info = self.creator_info()
        size = path.stat().st_size
        limit = info.get("max_video_post_duration_sec")
        level, note = self.privacy_for(privacy, info)
        chunk = min(size, CHUNK)
        total = max(1, size // chunk)  # the last chunk takes the remainder, as TikTok allows
        init = self._call("/v2/post/publish/video/init/", {
            "post_info": {"title": caption[:2200], "privacy_level": level, "disable_duet": False, "disable_comment": False,
                          "disable_stitch": False},
            "source_info": {"source": "FILE_UPLOAD", "video_size": size, "chunk_size": chunk, "total_chunk_count": total}})
        publish_id, upload_url = init["publish_id"], init["upload_url"]
        self._upload(upload_url, path, size, chunk, total)
        status = self.wait(publish_id)
        notes = [note] if note else []
        if limit:
            notes.append(f"TikTok allows videos up to {limit} s on this account")
        return {"publish_id": publish_id, "status": status["status"], "post_id": status.get("post_id"), "notes": notes}

    def _upload(self, url: str, path: pathlib.Path, size: int, chunk: int, total: int) -> None:
        with path.open("rb") as f:
            for i in range(total):
                start = i * chunk
                end = size - 1 if i == total - 1 else start + chunk - 1
                f.seek(start)
                data = f.read(end - start + 1)
                try:
                    r = self.http.put(url, content=data, headers={"Content-Type": "video/mp4", "Content-Length": str(len(data)),
                                                                  "Content-Range": f"bytes {start}-{end}/{size}"})
                except httpx.HTTPError as e:
                    raise TikTokError(f"the upload to TikTok broke off ({type(e).__name__})") from None
                if r.status_code not in (200, 201, 206):
                    raise TikTokError(f"TikTok did not accept the upload (HTTP {r.status_code})")

    def status(self, publish_id: str) -> dict:
        data = self._call("/v2/post/publish/status/fetch/", {"publish_id": publish_id})
        ids = data.get("publicaly_available_post_id") or []  # TikTok's own spelling
        return {"status": data.get("status", ""), "fail_reason": data.get("fail_reason", ""), "post_id": str(ids[0]) if ids else None}

    def wait(self, publish_id: str, *, tries: int = 24, every: float = 5.0) -> dict:
        """Look until TikTok says it is published or failed. If it is still working after about two minutes, say so and go on."""
        last = {"status": "PROCESSING_UPLOAD", "fail_reason": "", "post_id": None}
        for _ in range(tries):
            last = self.status(publish_id)
            if last["status"] == "FAILED":
                raise TikTokError(f"TikTok could not publish it: {last['fail_reason'] or 'no reason given'}")
            if last["status"] in FINAL:
                return last
            self._sleep(every)
        return last

    # -- numbers ----------------------------------------------------------------------------------------------

    def video_stats(self, video_ids: list[str]) -> dict[str, dict]:
        """Views, likes, comments and shares for public videos, by id. Private posts are not in the answer."""
        if not video_ids:
            return {}
        data = self._call("/v2/video/query/", {"filters": {"video_ids": video_ids[:20]}},
                          params={"fields": "id,view_count,like_count,comment_count,share_count"})
        return {str(v["id"]): {"views": v.get("view_count"), "likes": v.get("like_count"),
                               "comments": v.get("comment_count"), "shares": v.get("share_count")} for v in data.get("videos", [])}
