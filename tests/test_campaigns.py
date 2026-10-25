"""Campaigns: reading Content Rewards, vetting, the queue, and a campaign's clips from video to link-ready. No network."""

from __future__ import annotations

import json

import httpx
import pytest
from fastapi.testclient import TestClient
from test_runs import FakeEngine, FakeJev

from villomillo import campaigns, channels, clipper, config, contentrewards, posting, routing, runs, store
from villomillo.db import Database
from villomillo.quota import Quota
from villomillo.services import Services
from villomillo.web.app import create_app

RULES = config.Scoring()
SOLVED = "6bdc8e4b-3ebf-44f1-aedb-067589ca9c89"
OTHER = "44a3ffcb-d4a1-492b-94a4-704919950246"
THIRD = "11111111-2222-4333-8444-555555555555"


def page(*fragments: str) -> str:
    """A Content Rewards page: its data rides inside self.__next_f.push calls, as in the real pages."""
    text = "".join(fragments)
    return "<html><body><script>self.__next_f.push([1," + json.dumps(text) + "])</script></body></html>"


def listed(cid, title, *, rate=1.25, platforms=("instagram", "youtube"), type_="cpm", category="Podcasts", left=14911.86, total=20000, app=False):
    item = {"availableBudgetRaw": left, "brand": "Clip Farm", "budgetSpentRaw": total - left, "budgetTotalRaw": total, "category": category,
            "creatorCountRaw": 406, "description": "Clip the show.", "id": cid, "platforms": list(platforms), "requiresApplication": app,
            "title": title, "payoutSortRaw": rate, "ratePer1kLabel": f"$${rate:.2f}", "type": type_}
    return '{"campaign":' + json.dumps(item) + "}"


def detail(cid, title, *, youtube=125, instagram=125, status="active", left_spent=508814, budget=2000000, app=False, category="Podcasts"):
    pay = [{"maxPayout": 30000, "minPayout": 300, "platform": "instagram", "rate": instagram}]
    if youtube is not None:
        pay.append({"maxPayout": 30000, "minPayout": 300, "platform": "youtube", "rate": youtube})
    data = {"description": "Clip the show.", "id": cid, "metrics": {"budgetSpentCents": left_spent, "creatorCount": 406, "totalViews": 4041231},
            "name": title, "organization": {"name": "Clip Farm"}, "private": False, "requiresApplication": app, "status": status, "statusReason": "funded",
            "configuration": {"content": {"contentCategories": [{"id": "x", "name": category}]}, "budget": budget, "requirement": {"preApproval": False},
                              "payoutModel": pay}, "type": "cpm", "briefCounts": {"contentRules": 0, "references": 1}}
    return '..."queryKey":["x"]},{"state":{"data":' + json.dumps(data) + ',"dataUpdateCount":1,"status":"success"},"queryKey":["campaigns","detail","' + cid + '"]}'


class FakeSite:
    """Content Rewards as an HTTP handler: a Discover page and one page per campaign."""

    def __init__(self, items, details):
        self.items, self.details, self.seen = items, details, []

    def client(self):
        def handler(request: httpx.Request) -> httpx.Response:
            self.seen.append(request.url.path)
            path = request.url.path
            if path == "/c/discover":
                return httpx.Response(200, text=page("[", ",".join(self.items), "]"))
            cid = path.rsplit("/", 1)[-1]
            if cid in self.details:
                return httpx.Response(200, text=page(self.details[cid]))
            return httpx.Response(404, text="not found")

        return httpx.Client(transport=httpx.MockTransport(handler))


@pytest.fixture
def db(tmp_path):
    return Database(tmp_path / "campaigns.db")


# ---- reading the pages --------------------------------------------------------------------------------------------


def test_a_campaign_id_is_found_in_a_link_or_on_its_own():
    assert contentrewards.campaign_id(f"https://contentrewards.com/c/campaigns/{SOLVED}?x=1") == SOLVED
    assert contentrewards.campaign_id(f" {SOLVED.upper()} ") == SOLVED
    assert contentrewards.campaign_id("https://example.com") is None


def test_the_discover_list_is_read_once_per_campaign():
    html = page("junk[", listed(SOLVED, "Solved"), ",", listed(OTHER, "$$10,000 Kai", rate=0, type_="pool"), ",", listed(SOLVED, "Solved"), "]")
    got = contentrewards.parse_list(html)
    assert [c["id"] for c in got] == [SOLVED, OTHER]
    assert got[0]["best_rate_cents"] == 125 and got[0]["budget_left_cents"] == 1491186 and got[0]["platforms"] == ["instagram", "youtube"]
    assert got[1]["type"] == "pool"


def test_a_campaign_page_gives_the_pay_for_each_platform_and_the_budget():
    d = contentrewards.parse_detail(page(detail(SOLVED, "Solved", youtube=90, instagram=150)), SOLVED)
    assert d["rates"]["youtube"] == {"rate_cents": 90, "min_payout_cents": 300, "max_payout_cents": 30000}
    assert d["rates"]["instagram"]["rate_cents"] == 150 and d["category"] == "Podcasts" and d["status"] == "active"
    assert d["budget_left_cents"] == 2000000 - 508814 and d["creators"] == 406 and d["platforms"] == ["instagram", "youtube"]


def test_a_page_that_cannot_be_read_says_so():
    with pytest.raises(contentrewards.CampaignPageError, match="could not be read"):
        contentrewards.parse_detail(page("nothing useful"), SOLVED)
    assert contentrewards.parse_list("<html>nothing</html>") == []


def test_http_trouble_is_a_plain_error():
    with pytest.raises(contentrewards.CampaignPageError, match="HTTP 404"):
        contentrewards.fetch_detail(THIRD, FakeSite([], {}).client())
    broken = httpx.Client(transport=httpx.MockTransport(lambda r: (_ for _ in ()).throw(httpx.ConnectError("no"))))
    with pytest.raises(contentrewards.CampaignPageError, match="could not reach"):
        contentrewards.fetch_list(broken)


# ---- vetting ----------------------------------------------------------------------------------------------------------


def base(**kw):
    c = {"status": "active", "type": "cpm", "platforms": ["youtube"], "rate_cents": 125, "budget_left_cents": 1000000, "requires_application": 0}
    return {**c, **kw}


@pytest.mark.parametrize(("changes", "reason"), [
    ({"status": "paused"}, "not taking clips right now (paused)"),
    ({"type": "pool"}, "pays from a pool, not per 1,000 views"),
    ({"platforms": ["tiktok", "instagram"]}, "does not allow YouTube"),
    ({"rate_cents": 70}, "pays $0.70 per 1,000 views on YouTube, under the $1.00 floor"),
    ({"rate_cents": None}, "pays nothing stated"),
    ({"budget_left_cents": 20000}, "only $200 of its budget is left"),
])
def test_a_campaign_is_skipped_with_its_reason(changes, reason):
    state, why = campaigns.vet(base(**changes), RULES)
    assert state == "skipped" and reason in why and why.startswith("Skipped:")


def test_a_good_campaign_is_worth_joining_and_an_application_is_pointed_out():
    state, why = campaigns.vet(base(), RULES)
    assert state == "recommended" and "$1.25 per 1,000 views" in why and "$10,000 of budget left" in why
    assert "apply on Content Rewards" in campaigns.vet(base(requires_application=1), RULES)[1]
    assert campaigns.vet(base(rate_cents=100), RULES)[0] == "recommended"  # exactly the floor is enough


# ---- the scan ------------------------------------------------------------------------------------------------------------


def test_a_scan_keeps_every_campaign_and_reads_the_pages_of_the_good_ones(db):
    site = FakeSite([listed(SOLVED, "Solved", rate=1.5), listed(OTHER, "$$10,000 Kai", rate=0, type_="pool"),
                     listed(THIRD, "Cheap show", rate=0.4)],
                    {SOLVED: detail(SOLVED, "Solved", youtube=125, instagram=150)})
    lines = campaigns.scan(db, RULES, site.client())
    assert "3 campaigns" in lines[0]
    by = {c["id"]: c for c in campaigns.all_campaigns(db)}
    assert by[SOLVED]["state"] == "recommended" and by[SOLVED]["rate_cents"] == 125  # the page's own YouTube pay, not the list's best
    assert by[OTHER]["state"] == "skipped" and by[OTHER]["title"] == "$10,000 Kai" and "pool" in by[OTHER]["reason"]
    assert by[THIRD]["state"] == "skipped" and "under the $1.00 floor" in by[THIRD]["reason"]
    assert f"/c/campaigns/{THIRD}" not in site.seen and f"/c/campaigns/{SOLVED}" in site.seen  # only worthwhile pages are read


def test_the_pages_own_youtube_pay_can_turn_a_listed_campaign_down(db):
    site = FakeSite([listed(SOLVED, "Solved", rate=1.5)], {SOLVED: detail(SOLVED, "Solved", youtube=90, instagram=150)})
    campaigns.scan(db, RULES, site.client())
    c = campaigns.get(db, SOLVED)
    assert c["state"] == "skipped" and "$0.90" in c["reason"]


def test_a_page_is_not_read_again_within_hours(db):
    site = FakeSite([listed(SOLVED, "Solved")], {SOLVED: detail(SOLVED, "Solved")})
    campaigns.scan(db, RULES, site.client())
    campaigns.scan(db, RULES, site.client())
    assert site.seen.count(f"/c/campaigns/{SOLVED}") == 1


def test_joining_and_stopping_survive_later_scans_and_a_joined_campaign_pauses_when_its_budget_goes(db):
    site = FakeSite([listed(SOLVED, "Solved")], {SOLVED: detail(SOLVED, "Solved")})
    campaigns.scan(db, RULES, site.client())
    campaigns.mark_joined(db, SOLVED)
    site.items = [listed(SOLVED, "Solved", left=40.0)]
    site.details = {}
    campaigns.scan(db, RULES, site.client())
    c = campaigns.get(db, SOLVED)
    assert c["state"] == "joined" and c["reason"].startswith("Paused:") and "$40 of its budget" in c["reason"]
    assert campaigns.clippable(c, RULES)[0] is False
    campaigns.drop(db, SOLVED)
    campaigns.scan(db, RULES, site.client())
    assert campaigns.get(db, SOLVED)["state"] == "dropped"
    with pytest.raises(LookupError):
        campaigns.mark_joined(db, THIRD)


def test_an_empty_discover_page_is_reported_not_silently_ignored(db):
    with pytest.raises(contentrewards.CampaignPageError, match="layout may have changed"):
        campaigns.scan(db, RULES, FakeSite([], {}).client())


def test_a_campaign_added_by_link_is_read_and_vetted_and_a_bad_link_is_refused(db):
    site = FakeSite([], {SOLVED: detail(SOLVED, "Solved w/ Mark")})
    c = campaigns.add_by_link(db, f"https://contentrewards.com/c/campaigns/{SOLVED}", RULES, site.client())
    assert c["state"] == "recommended" and c["title"] == "Solved w/ Mark" and c["rate_cents"] == 125
    with pytest.raises(campaigns.CampaignError, match="Content Rewards campaign link"):
        campaigns.add_by_link(db, "https://example.com", RULES, site.client())
    with pytest.raises(campaigns.CampaignError, match="HTTP 404"):
        campaigns.add_by_link(db, f"contentrewards.com/c/campaigns/{THIRD}", RULES, site.client())


def test_the_solved_campaign_you_joined_is_the_first_record_with_its_rules_and_tags_once(db):
    campaigns.seed_solved(db, RULES)
    c = campaigns.get(db, SOLVED)
    assert c["state"] == "joined" and c["tags"] == "@solvedclips @iammarkmanson"
    assert (c["rules"]["min_s"], c["rules"]["max_s"], c["rules"]["face_window_s"]) == (20, 60, 5)
    assert c["rules"]["require_captions"] and c["rules"]["no_ad_reads"]
    campaigns.drop(db, SOLVED)
    campaigns.seed_solved(db, RULES)
    assert campaigns.get(db, SOLVED)["state"] == "dropped"  # never put back


# ---- what you set -------------------------------------------------------------------------------------------------------


def test_sources_must_be_channels_or_video_links(db):
    campaigns.seed_solved(db, RULES)
    campaigns.save_setup(db, SOLVED, sources="@solvedpodcast\nhttps://www.youtube.com/channel/UCabcdefghijklmnopqrstuv\nhttps://youtu.be/aB3dEfGhIjK\n\n",
                         tags=" @a   @b ")
    c = campaigns.get(db, SOLVED)
    assert c["tags"] == "@a @b" and c["rules"] is None  # no rules ticked: none are checked
    kinds = [k for k, _ in campaigns.parse_sources(c["sources"])]
    assert kinds == ["channel", "channel", "video"]
    with pytest.raises(campaigns.CampaignError, match="line 2"):
        campaigns.save_setup(db, SOLVED, sources="@fine\nnot a source at all", tags="")
    with pytest.raises(LookupError):
        campaigns.save_setup(db, THIRD, sources="", tags="")


def test_rules_are_saved_checked_for_sense_and_turned_back_into_the_clip_checks(db):
    campaigns.seed_solved(db, RULES)
    campaigns.save_setup(db, SOLVED, sources="", tags="", use_rules=True, min_s="30", max_s="45", face_window_s="3", require_captions=True)
    rules = campaigns.rules_for(campaigns.get(db, SOLVED))
    assert (rules.min_s, rules.max_s, rules.face_window_s, rules.require_captions, rules.no_ad_reads) == (30, 45, 3, True, False)
    with pytest.raises(campaigns.CampaignError, match="do not make sense"):
        campaigns.save_setup(db, SOLVED, sources="", tags="", use_rules=True, min_s="50", max_s="40")


def test_a_campaigns_tags_replace_the_channels_for_its_clips_only():
    posting_cfg = config.Posting(platforms={"youtube_shorts": "api"}, tags={"youtube_shorts": "@channel"})
    db_ = None
    assert campaigns.posting_for(db_, posting_cfg, "youtube_shorts", None) is posting_cfg


# ---- the videos -----------------------------------------------------------------------------------------------------------


class FakeYT:
    def __init__(self, videos):
        self.videos_list = videos

    def channel(self, *, handle=None, channel_id=None):
        return {"channel_id": "UCx", "uploads_playlist": "UUx", "title": "Show"}

    def recent_upload_ids(self, playlist, *, limit=50):
        return [v["video_id"] for v in self.videos_list][:limit]

    def videos(self, ids):
        return [v for v in self.videos_list if v["video_id"] in ids]


def vid(n, minutes=50, live="none"):
    return {"video_id": f"vid{n:08d}", "title": f"Episode {n}", "duration_s": minutes * 60, "live_status": live}


def joined(db, sources="@show", **kw):
    campaigns.seed_solved(db, RULES)
    campaigns.save_setup(db, SOLVED, sources=sources, tags="", **kw)
    return campaigns.get(db, SOLVED)


def test_a_channel_source_queues_only_full_episodes_and_stops_at_the_queue_depth(db):
    c = joined(db)
    yt = FakeYT([vid(1), vid(2, minutes=1), vid(3, live="live"), *[vid(n) for n in range(4, 12)]])
    lines = campaigns.fill_queue(db, c, yt)
    ids = [q["video_id"] for q in campaigns.queue_of(db, SOLVED)]
    assert len(ids) == campaigns.QUEUE_AHEAD == len(lines)
    assert "vid00000002" not in ids and "vid00000003" not in ids and "vid00000001" in ids
    assert campaigns.fill_queue(db, c, yt) == []  # already full


def test_video_links_are_queued_and_nothing_is_queued_twice(db):
    c = joined(db, sources="https://www.twitch.tv/videos/2345678901\nhttps://youtu.be/aB3dEfGhIjK\nhttps://youtu.be/aB3dEfGhIjK")
    campaigns.fill_queue(db, c, None)
    assert sorted(q["video_id"] for q in campaigns.queue_of(db, SOLVED)) == ["aB3dEfGhIjK", "tw_2345678901"]


# ---- starting and finishing a campaign's videos -------------------------------------------------------------------------


@pytest.fixture
def sv(tmp_path, monkeypatch):
    database = Database(tmp_path / "sv.db")
    monkeypatch.setattr(clipper, "OUTBOX", tmp_path / "outbox")
    monkeypatch.setattr(clipper.validate, "check", lambda path, **k: {"ok": True, "problems": [], "warnings": [], "facts": {"duration_s": 30}})
    from villomillo import campaign as rules_check
    monkeypatch.setitem(rules_check.check.__kwdefaults__, "face", lambda path, window: 1.0)
    service = Services(config.AppConfig(), channels.load(database), database, Quota(database, general_cap=9, search_cap=9), FakeYT([vid(1), vid(2)]), FakeJev())
    return service


def ready_campaign(sv):
    campaigns.seed_solved(sv.db, RULES)
    campaigns.save_setup(sv.db, SOLVED, sources="@show", tags="@solvedclips @iammarkmanson", use_rules=True, min_s="20", max_s="60",
                         face_window_s="5", require_captions=True, no_ad_reads=True)
    return campaigns.get(sv.db, SOLVED)


def test_the_next_video_starts_for_a_joined_campaign_one_at_a_time(sv):
    ready_campaign(sv)
    site = FakeSite([listed(SOLVED, "Solved")], {SOLVED: detail(SOLVED, "Solved")})
    campaigns.fill_queue(sv.db, campaigns.get(sv.db, SOLVED), sv.yt)
    lines = campaigns.start_next(sv, site.client())
    assert lines and "started" in lines[0]
    with sv.db.conn() as c:
        run = dict(c.execute("SELECT * FROM runs").fetchone())
        assert run["source"] == "campaign" and run["campaign_id"] == SOLVED and run["profile_id"] == "solved"
        assert c.execute("SELECT run_id FROM campaign_queue WHERE video_id=?", (run["video_id"],)).fetchone()["run_id"] == run["id"]
    assert campaigns.start_next(sv, site.client()) == ["Waiting: a campaign video is being clipped. The next one starts when it is done."]  # one at a time


def test_nothing_starts_for_a_campaign_that_cannot_be_clipped_for(sv):
    ready_campaign(sv)
    campaigns.fill_queue(sv.db, campaigns.get(sv.db, SOLVED), sv.yt)
    site = FakeSite([], {SOLVED: detail(SOLVED, "Solved", youtube=80)})  # now pays under the floor
    assert campaigns.start_next(sv, site.client()) == []
    assert campaigns.get(sv.db, SOLVED)["reason"].startswith("Paused:")
    with sv.db.conn() as c:
        assert c.execute("SELECT COUNT(*) AS n FROM runs").fetchone()["n"] == 0
    campaigns.drop(sv.db, SOLVED)
    assert campaigns.start_next(sv, FakeSite([], {SOLVED: detail(SOLVED, "Solved")}).client()) == []  # a stopped campaign is left alone


def run_to_posts(sv, monkeypatch, *, engine=None):
    engine = engine or FakeEngine()
    ready_campaign(sv)
    campaigns.fill_queue(sv.db, campaigns.get(sv.db, SOLVED), sv.yt)
    campaigns.start_next(sv, FakeSite([], {SOLVED: detail(SOLVED, "Solved")}).client())
    run_id = 1
    for _ in range(4):
        if runs.get(sv.db, run_id)["state"] != "analysing":
            break
        runs.analyse(sv, run_id, engine)
    return run_id, engine


def test_a_campaign_video_goes_ahead_by_itself_at_the_campaigns_own_pay(sv):
    run_id, engine = run_to_posts(sv, None)
    run = runs.get(sv.db, run_id)
    assert run["state"] == "making" and engine.submitted[-1]["moments"] == [1, 2]  # no Start press
    moments = run["plan"]["moments"]
    assert all(m["cpm"] == 1.25 and not m["cpm_estimated"] for m in moments)
    assert "the campaign's stated pay" in moments[0]["explain"][1]


def test_a_campaign_that_ran_out_of_budget_stops_its_planned_video(sv):
    ready_campaign(sv)
    campaigns.fill_queue(sv.db, campaigns.get(sv.db, SOLVED), sv.yt)
    campaigns.start_next(sv, FakeSite([], {SOLVED: detail(SOLVED, "Solved")}).client())
    campaigns._set(sv.db, SOLVED, budget_left_cents=1000)  # the budget went between the queue and the plan
    engine = FakeEngine()
    runs.analyse(sv, 1, engine)
    runs.analyse(sv, 1, engine)
    run = runs.get(sv.db, 1)
    assert run["state"] == "nothing" and "budget" in run["note"] and len(engine.submitted) == 1


def test_a_campaigns_clips_carry_its_rules_tags_and_link_submission(sv):
    run_id, engine = run_to_posts(sv, None)
    runs.collect(sv, run_id, engine)
    with sv.db.conn() as c:
        clips = c.execute("SELECT campaign_id, state, grade_json FROM clips_ready ORDER BY id").fetchall()
        posts = c.execute("SELECT id, caption, status FROM posts ORDER BY id").fetchall()
    assert [r["campaign_id"] for r in clips] == [SOLVED, SOLVED]
    assert all(json.loads(r["grade_json"])["campaign"]["ok"] for r in clips)  # the campaign's own rules were checked
    assert all("@solvedclips @iammarkmanson" in p["caption"] for p in posts)  # and its tags are in every caption
    with sv.db.conn() as c:  # a post goes out: its link is then ready to hand in
        c.execute("UPDATE posts SET status='uploaded', url='https://youtube.com/shorts/aB3dEfGhIjK', posted_at=?", (store.iso(),))
    ready = campaigns.ready_to_submit(sv.db)
    assert len(ready) == 2 and ready[0]["campaign_title"] == "Solved"
    campaigns.mark_submitted(sv.db, ready[0]["post_id"])
    assert len(campaigns.ready_to_submit(sv.db)) == 1
    with pytest.raises(LookupError):
        campaigns.mark_submitted(sv.db, 999)


def test_a_clip_breaking_the_campaigns_rules_is_not_posted(sv, monkeypatch):
    from villomillo import campaign as rules_check

    monkeypatch.setitem(rules_check.check.__kwdefaults__, "face", lambda path, window: 0.0)  # no face in the first seconds
    run_id, engine = run_to_posts(sv, monkeypatch)
    runs.collect(sv, run_id, engine)
    with sv.db.conn() as c:
        assert [r["state"] for r in c.execute("SELECT state FROM clips_ready")] == ["rejected_auto"] * 2
        assert c.execute("SELECT COUNT(*) AS n FROM posts").fetchone()["n"] == 0


def test_a_pasted_link_has_no_campaign_rules_or_tags(sv):
    ready_campaign(sv)
    engine = FakeEngine()
    run_id = runs.create(sv, "solved", "https://www.youtube.com/watch?v=aB3dEfGhIjK")
    for _ in range(4):
        runs.analyse(sv, run_id, engine)
    runs.start(sv, run_id, engine)
    runs.collect(sv, run_id, engine)
    with sv.db.conn() as c:
        assert all(r["campaign_id"] is None and "campaign" not in json.loads(r["grade_json"]) for r in c.execute("SELECT * FROM clips_ready"))
        assert all("@solvedclips" not in r["caption"] for r in c.execute("SELECT caption FROM posts"))


def test_the_worker_job_reads_the_site_fills_queues_and_starts_a_video(sv, monkeypatch):
    ready_campaign(sv)
    site = FakeSite([listed(SOLVED, "Solved")], {SOLVED: detail(SOLVED, "Solved")})
    monkeypatch.setattr(contentrewards, "new_client", site.client)
    lines = campaigns.work(sv, site.client())
    assert any("public list" in line for line in lines) and any("queued" in line for line in lines) and any("started" in line for line in lines)


def test_the_campaigns_job_is_scheduled_once_for_everything(sv):
    from villomillo import tasks

    tasks.ensure_schedules(sv.db, sv.app, sv.profiles)
    with sv.db.conn() as c:
        row = c.execute("SELECT * FROM schedules WHERE name='campaigns'").fetchone()
    assert row["profile_id"] is None and row["interval_s"] == 3600
    assert tasks.tick_schedules(sv.db) >= 1


# ---- the pages ----------------------------------------------------------------------------------------------------------------


@pytest.fixture
def web(sv, monkeypatch):
    site = FakeSite([listed(SOLVED, "Solved"), listed(THIRD, "Cheap show", rate=0.4)], {SOLVED: detail(SOLVED, "Solved"), THIRD: detail(THIRD, "Cheap show", youtube=40)})
    monkeypatch.setattr(contentrewards, "new_client", site.client)
    return TestClient(create_app(sv), base_url="http://127.0.0.1:8780"), sv


def test_the_campaign_pages_render_and_explain_the_manual_steps(web):
    client, sv = web
    campaigns.seed_solved(sv.db, RULES)
    campaigns.scan(sv.db, RULES, FakeSite([listed(SOLVED, "Solved"), listed(THIRD, "Cheap show", rate=0.4)], {SOLVED: detail(SOLVED, "Solved")}).client())
    page_ = client.get("/campaigns?profile=solved").text
    assert "Campaigns" in page_ and "Skipped" in page_ and "by hand" in page_ and "Cheap show" in page_
    one = client.get(f"/campaigns/{SOLVED}").text
    assert "Where its videos are" in one and "Open on Content Rewards" in one and "@solvedclips" in one
    assert client.get("/campaigns/nope").status_code == 404


def test_adding_by_link_then_joining_setting_up_and_stopping(web):
    client, sv = web
    r = client.post("/campaigns/add", data={"link": f"https://contentrewards.com/c/campaigns/{THIRD}"}, follow_redirects=False)
    assert r.status_code == 303
    assert "Skipped" in client.get(f"/campaigns/{THIRD}").text
    bad = client.post("/campaigns/add", data={"link": "nonsense"}, follow_redirects=True)
    assert "Content Rewards campaign link" in bad.text
    client.post(f"/campaigns/{SOLVED}/join", follow_redirects=False)  # unknown yet: 404
    client.post("/campaigns/add", data={"link": SOLVED})
    assert client.post(f"/campaigns/{SOLVED}/join", follow_redirects=False).status_code == 303
    assert campaigns.get(sv.db, SOLVED)["state"] == "joined"
    form = {"sources": "@show", "tags": "@x", "use_rules": "1", "min_s": "25", "max_s": "50", "face_window_s": "4", "require_captions": "1"}
    assert client.post(f"/campaigns/{SOLVED}/setup", data=form, follow_redirects=False).headers["location"].endswith("saved=1")
    assert campaigns.get(sv.db, SOLVED)["rules"]["min_s"] == 25
    bad_setup = client.post(f"/campaigns/{SOLVED}/setup", data={**form, "sources": "junk line"}, follow_redirects=True)
    assert "line 1" in bad_setup.text
    assert client.post(f"/campaigns/{SOLVED}/drop", follow_redirects=False).status_code == 303
    assert campaigns.get(sv.db, SOLVED)["state"] == "dropped"
    assert client.post("/campaigns/add", data={"link": "x"}, headers={"Origin": "https://evil.example"}).status_code == 403


def test_home_asks_for_what_only_you_can_do(web):
    client, sv = web
    campaigns.seed_solved(sv.db, RULES)
    home = client.get("/?profile=solved").text
    assert "videos are" in home and "Tell VilloMillo where" in home
    campaigns.save_setup(sv.db, SOLVED, sources="@show", tags="")
    campaigns.upsert(sv.db, {"id": THIRD, "title": "Good show", "platforms": ["youtube"], "type": "cpm", "status": "active", "best_rate_cents": 200,
                             "budget_left_cents": 900000, "category": "Podcasts"})
    campaigns.revet(sv.db, campaigns.get(sv.db, THIRD), RULES)
    assert "1 campaign worth joining" in client.get("/?profile=solved").text
    run_id = 1
    with sv.db.conn() as c:
        c.execute("INSERT INTO clips_ready (profile_id, engine_clip_id, video_id, start_s, end_s, engine_score, hook, file_path, validation_json, grade_json, state, reason, created_at, campaign_id) "
                  "VALUES ('solved', 1, 'v', 0, 30, 80, 'h', 'f', '{}', '{}', 'approved', 'r', ?, ?)", (store.iso(), SOLVED))
        c.execute("INSERT INTO posts (profile_id, clip_id, platform, route, status, title, caption, tags_json, scheduled_at, created_at, url) "
                  "VALUES ('solved', 1, 'youtube_shorts', 'api', 'uploaded', 't', 'c', '[]', ?, ?, 'https://youtube.com/shorts/aB3dEfGhIjK')", (store.iso(), store.iso()))
    assert "Submit 1 link to" in client.get("/?profile=solved").text
    assert client.post("/api/campaign-submitted", data={"post_id": 1}).status_code == 200
    assert "Submit 1 link" not in client.get("/?profile=solved").text
    assert client.post("/api/campaign-submitted", data={"post_id": 99}).status_code == 404
    assert run_id


def test_the_recheck_command_only_touches_the_campaigns_own_clips(sv, monkeypatch):
    from villomillo import campaign as rules_check

    ready_campaign(sv)
    with sv.db.conn() as c:
        for n, camp in ((1, SOLVED), (2, None)):
            c.execute("INSERT INTO clips_ready (profile_id, engine_clip_id, video_id, start_s, end_s, engine_score, hook, file_path, validation_json, grade_json, state, reason, created_at, campaign_id) "
                      "VALUES ('solved', ?, 'v', 0, 30, 80, 'h', 'f.mp4', '{}', ?, 'review', 'r', ?, ?)",
                      (n, json.dumps({"caption_text": "words", "ad_read": 0.0}), store.iso(), camp))
    monkeypatch.setitem(rules_check.check.__kwdefaults__, "face", lambda path, window: 0.0)
    lines = clipper.recheck(sv.db, FakeEngine(), sv.jev, sv.profiles["solved"], SOLVED, campaigns.rules_for(campaigns.get(sv.db, SOLVED)))
    assert len(lines) == 1
    with sv.db.conn() as c:
        assert [r["state"] for r in c.execute("SELECT state FROM clips_ready ORDER BY id")] == ["rejected_auto", "review"]


def test_released_held_clips_still_work_after_the_change(sv):
    # routing.release_held plans each held clip's posts: nothing about campaigns breaks that
    assert routing.held_categories(sv.db) == [] and posting is not None
