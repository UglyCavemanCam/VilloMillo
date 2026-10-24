"""TikTok: sign-in, posting, the daily limit, and the numbers. No network: TikTok's answers are scripted."""

from __future__ import annotations

import hashlib
import json
import pathlib
from datetime import datetime, timedelta, timezone

import httpx
import pytest
import solved_profile
from fastapi.testclient import TestClient

from villomillo import accounts, channels, config, posting, secrets, store, tiktok
from villomillo.db import Database
from villomillo.quota import Quota
from villomillo.services import Services
from villomillo.web.app import create_app

NOW = datetime(2026, 10, 24, 16, 0, tzinfo=timezone.utc)


class Script:
    """A fake TikTok: records requests and answers from `replies` by path."""

    def __init__(self, **replies):
        self.replies, self.seen = replies, []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = request.content
        self.seen.append((request.method, request.url.path, request.url.query.decode(), dict(request.headers), body))
        reply = self.replies.get(request.url.path)
        if callable(reply):
            reply = reply(request)
        if reply is None:
            return httpx.Response(404, json={})
        return reply if isinstance(reply, httpx.Response) else httpx.Response(200, json=reply)

    def client(self):
        return httpx.Client(transport=httpx.MockTransport(self))

    def bodies(self, path):
        return [json.loads(b) for m, p, q, h, b in self.seen if p == path and b and m == "POST"]


def ok(data=None):
    return {"data": data or {}, "error": {"code": "ok", "message": ""}}


def err(code, message=""):
    return {"data": {}, "error": {"code": code, "message": message}}


def token_file(tmp_path, *, expires_in=3600, refresh_in=86400 * 300) -> pathlib.Path:
    path = tmp_path / "tiktok_token.json"
    path.write_text(json.dumps({"access_token": "AT", "refresh_token": "RT", "expires_at": NOW.timestamp() + expires_in,
                                "refresh_expires_at": NOW.timestamp() + refresh_in, "open_id": "oid"}))
    return path


def account(tmp_path, script, **kw) -> tiktok.TikTok:
    return tiktok.TikTok(token_file(tmp_path, **kw), "KEY", "SECRET", http=script.client(), sleep=lambda s: None, clock=lambda: NOW.timestamp())


# ---- sign-in -------------------------------------------------------------------------------------------------------


def test_the_pkce_challenge_is_the_hex_sha256_of_the_verifier():
    verifier, challenge = tiktok.new_pkce()
    assert 43 <= len(verifier) <= 128 and challenge == hashlib.sha256(verifier.encode()).hexdigest()
    assert tiktok.new_pkce()[0] != verifier


def test_the_sign_in_address_asks_only_for_what_is_needed():
    url = tiktok.auth_url("KEY", "CH", "ST")
    assert url.startswith("https://www.tiktok.com/v2/auth/authorize/?") and "client_key=KEY" in url and "code_challenge=CH" in url
    assert "user.info.basic%2Cvideo.publish%2Cvideo.list" in url and "127.0.0.1%3A8788%2Fcallback%2F" in url


def test_the_code_is_exchanged_for_tokens_and_a_refusal_is_explained():
    script = Script(**{"/v2/oauth/token/": {"access_token": "AT", "refresh_token": "RT", "expires_in": 86400,
                                          "refresh_expires_in": 31536000, "open_id": "oid"}})
    tokens = tiktok.exchange_code(script.client(), "KEY", "SECRET", "CODE", "VERIFIER", now=1000.0)
    assert (tokens.access_token, tokens.refresh_token, tokens.open_id) == ("AT", "RT", "oid") and tokens.expires_at == 87400.0
    form = script.seen[0][4].decode()
    assert "code_verifier=VERIFIER" in form and "grant_type=authorization_code" in form and "client_secret=SECRET" in form
    bad = Script(**{"/v2/oauth/token/": {"error": "invalid_grant", "error_description": "code expired"}})
    with pytest.raises(tiktok.TikTokError, match="code expired"):
        tiktok.exchange_code(bad.client(), "KEY", "SECRET", "CODE", "V")


def test_an_access_token_near_its_end_is_renewed_and_kept(tmp_path):
    script = Script(**{"/v2/oauth/token/": {"access_token": "NEW", "refresh_token": "RT2", "expires_in": 86400, "refresh_expires_in": 31536000}})
    tt = account(tmp_path, script, expires_in=60)
    assert tt.access_token() == "NEW"
    assert json.loads(tt.path.read_text())["refresh_token"] == "RT2" and json.loads(tt.path.read_text())["open_id"] == "oid"
    assert account(tmp_path, Script(), expires_in=3600).access_token() == "AT"  # still good: no request at all


def test_an_expired_sign_in_asks_you_to_connect_again(tmp_path):
    with pytest.raises(tiktok.TikTokError, match="connect the account again"):
        account(tmp_path, Script(), expires_in=-10, refresh_in=-10).access_token()
    with pytest.raises(tiktok.TikTokError, match="not connected"):
        tiktok.TikTok(tmp_path / "missing.json", "K", "S").access_token()


def test_is_connected_reads_the_token_file(tmp_path):
    assert not tiktok.is_connected(tmp_path / "missing.json") and tiktok.is_connected(token_file(tmp_path))


# ---- posting --------------------------------------------------------------------------------------------------------


def posting_script(*, options=("SELF_ONLY",), statuses=("PROCESSING_UPLOAD", "PUBLISH_COMPLETE"), post_ids=(777,), **extra):
    queue = list(statuses)

    def status(request):
        s = queue.pop(0) if len(queue) > 1 else queue[0]
        data = {"status": s}
        if s == "PUBLISH_COMPLETE" and post_ids:
            data["publicaly_available_post_id"] = list(post_ids)
        if s == "FAILED":
            data["fail_reason"] = "video_pull_failed"
        return ok(data)

    return Script(**{
        "/v2/post/publish/creator_info/query/": ok({"privacy_level_options": list(options), "max_video_post_duration_sec": 600}),
        "/v2/post/publish/video/init/": ok({"publish_id": "pub1", "upload_url": "https://upload.tiktok.test/u/1"}),
        "/u/1": httpx.Response(201), "/v2/post/publish/status/fetch/": status, **extra})


def test_a_video_is_uploaded_in_chunks_and_its_outcome_is_returned(tmp_path, monkeypatch):
    monkeypatch.setattr(tiktok, "CHUNK", 4)
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"0123456789")
    script = posting_script()
    result = account(tmp_path, script).post(video, "A caption #fyp", "private")
    init = script.bodies("/v2/post/publish/video/init/")[0]
    assert init["source_info"] == {"source": "FILE_UPLOAD", "video_size": 10, "chunk_size": 4, "total_chunk_count": 2}
    assert init["post_info"]["title"] == "A caption #fyp" and init["post_info"]["privacy_level"] == "SELF_ONLY"
    puts = [(h["content-range"], b) for m, p, q, h, b in script.seen if m == "PUT"]
    assert puts == [("bytes 0-3/10", b"0123"), ("bytes 4-9/10", b"456789")]
    assert result["publish_id"] == "pub1" and result["status"] == "PUBLISH_COMPLETE" and result["post_id"] == "777"
    assert any("600 s" in n for n in result["notes"])
    assert all(h["authorization"] == "Bearer AT" for m, p, q, h, b in script.seen if "tiktokapis" in str(h.get("host", "open.tiktokapis.com")) and p.startswith("/v2"))


def test_a_public_post_is_asked_for_only_when_tiktok_offers_it(tmp_path):
    tt = account(tmp_path, Script())
    assert tt.privacy_for("public", {"privacy_level_options": ["PUBLIC_TO_EVERYONE", "SELF_ONLY"]}) == ("PUBLIC_TO_EVERYONE", None)
    level, note = tt.privacy_for("public", {"privacy_level_options": ["SELF_ONLY"]})
    assert level == "SELF_ONLY" and "not public" in note
    assert tt.privacy_for("unlisted", {"privacy_level_options": ["PUBLIC_TO_EVERYONE", "SELF_ONLY"]})[0] == "SELF_ONLY"


def test_a_private_post_has_no_public_id_yet_and_is_still_a_success(tmp_path):
    video = tmp_path / "c.mp4"
    video.write_bytes(b"abc")
    script = posting_script(statuses=("SEND_TO_USER_INBOX", "SEND_TO_USER_INBOX"), post_ids=())
    result = account(tmp_path, script).post(video, "x")
    assert result["post_id"] is None and result["status"] == "SEND_TO_USER_INBOX"


@pytest.mark.parametrize(("code", "text"), [
    ("spam_risk_too_many_posts", "daily posting limit"),
    ("unaudited_client_can_only_post_to_private_accounts", "not audited this app"),
    ("access_token_invalid", "reconnect the account"),
    ("something_else", "something_else"),
])
def test_tiktoks_refusals_are_said_in_plain_words(tmp_path, code, text):
    video = tmp_path / "c.mp4"
    video.write_bytes(b"abc")
    script = posting_script(**{"/v2/post/publish/creator_info/query/": err(code, "no")})
    with pytest.raises(tiktok.TikTokError, match=text):
        account(tmp_path, script).post(video, "x")


def test_a_failed_publish_and_a_broken_upload_are_reported(tmp_path):
    video = tmp_path / "c.mp4"
    video.write_bytes(b"abc")
    with pytest.raises(tiktok.TikTokError, match="video_pull_failed"):
        account(tmp_path, posting_script(statuses=("FAILED",))).post(video, "x")
    with pytest.raises(tiktok.TikTokError, match="did not accept the upload"):
        account(tmp_path, posting_script(**{"/u/1": httpx.Response(500)})).post(video, "x")


def test_a_video_still_processing_after_two_minutes_is_not_an_error(tmp_path):
    video = tmp_path / "c.mp4"
    video.write_bytes(b"abc")
    result = account(tmp_path, posting_script(statuses=("PROCESSING_UPLOAD",))).post(video, "x")
    assert result["status"] == "PROCESSING_UPLOAD"


def test_the_numbers_of_public_videos_are_read_by_id(tmp_path):
    script = Script(**{"/v2/video/query/": ok({"videos": [{"id": 777, "view_count": 1200, "like_count": 90, "comment_count": 4, "share_count": 7}]})})
    stats = account(tmp_path, script).video_stats(["777", "888"])
    assert stats == {"777": {"views": 1200, "likes": 90, "comments": 4, "shares": 7}}
    assert "fields=id%2Cview_count" in script.seen[0][2] and script.bodies("/v2/video/query/")[0]["filters"]["video_ids"] == ["777", "888"]
    assert account(tmp_path, Script()).video_stats([]) == {}


# ---- posting through the channel ---------------------------------------------------------------------------------------


class FakeTikTok:
    def __init__(self, post_id="555", fail=None):
        self.sent, self.post_id, self.fail = [], post_id, fail

    def post(self, path, caption, privacy):
        if self.fail:
            raise tiktok.TikTokError(self.fail)
        self.sent.append((str(path), caption, privacy))
        return {"publish_id": "pub9", "status": "PUBLISH_COMPLETE", "post_id": self.post_id, "notes": ["TikTok only offers SELF_ONLY"]}


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "tt.db")
    with database.conn() as c:
        c.execute("INSERT INTO videos (profile_id, video_id, found_by, channel_id, channel_title, title, description, tags_json, duration_s, "
                  "published_at, first_seen_at, last_seen_at) VALUES ('solved','vid00000001','paste','UC','Show','An Episode','','[]',3000,?,?,?)",
                  (store.iso(), store.iso(), store.iso()))
    return database


def both(**changes) -> config.Profile:
    p = solved_profile.solved()
    posting_cfg = p.posting.model_copy(update={"platforms": {"youtube_shorts": "api", "tiktok": "api"}, "dry_run": False, "min_gap_minutes": 0,
                                               "daily_cap": 100, "platform_caps": {"tiktok": 15}, "tags": {}, **changes})
    return p.model_copy(update={"posting": posting_cfg, "tiktok_handle": "@podscliphouse"})


def clip(db, n=1) -> int:
    with db.conn() as c:
        c.execute("INSERT INTO clips_ready (profile_id, engine_clip_id, video_id, start_s, end_s, engine_score, hook, file_path, validation_json, "
                  "grade_json, state, reason, created_at, category) VALUES ('solved', ?, 'vid00000001', 0, 30, 88, 'A hard truth', ?, '{}', ?, 'approved', 'r', ?, 'Podcasts')",
                  (n, f"/tmp/{n}.mp4", json.dumps({"caption_text": "words"}), store.iso()))
        return c.execute("SELECT MAX(id) AS id FROM clips_ready").fetchone()["id"]


def test_one_clip_is_planned_for_youtube_and_tiktok_together(db):
    p = both()
    posts = posting.plan_all(db, p, clip(db), now=NOW)
    assert {x["platform"] for x in posts} == {"youtube_shorts", "tiktok"}
    assert posting.plan_all(db, p, 1, now=NOW) == []  # nothing is planned twice
    with db.conn() as c:
        tt = c.execute("SELECT title, caption FROM posts WHERE platform='tiktok'").fetchone()
    assert tt["title"] == "" and "A hard truth" in tt["caption"]


def test_a_due_tiktok_post_is_sent_and_its_ids_are_kept(db):
    p = both()
    posting.plan_all(db, p, clip(db), now=NOW)
    accounts.set_audit(db, "solved", "tiktok", True)
    accounts.set_audit(db, "solved", "youtube_shorts", True)
    fake = FakeTikTok()
    lines = posting.run_due(db, p, now=NOW + timedelta(hours=1), adapters={"tiktok": posting.TikTokPoster(fake, "@podscliphouse")})
    with db.conn() as c:
        row = c.execute("SELECT status, url, publish_id, remote_id, posted_at FROM posts WHERE platform='tiktok'").fetchone()
    assert row["status"] == "uploaded" and row["url"] == "https://www.tiktok.com/@podscliphouse/video/555"
    assert (row["publish_id"], row["remote_id"]) == ("pub9", "555") and row["posted_at"]
    assert fake.sent[0][2] == "private" and any("tiktok" in line and "uploaded" in line for line in lines)
    assert posting.posted_today(db, "solved", "tiktok", now=NOW + timedelta(hours=1)) == 1


def test_the_audit_must_have_passed_before_tiktok_posts_for_real(db):
    p = both()
    posting.plan_all(db, p, clip(db), now=NOW)
    fake = FakeTikTok()
    posting.run_due(db, p, now=NOW + timedelta(hours=1), adapters={"tiktok": posting.TikTokPoster(fake)})
    with db.conn() as c:
        assert c.execute("SELECT status FROM posts WHERE platform='tiktok'").fetchone()["status"] == "manual_ready"
    assert fake.sent == []


def test_tiktoks_daily_limit_holds_the_next_post_until_tomorrow(db):
    p = both(platform_caps={"tiktok": 1})
    posting.plan_all(db, p, clip(db, 1), now=NOW)
    posting.plan_all(db, p, clip(db, 2), now=NOW)
    accounts.set_audit(db, "solved", "tiktok", True)
    fake = FakeTikTok()
    later = NOW + timedelta(hours=1)
    lines = posting.run_due(db, p, now=later, adapters={"tiktok": posting.TikTokPoster(fake)})
    assert len(fake.sent) == 1 and not any("limit" in line for line in lines)
    with db.conn() as c:
        assert sorted(r["status"] for r in c.execute("SELECT status FROM posts WHERE platform='tiktok'")) == ["queued", "uploaded"]
    with db.conn() as c:  # the second post was scheduled for the next day, not squeezed in
        slots = [r["scheduled_at"] for r in c.execute("SELECT scheduled_at FROM posts WHERE platform='tiktok' ORDER BY id")]
    assert store.parse_time(slots[1]) - store.parse_time(slots[0]) >= timedelta(hours=6)
    with db.conn() as c:  # even if it were due now, today's limit still holds it back
        c.execute("UPDATE posts SET scheduled_at=? WHERE platform='tiktok' AND status='queued'", (store.iso(NOW),))
    again = posting.run_due(db, p, now=later, adapters={"tiktok": posting.TikTokPoster(fake)})
    assert len(fake.sent) == 1 and any("limit of 1" in line for line in again)


def test_a_tiktok_failure_is_shown_and_the_limit_message_does_not_count_against_the_account(db):
    p = both()
    posting.plan_all(db, p, clip(db), now=NOW)
    accounts.set_audit(db, "solved", "tiktok", True)
    fake = FakeTikTok(fail="TikTok's daily posting limit for this account was reached (spam_risk_too_many_posts); the post waits for tomorrow")
    posting.run_due(db, p, now=NOW + timedelta(hours=1), adapters={"tiktok": posting.TikTokPoster(fake)})
    with db.conn() as c:
        assert "daily posting limit" in c.execute("SELECT error FROM posts WHERE platform='tiktok'").fetchone()["error"]
    assert accounts.state(db, "solved", "tiktok")["failures"] == 0 and not accounts.is_paused(db, "solved", "tiktok")


# ---- real numbers -------------------------------------------------------------------------------------------------------


class FakeYT:
    def videos(self, ids):
        return [{"video_id": i, "views": 4000, "likes": 100, "comments": 5} for i in ids]


class FakeAccount:
    def __init__(self):
        self.asked = []

    def status(self, publish_id):
        return {"status": "PUBLISH_COMPLETE", "fail_reason": "", "post_id": "555"}

    def video_stats(self, ids):
        self.asked.append(ids)
        return {"555": {"views": 900, "likes": 60, "comments": 2, "shares": 3}}


def sent_posts(db, p):
    posting.plan_all(db, p, clip(db), now=NOW)
    with db.conn() as c:
        c.execute("UPDATE posts SET status='uploaded', posted_at=?", (store.iso(NOW),))
        c.execute("UPDATE posts SET url='https://youtube.com/shorts/aB3dEfGhIjK' WHERE platform='youtube_shorts'")
        c.execute("UPDATE posts SET publish_id='pub9' WHERE platform='tiktok'")


def test_real_views_are_collected_for_youtube_and_tiktok(db):
    p = both()
    sent_posts(db, p)
    lines = posting.collect_stats(db, p, yt=FakeYT(), tiktok_account=FakeAccount(), now=NOW + timedelta(hours=6))
    with db.conn() as c:
        got = {r["platform"]: (r["views"], r["likes"]) for r in c.execute(
            "SELECT p.platform, s.views, s.likes FROM post_stats s JOIN posts p ON p.id=s.post_id")}
        assert c.execute("SELECT remote_id FROM posts WHERE platform='tiktok'").fetchone()["remote_id"] == "555"
    assert got == {"youtube_shorts": (4000, 100), "tiktok": (900, 60)} and len(lines) == 2


def test_nothing_is_collected_without_a_connection_and_a_numbers_failure_is_reported(db):
    p = both()
    sent_posts(db, p)
    assert posting.collect_stats(db, p, now=NOW) == []

    class Broken(FakeAccount):
        def video_stats(self, ids):
            raise tiktok.TikTokError("scope_not_authorized")

    lines = posting.collect_stats(db, p, tiktok_account=Broken(), now=NOW)
    assert any("could not be read" in line for line in lines)


def test_a_sudden_fall_in_views_pauses_the_account(db):
    p = both()
    for n in range(1, 11):
        posting.plan(db, p, clip(db, n), now=NOW, platform="tiktok")
    with db.conn() as c:
        ids = [r["id"] for r in c.execute("SELECT id FROM posts ORDER BY id")]
        c.execute("UPDATE posts SET status='uploaded', posted_at=?, remote_id=id", (store.iso(NOW),))
        for i, post_id in enumerate(ids):
            c.execute("INSERT INTO post_stats (post_id, taken_at, views) VALUES (?,?,?)", (post_id, store.iso(NOW), 1000 if i < 5 else 50))

    class Quiet(FakeAccount):
        def video_stats(self, ids):
            return {}

    lines = posting.collect_stats(db, p, tiktok_account=Quiet(), now=NOW)
    assert accounts.is_paused(db, "solved", "tiktok") and any("paused" in line for line in lines)


# ---- the dashboard ------------------------------------------------------------------------------------------------------


@pytest.fixture
def web(tmp_path):
    database = Database(tmp_path / "web.db")
    sv = Services(config.AppConfig(), channels.load(database), database, Quota(database, general_cap=9, search_cap=9))
    return TestClient(create_app(sv), base_url="http://127.0.0.1:8780"), sv


def test_the_channel_page_explains_what_tiktok_needs(web, monkeypatch):
    client, sv = web
    monkeypatch.setattr(secrets, "is_stored", lambda name: False)
    monkeypatch.setattr(secrets, "get", lambda name: (_ for _ in ()).throw(secrets.MissingSecret(name)))
    page = client.get("/channels/solved").text
    assert "@podscliphouse" in page and "key tiktok_key" in page and "127.0.0.1:8788/callback/" in page
    assert "Posted today" in page and "of 15" in page and "of 100" in page


def test_connecting_tiktok_needs_the_apps_keys_and_then_starts_the_sign_in(web, monkeypatch):
    client, sv = web
    monkeypatch.setattr(secrets, "get", lambda name: (_ for _ in ()).throw(secrets.MissingSecret(name)))
    r = client.post("/api/channel-tiktok", data={"profile": "solved"})
    assert r.status_code == 422 and "tiktok_key" in r.text
    started = []
    monkeypatch.setattr(secrets, "get", lambda name: "x")
    monkeypatch.setattr(posting, "connect_tiktok", lambda profile: started.append(profile.id) or {"open_id": "o"})
    assert client.post("/api/channel-tiktok", data={"profile": "solved"}).status_code == 200
    assert client.post("/api/channel-tiktok", data={"profile": "nope"}).status_code == 404
    assert client.post("/api/channel-tiktok", data={"profile": "solved"}, headers={"Origin": "https://evil.example"}).status_code == 403


def test_a_tiktok_handle_adds_tiktok_to_the_channel_and_removing_it_takes_it_away(web):
    client, sv = web
    base = {"name": "Podcasts", "category": "Podcasts", "about": "Talk"}
    client.post("/channels/solved/save", data={**base, "tiktok_handle": ""}, follow_redirects=False)
    assert "tiktok" not in sv.profiles["solved"].posting.platforms
    client.post("/channels/solved/save", data={**base, "tiktok_handle": "@podscliphouse", "tiktok_route": "api", "tiktok_daily_cap": "10"}, follow_redirects=False)
    p = sv.profiles["solved"].posting
    assert p.platforms["tiktok"] == "api" and p.cap_for("tiktok") == 10
    assert client.post("/channels/solved/save", data={**base, "tiktok_handle": "@podscliphouse", "tiktok_daily_cap": "16"}).status_code == 422
    assert client.post("/channels/solved/save", data={**base, "tiktok_handle": "@podscliphouse", "tiktok_route": "carrier pigeon"}).status_code == 422
    assert client.post("/channels/solved/save", data={**base, "tiktok_handle": "", "tiktok_route": "api"}).status_code == 422


def test_the_starting_channel_posts_to_tiktok_with_its_own_limits():
    p = config.load_profiles()["solved"]
    assert p.tiktok_handle == "@podscliphouse" and set(p.posting.platforms) == {"youtube_shorts", "tiktok"}
    assert (p.posting.cap_for("youtube_shorts"), p.posting.cap_for("tiktok")) == (100, 15) and p.posting.tags == {}


def test_insights_shows_the_real_numbers_that_were_collected(web):
    client, sv = web
    with sv.db.conn() as c:
        c.execute("INSERT INTO clips_ready (profile_id, engine_clip_id, video_id, start_s, end_s, engine_score, hook, file_path, validation_json, grade_json, state, reason, created_at) "
                  "VALUES ('solved', 1, 'v', 0, 30, 80, 'h', 'f', '{}', '{}', 'approved', 'r', ?)", (store.iso(),))
        c.execute("INSERT INTO posts (profile_id, clip_id, platform, route, status, title, caption, tags_json, scheduled_at, created_at, url) "
                  "VALUES ('solved', 1, 'tiktok', 'api', 'uploaded', '', 'c', '[]', ?, ?, 'https://www.tiktok.com/@a/video/1')", (store.iso(), store.iso()))
        c.execute("INSERT INTO post_stats (post_id, taken_at, views, likes) VALUES (1, ?, 12345, 99)", (store.iso(),))
    page = client.get("/insights?profile=solved").text
    assert "12,345" in page and "tiktok" in page.lower()


# ---- the sign-in window ---------------------------------------------------------------------------------------------------


def test_signing_in_catches_the_redirect_exchanges_the_code_and_keeps_the_tokens(tmp_path):
    script = Script(**{"/v2/oauth/token/": {"access_token": "AT", "refresh_token": "RT", "expires_in": 86400, "refresh_expires_in": 31536000, "open_id": "oid"}})
    path = tmp_path / "tok.json"

    def browser(url):  # the person signs in on TikTok, which sends the browser back to this PC
        q = dict(p.split("=", 1) for p in url.split("?", 1)[1].split("&"))
        httpx.get(f"{tiktok.REDIRECT_URI}?code=CODE&state={q['state']}", timeout=5)

    tokens = tiktok.sign_in(path, "KEY", "SECRET", client=script.client(), open_browser=browser, timeout_s=10)
    assert tokens.open_id == "oid" and tiktok.is_connected(path)
    assert "code=CODE" in script.seen[0][4].decode() and "code_verifier=" in script.seen[0][4].decode()


def test_a_sign_in_answer_for_the_wrong_request_or_a_refusal_is_not_accepted(tmp_path):
    script = Script()

    def wrong(url):
        httpx.get(f"{tiktok.REDIRECT_URI}?code=CODE&state=not-ours", timeout=5)

    with pytest.raises(tiktok.TikTokError, match="different request"):
        tiktok.sign_in(tmp_path / "t.json", "K", "S", client=script.client(), open_browser=wrong, timeout_s=10)

    def refused(url):
        state = dict(p.split("=", 1) for p in url.split("?", 1)[1].split("&"))["state"]
        httpx.get(f"{tiktok.REDIRECT_URI}?error=access_denied&state={state}", timeout=5)

    with pytest.raises(tiktok.TikTokError, match="access_denied"):
        tiktok.sign_in(tmp_path / "t.json", "K", "S", client=script.client(), open_browser=refused, timeout_s=10)
    assert not (tmp_path / "t.json").exists() and script.seen == []
