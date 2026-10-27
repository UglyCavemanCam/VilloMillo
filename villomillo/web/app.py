"""The dashboard: Home, Research, Clips, Calendar, Accounts, Insights and Settings.

It only answers requests addressed to 127.0.0.1 or localhost, and every change (a decision, a pause, a choice) refuses a
request that comes from a page on any other site. Pages are server-rendered from template files; see pages.py for the
data each one shows and docs/DASHBOARD-REDESIGN.md for the design.
"""

from __future__ import annotations

import json
import pathlib
import re
import subprocess
import tempfile
import threading
from datetime import date
from html import escape as html_escape
from urllib.parse import quote, urlsplit

from fastapi import Body, FastAPI, Form, HTTPException, Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, FileSystemLoader
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .. import (
    accounts,
    activity,
    campaigns,
    claude,
    channels,
    clipper,
    copywriter,
    engine_manager,
    links,
    posting,
    relook,
    removal,
    research,
    rewards,
    routing,
    runs,
    safety,
    secrets,
    spend,
    store,
    tasks,
    timefmt,
)
from ..formats import assets as format_assets
from ..formats import font_uploads as format_font_uploads
from ..formats import placement as format_placement
from ..formats import music as format_music
from ..formats import designs as format_designs
from ..formats import live as format_live
from ..formats import preview as format_preview
from ..formats import templates as format_templates
from ..services import Services
from . import pages

LOCAL_HOSTS = {"127.0.0.1", "localhost"}
HERE = pathlib.Path(__file__).parent
THUMBS = pathlib.Path(clipper.OUTBOX).parent / "thumbs"
PREVIEWS = pathlib.Path(clipper.OUTBOX).parent / "previews"


def _only_local(request: Request, what: str) -> None:
    origin = request.headers.get("origin")
    if origin and urlsplit(origin).hostname not in LOCAL_HOSTS:
        raise HTTPException(403, f"{what} only come from this dashboard")


def _thumbnail(source: pathlib.Path, clip_id: int) -> pathlib.Path | None:
    """A small still from the clip, made once and kept."""
    dest = THUMBS / f"{clip_id}.jpg"
    if dest.exists() and dest.stat().st_mtime >= source.stat().st_mtime:
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    from core import binaries

    try:
        run = subprocess.run([binaries.ffmpeg(), "-y", "-v", "error", "-ss", "1.0", "-i", str(source), "-frames:v", "1",
                              "-vf", "scale=360:-2", "-q:v", "4", str(dest)], capture_output=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None
    return dest if run.returncode == 0 and dest.exists() else None


def create_app(sv: Services) -> FastAPI:
    app = FastAPI(title="VilloMillo", docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=sorted(LOCAL_HOSTS))
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    env = Environment(loader=FileSystemLoader(HERE / "templates"), autoescape=True)
    env.filters.update(clock=timefmt.clock, day=timefmt.day, short=timefmt.short, full=timefmt.full, ago=timefmt.ago,
                       money=pages.money, mmss=pages.mmss)

    def profile_or_404(profile_id: str | None):
        pid = profile_id or next(iter(sv.profiles), None)
        if pid not in sv.profiles:
            raise HTTPException(404, "no such channel profile")
        return sv.profiles[pid]

    def render(name: str, p, path: str, active: str, title: str, **ctx) -> HTMLResponse:
        return HTMLResponse(env.get_template(name).render(
            shell=pages.shell(sv, p, path, active), page={"title": title, "active": active}, **ctx))

    def fragment(name: str, **ctx) -> HTMLResponse:
        return HTMLResponse(env.get_template(name).render(**ctx))

    def jev_factory():
        from ..jev import Jev

        return sv.jev or Jev(secrets.get(secrets.JEV))

    def in_profile(profile: str, platform: str | None = None):
        if profile not in sv.profiles:
            raise HTTPException(404, "no such channel profile")
        if platform is not None and (sv.profiles[profile].posting is None or platform not in sv.profiles[profile].posting.platforms):
            raise HTTPException(404, "no such channel account")
        return sv.profiles[profile]

    # ---- errors: a browser gets a page inside the dashboard, everything else gets the JSON it always did ----------

    def wants_page(request: Request) -> bool:
        return (request.method == "GET" and "text/html" in request.headers.get("accept", "")
                and not request.url.path.startswith(("/api/", "/static/")) and request.headers.get("x-requested-with") != "panel")

    def error_page(request: Request, status: int, headline: str, text: str) -> HTMLResponse:
        p = sv.profiles.get(request.query_params.get("profile", "")) or next(iter(sv.profiles.values()), None)
        try:
            body = env.get_template("error.html").render(
                shell=pages.shell(sv, p, request.url.path, ""), page={"title": headline, "active": ""},
                status=status, headline=headline, text=text, home=f"/?profile={p.id}")
        except Exception:  # the page that explains a failure must not fail too
            body = f"<!doctype html><title>{status} | VilloMillo</title><h1>{html_escape(headline)}</h1><p>{html_escape(text)}</p>"
        return HTMLResponse(body, status_code=status)

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException):
        if not wants_page(request):
            return await http_exception_handler(request, exc)
        known = {404: ("That page is not here", "The address may be old, or the clip or post was removed."),
                 403: ("Not allowed", "This dashboard only answers requests from this computer.")}
        headline, text = known.get(exc.status_code, ("Something went wrong", str(exc.detail)))
        return error_page(request, exc.status_code, headline, text)

    @app.exception_handler(Exception)
    async def crashed(request: Request, exc: Exception):
        store.event(sv.db, "error", "dashboard", f"{request.url.path}: {type(exc).__name__}: {str(exc)[:200]}")
        if not wants_page(request):
            return JSONResponse({"detail": "the dashboard hit an error; it is in the event log"}, status_code=500)
        return error_page(request, 500, "The dashboard hit an error",
                          f"{type(exc).__name__}: {str(exc)[:200]}. It was written to the event log; nothing was changed.")

    # ---- pages ------------------------------------------------------------------------------------------------

    @app.get("/health")
    def health():
        return {"ok": True}

    @app.get("/", response_class=HTMLResponse)
    def home(profile: str | None = None, problem: str = "", again: str = ""):
        p = profile_or_404(profile)
        return render("home.html", p, "/", "home", "Home", d={**pages.home_page(sv, p), "problem": problem, "again": again})

    # ---- channels ----------------------------------------------------------------------------------------------

    connecting: set[str] = set()  # channels whose YouTube sign-in window is open right now

    def finish_channel_change(category: str | None = None) -> int:
        """After a channel is added or changed: schedule its jobs, and plan the clips that were held for its category."""
        tasks.ensure_schedules(sv.db, sv.app, sv.profiles)
        if not category:
            return 0
        planned = routing.release_held(sv.db, jev_factory, sv.profiles, category)
        for post in planned:
            if post["status"] == "queued":
                tasks.enqueue(sv.db, "copy", {"profile": post["profile"], "post_id": post["id"]}, dedupe_key=f"copy:{post['id']}")
        return len(planned)

    @app.get("/channels", response_class=HTMLResponse)
    def channels_list(profile: str | None = None):
        p = profile_or_404(profile)
        return render("channels.html", p, "/channels", "channels", "Channels", d=pages.channels_page(sv, p))

    @app.post("/channels/catch-all")
    def channels_catch_all(on: str = Form("0"), channel: str = Form("")):
        """The switch at the top of Channels: on sends every clip to one channel; off sends each to its own category's channel."""
        turn_on = on == "1"
        try:
            channels.set_catch_all(sv.db, sv.profiles, on=turn_on, channel=channel or None)
        except channels.ChannelError as e:
            raise HTTPException(400, str(e))
        released = 0
        if turn_on:  # clips held for a missing category channel now have somewhere to go
            for h in routing.held_categories(sv.db):
                released += finish_channel_change(h["category"])
        else:
            tasks.ensure_schedules(sv.db, sv.app, sv.profiles)
        return RedirectResponse(f"/channels?released={released}", status_code=303)

    @app.get("/channels/new", response_class=HTMLResponse)
    def channel_new(profile: str | None = None, category: str = ""):
        p = profile_or_404(profile)
        return render("channel_new.html", p, "/channels", "channels", "Add channel",
                      d={"form": {"name": category, "category": category, "about": ""}, "error": None})

    @app.post("/channels/new", response_class=HTMLResponse)
    def channel_create(request: Request, name: str = Form(""), category: str = Form(""), about: str = Form("")):
        _only_local(request, "changes")
        try:
            c = channels.create(sv.db, sv.profiles, name=name, category=category, about=about)
        except channels.ChannelError as e:
            r = render("channel_new.html", profile_or_404(None), "/channels", "channels", "Add channel",
                       d={"form": {"name": name, "category": category, "about": about}, "error": str(e)})
            r.status_code = 422
            return r
        released = finish_channel_change(c.category)
        store.event(sv.db, "info", "channels", f"Channel {c.display_name} added" + (f"; {released} held clip(s) planned" if released else ""))
        return RedirectResponse(f"/channels/{c.id}?profile={c.id}&new=1&released={released}", status_code=303)

    @app.get("/channels/{channel_id}", response_class=HTMLResponse)
    def channel_detail(channel_id: str, new: int = 0, released: int = 0, saved: int = 0):
        c = in_profile(channel_id)
        d = pages.channel_page(sv, c)
        d.update(new=bool(new), released=released, saved=bool(saved))
        return render("channel.html", c, "/channels", "channels", pages.channel_name(c), d=d)

    @app.post("/channels/{channel_id}/save", response_class=HTMLResponse)
    async def channel_save(request: Request, channel_id: str):
        _only_local(request, "changes")
        c = in_profile(channel_id)
        form = {k: str(v) for k, v in (await request.form()).items()}
        allowed = {"name", "category", "about", "brand_handle", "youtube_tags", "youtube_daily_cap", "youtube_route", "dry_run", "privacy", "upload_size", "min_gap_minutes", "max_gap_minutes", "auto_post_min_score"}
        before = c.category
        try:
            updated = channels.update(sv.db, sv.profiles, channel_id, **{k: v for k, v in form.items() if k in allowed})
        except channels.ChannelError as e:
            d = pages.channel_page(sv, c, form=form, error=str(e))
            d.update(new=False, released=0, saved=False)
            r = render("channel.html", c, "/channels", "channels", pages.channel_name(c), d=d)
            r.status_code = 422
            return r
        released = finish_channel_change(updated.category if updated.category != before else None)
        if (updated.posting.min_gap_minutes, updated.posting.max_gap_minutes) != (c.posting.min_gap_minutes, c.posting.max_gap_minutes):
            posting.respace_queued(sv.db, updated)  # the posts already waiting follow the new gap too
        return RedirectResponse(f"/channels/{channel_id}?profile={channel_id}&saved=1&released={released}", status_code=303)

    @app.post("/api/channel-privacy")
    def channel_privacy(request: Request, profile: str = Form(...), privacy: str = Form(...)):
        """The Public / Private switch on a channel's card: who can watch the clips it posts from now on."""
        _only_local(request, "changes")
        in_profile(profile)
        try:
            channels.update(sv.db, sv.profiles, profile, privacy=privacy)
        except channels.ChannelError as e:
            raise HTTPException(422, str(e)) from None
        store.event(sv.db, "info", "channels", f"{sv.profiles[profile].display_name}: new posts are {privacy}")
        return JSONResponse({"ok": True})

    @app.post("/api/channel-youtube")
    def channel_youtube(request: Request, profile: str = Form(...)):
        _only_local(request, "changes")
        c = in_profile(profile)
        if not posting.YOUTUBE_CLIENT.exists():
            raise HTTPException(422, f"save your Google OAuth client file as {posting.YOUTUBE_CLIENT} first")
        if profile in connecting:
            raise HTTPException(409, "a sign-in window is already open for this channel")
        connecting.add(profile)

        def sign_in():
            try:
                info = posting.connect_youtube(c)
                store.event(sv.db, "info", "channels", f"{pages.channel_name(c)}: YouTube connected as {info.get('title')}")
            except Exception as e:  # window closed, wrong account, no network: say so in the log, change nothing
                store.event(sv.db, "warn", "channels", f"{pages.channel_name(c)}: YouTube sign-in did not finish: {str(e)[:200]}")
            finally:
                connecting.discard(profile)

        threading.Thread(target=sign_in, name=f"youtube-connect-{profile}", daemon=True).start()
        return JSONResponse({"ok": True})

    @app.post("/api/channel-delete")
    def channel_delete(request: Request, profile: str = Form(...)):
        _only_local(request, "changes")
        in_profile(profile)
        try:
            channels.delete(sv.db, sv.profiles, profile)
        except channels.ChannelError as e:
            raise HTTPException(422, str(e)) from None
        tasks.ensure_schedules(sv.db, sv.app, sv.profiles)
        return JSONResponse({"ok": True})

    # ---- research ------------------------------------------------------------------------------------------------

    @app.get("/runs/{run_id}/live", response_class=HTMLResponse)
    def run_live(run_id: int, profile: str | None = None):
        try:
            d = pages.run_page(sv, run_id)
        except LookupError:
            raise HTTPException(404, "no such video run") from None
        html = env.get_template("partials/timeline.html").render(d=d)
        return HTMLResponse(html, headers={"X-State": d["run"]["state"]})

    @app.get("/now", response_class=HTMLResponse)
    def now_panel(profile: str | None = None):
        return fragment("partials/now.html", d=pages.now_view(sv, profile_or_404(profile)))

    @app.get("/research/feed", response_class=HTMLResponse)
    def research_feed():
        return fragment("partials/feed.html", feed=pages.feed_lines(activity.recent(sv.db, "research", 25)))

    @app.get("/campaigns/feed", response_class=HTMLResponse)
    def campaigns_feed():
        return fragment("partials/feed.html", feed=pages.feed_lines(activity.recent(sv.db, "campaign", 25)))

    @app.get("/results", response_class=HTMLResponse)
    def results(profile: str | None = None):
        p = profile_or_404(profile)
        return render("results.html", p, "/results", "results", "Results", d=pages.results_page(sv, p))

    @app.get("/rules", response_class=HTMLResponse)
    def rules(profile: str | None = None):
        p = profile_or_404(profile)
        return render("rules.html", p, "/rules", "settings", "Scoring and safety rules", d=pages.rules_page(sv, p))

    @app.get("/research", response_class=HTMLResponse)
    def research_page(profile: str | None = None, problem: str = "", saved: int = 0):
        p = profile_or_404(profile)
        return render("research.html", p, "/research", "research", "Research", d={**pages.research_page(sv, p), "problem": problem, "saved": bool(saved)})

    @app.post("/research/settings")
    async def research_settings(request: Request):
        _only_local(request, "changes")
        form = await request.form()
        try:
            research.save_settings(sv.db, sv.app.research, sv.app.scoring, on=form.get("on") == "1" if "on" in form else None,
                                   videos_per_day=form.get("videos_per_day"), bar_max=form.get("bar_max"), topics=form.get("topics"))
        except research.ResearchError as e:
            return RedirectResponse(f"/research?problem={quote(str(e))}", status_code=303)
        return RedirectResponse("/research?saved=1", status_code=303)

    @app.post("/research/sources/add")
    def research_source_add(request: Request, platform: str = Form(...), channel: str = Form("")):
        _only_local(request, "changes")
        try:
            research.add_source(sv.db, platform, channel)
        except research.ResearchError as e:
            return RedirectResponse(f"/research?problem={quote(str(e))}", status_code=303)
        return RedirectResponse("/research?saved=1", status_code=303)

    @app.post("/research/sources/{source_id}/remove")
    def research_source_remove(source_id: int, request: Request):
        _only_local(request, "changes")
        research.remove_source(sv.db, source_id)
        return RedirectResponse("/research", status_code=303)

    @app.post("/research/look-now")
    def research_look_now(request: Request):
        _only_local(request, "changes")
        tasks.enqueue(sv.db, "research", {"profile": None}, dedupe_key=f"research:now:{int(store.now().timestamp() // 60)}", priority=10)
        return RedirectResponse("/research", status_code=303)

    # ---- campaigns -----------------------------------------------------------------------------------------------

    @app.post("/campaigns/switch")
    async def campaigns_switch(request: Request):
        _only_local(request, "changes")
        form = await request.form()
        campaigns.set_on(sv.db, form.get("on") == "1")
        return RedirectResponse("/campaigns", status_code=303)

    @app.get("/campaigns", response_class=HTMLResponse)
    def campaigns_list(profile: str | None = None, problem: str = "", rewards: str = "", n: int = 0):
        p = profile_or_404(profile)
        return render("campaigns.html", p, "/campaigns", "campaigns", "Campaigns", d={**pages.campaigns_page(sv, p), "problem": problem, "rewards_note": rewards, "rewards_n": n})

    @app.post("/campaigns/add")
    def campaign_add(request: Request, link: str = Form("")):
        _only_local(request, "changes")
        try:
            c = campaigns.add_by_link(sv.db, link, sv.app.scoring)
        except campaigns.CampaignError as e:
            return RedirectResponse(f"/campaigns?problem={quote(str(e))}", status_code=303)
        return RedirectResponse(f"/campaigns/{c['id']}", status_code=303)

    @app.post("/campaigns/rewards/login")
    def rewards_login(request: Request):
        """Open the sign-in window on this computer. You sign in there once; the app only keeps the browser's own session."""
        _only_local(request, "changes")
        started = rewards.start_login(sv.db)
        return RedirectResponse("/campaigns?rewards=" + ("opened" if started else "already"), status_code=303)

    @app.post("/campaigns/rewards/check")
    def rewards_check(request: Request):
        _only_local(request, "changes")
        tasks.enqueue(sv.db, "rewards_check", {}, dedupe_key=f"rewards_check:{int(store.now().timestamp() // 20)}", priority=10)
        return RedirectResponse("/campaigns?rewards=checking", status_code=303)

    @app.post("/campaigns/rewards/settings")
    async def rewards_settings(request: Request):
        _only_local(request, "changes")
        form = {k: str(v) for k, v in (await request.form()).items()}
        if "auto" in form:
            rewards.set_auto(sv.db, form["auto"] == "1")
        if "dry_run" in form:
            rewards.set_dry_run(sv.db, form["dry_run"] == "1")
        return RedirectResponse("/campaigns?rewards=saved", status_code=303)

    @app.post("/campaigns/rewards/submit-waiting")
    def rewards_submit_waiting(request: Request):
        """Hand in every posted campaign clip that has not been handed in yet, one at a time with random pauses."""
        _only_local(request, "changes")
        n = rewards.queue_waiting(sv.db) + rewards.retry_failed(sv.db)
        return RedirectResponse(f"/campaigns?rewards=queued&n={n}", status_code=303)

    @app.post("/campaigns/look-again")
    def campaigns_look_again(request: Request):
        _only_local(request, "changes")
        tasks.enqueue(sv.db, "campaigns", {"profile": None}, dedupe_key=f"campaigns:now:{int(store.now().timestamp() // 60)}", priority=10)
        return RedirectResponse("/campaigns", status_code=303)

    @app.get("/campaigns/{campaign_id}", response_class=HTMLResponse)
    def campaign_detail(campaign_id: str, profile: str | None = None, problem: str = "", saved: int = 0):
        d = pages.campaign_page(sv, campaign_id)
        if d is None:
            raise HTTPException(404, "no such campaign")
        return render("campaign.html", profile_or_404(profile), "/campaigns", "campaigns", d["c"]["title"][:60], d={**d, "problem": problem, "saved": bool(saved)})

    def campaign_action(campaign_id: str, request: Request, action):
        _only_local(request, "changes")
        try:
            action(sv.db, campaign_id)
        except LookupError:
            raise HTTPException(404, "no such campaign") from None
        return RedirectResponse(f"/campaigns/{campaign_id}", status_code=303)

    @app.post("/campaigns/{campaign_id}/join")
    def campaign_join(campaign_id: str, request: Request):
        return campaign_action(campaign_id, request, campaigns.mark_joined)

    @app.post("/campaigns/{campaign_id}/drop")
    def campaign_drop(campaign_id: str, request: Request):
        return campaign_action(campaign_id, request, campaigns.drop)

    @app.post("/campaigns/{campaign_id}/setup")
    async def campaign_setup(campaign_id: str, request: Request):
        _only_local(request, "changes")
        form = {k: str(v) for k, v in (await request.form()).items()}
        try:
            campaigns.save_setup(sv.db, campaign_id, sources=form.get("sources", ""), tags=form.get("tags", ""), min_s=form.get("min_s"),
                                 max_s=form.get("max_s"), face_window_s=form.get("face_window_s"), use_rules=form.get("use_rules") == "1", require_face=form.get("require_face") == "1",
                                 require_captions=form.get("require_captions") == "1", no_ad_reads=form.get("no_ad_reads") == "1")
        except LookupError:
            raise HTTPException(404, "no such campaign") from None
        except campaigns.CampaignError as e:
            return RedirectResponse(f"/campaigns/{campaign_id}?problem={quote(str(e))}", status_code=303)
        tasks.enqueue(sv.db, "campaigns", {"profile": None}, dedupe_key=f"campaigns:saved:{int(store.now().timestamp() // 60)}", priority=10)  # fill the queue now, not within the hour
        return RedirectResponse(f"/campaigns/{campaign_id}?saved=1", status_code=303)

    @app.post("/api/campaign-submitted")
    def campaign_submitted(request: Request, post_id: int = Form(...)):
        _only_local(request, "changes")
        try:
            campaigns.mark_submitted(sv.db, post_id)
        except LookupError as e:
            raise HTTPException(404, str(e)) from None
        return JSONResponse({"ok": True})

    # ---- pasted links --------------------------------------------------------------------------------------------

    @app.post("/paste")
    def paste(request: Request, link: str = Form(""), profile: str = Form(""), redo: str = Form("")):
        _only_local(request, "changes")
        p = profile_or_404(profile or None)
        try:
            run_id = runs.create(sv, p.id, link, redo=redo == "1")
        except links.LinkError as e:
            return RedirectResponse(f"/?profile={p.id}&problem={quote(str(e))}", status_code=303)
        except runs.AlreadyClipped as e:  # ask before clipping it a second time
            return RedirectResponse(f"/?profile={p.id}&problem={quote(e.why + '.')}&again={quote(link)}", status_code=303)
        run = runs.get(sv.db, run_id)
        if run["source"] != "paste":  # it was already being worked on by Research or a campaign: you are shown that video
            from .. import claims as _claims

            info = _claims.phrase(sv.db, run)
            return RedirectResponse(f"/runs/{run_id}?profile={p.id}&info={quote(info[0].upper() + info[1:] + '. Here it is.')}", status_code=303)
        return RedirectResponse(f"/runs/{run_id}?profile={p.id}", status_code=303)

    @app.get("/runs/{run_id}", response_class=HTMLResponse)
    def run_detail(run_id: int, profile: str | None = None, problem: str = "", info: str = ""):
        try:
            d = pages.run_page(sv, run_id)
        except LookupError:
            raise HTTPException(404, "no such video run") from None
        p = profile_or_404(profile)
        return render("run.html", p, "/", "home", d["row"]["title"][:60], d={**d, "problem": problem, "info": info})

    def run_action(run_id: int, request: Request, action):
        _only_local(request, "changes")
        try:
            action(sv, run_id)
        except LookupError:
            raise HTTPException(404, "no such video run") from None
        except (runs.RunError, clipper.EngineUnavailable) as e:
            return RedirectResponse(f"/runs/{run_id}?problem={quote(str(e))}", status_code=303)
        return RedirectResponse(f"/runs/{run_id}", status_code=303)

    @app.post("/runs/{run_id}/start")
    def run_start(run_id: int, request: Request):
        return run_action(run_id, request, runs.start)

    @app.post("/runs/{run_id}/retry")
    def run_retry(run_id: int, request: Request):
        return run_action(run_id, request, runs.retry)

    def _ids(text: str) -> list[int]:
        try:
            return [int(x) for x in text.split(",") if x.strip()][:200]
        except ValueError:
            raise HTTPException(422, "clip_ids must be numbers separated by commas") from None

    @app.get("/api/clips/impact")
    def clips_impact(profile: str, clip_ids: str = ""):
        in_profile(profile)
        return JSONResponse(removal.impact(sv.db, profile, _ids(clip_ids)))

    @app.post("/api/clips/remove")
    def clips_remove(request: Request, profile: str = Form(...), clip_ids: str = Form("")):
        _only_local(request, "changes")
        in_profile(profile)
        try:
            return JSONResponse({"ok": True, **removal.remove(sv.db, profile, _ids(clip_ids))})
        except removal.RemovalError as e:
            raise HTTPException(422, str(e)) from None

    @app.post("/api/clips/restore")
    def clips_restore(request: Request, profile: str = Form(...), clip_ids: str = Form("")):
        _only_local(request, "changes")
        in_profile(profile)
        try:
            return JSONResponse({"ok": True, "restored": removal.restore(sv.db, profile, _ids(clip_ids))})
        except removal.RemovalError as e:
            raise HTTPException(422, str(e)) from None

    @app.post("/runs/{run_id}/remove-clips")
    def run_remove_clips(run_id: int, request: Request):
        _only_local(request, "changes")
        try:
            run = runs.get(sv.db, run_id)
        except LookupError:
            raise HTTPException(404, "no such video run") from None
        ids = [c["id"] for c in runs.clips_of(sv.db, run)]
        try:
            removal.remove(sv.db, run["profile_id"], ids)
        except removal.RemovalError as e:
            return RedirectResponse(f"/runs/{run_id}?problem={quote(str(e))}", status_code=303)
        return RedirectResponse(f"/runs/{run_id}", status_code=303)

    @app.post("/runs/{run_id}/remove")
    def run_remove(run_id: int, request: Request):
        _only_local(request, "changes")
        try:
            runs.remove(sv, run_id)
        except LookupError:
            raise HTTPException(404, "no such video run") from None
        except runs.RunError as e:
            return RedirectResponse(f"/runs/{run_id}?problem={quote(str(e))}", status_code=303)
        return RedirectResponse("/", status_code=303)

    @app.post("/api/emergency-stop")
    def emergency_stop(request: Request):
        _only_local(request, "changes")
        safety.stop(sv.db)
        return JSONResponse({"ok": True})

    @app.post("/api/emergency-resume")
    def emergency_resume(request: Request):
        _only_local(request, "changes")
        safety.resume(sv.db)
        return JSONResponse({"ok": True})

    @app.get("/clips", response_class=HTMLResponse)
    def clips(profile: str | None = None, tab: str = "all", sort: str = "best", min_score: str = "0", video: str = "", post: str = "any"):
        p = profile_or_404(profile)
        score = int(min_score) if min_score.isdigit() else 0
        return render("clips.html", p, "/clips", "clips", "Clips", d=pages.clips_page(sv, p, tab, sort=sort, min_score=score, video=video, post=post))

    @app.get("/clips/{clip_id}/panel", response_class=HTMLResponse)
    def clip_panel(clip_id: int, profile: str):
        p = profile_or_404(profile)
        data = pages.clip_panel(sv, p, clip_id)
        if data is None:
            raise HTTPException(404, "no such clip")
        return fragment("partials/clip_panel.html", p=p, d=data)

    def clip_path(clip_id: int, profile: str) -> pathlib.Path:
        profile_or_404(profile)
        row = pages.one(sv.db, "SELECT file_path FROM clips_ready WHERE id=? AND (profile_id=? OR id IN "
                               "(SELECT clip_id FROM posts WHERE profile_id=?))", clip_id, profile, profile)
        if row is None:
            raise HTTPException(404, "no such clip")
        path = pathlib.Path(row["file_path"]).resolve()
        if clipper.OUTBOX.resolve() not in path.parents or not path.exists():
            raise HTTPException(404, "file not found")
        return path

    @app.get("/clips/{clip_id}/file")
    def clip_file(clip_id: int, profile: str):
        return FileResponse(clip_path(clip_id, profile), media_type="video/mp4")

    @app.get("/clips/{clip_id}/thumb")
    def clip_thumb(clip_id: int, profile: str):
        thumb = _thumbnail(clip_path(clip_id, profile), clip_id)
        if thumb is None:
            raise HTTPException(404, "no preview image")
        return FileResponse(thumb, media_type="image/jpeg", headers={"Cache-Control": "max-age=3600"})

    @app.get("/api/studio")
    def studio_data(profile: str):
        from .. import studio

        p = profile_or_404(profile)
        return JSONResponse(studio.build(sv.db, clipper.Engine().log, p.id))

    @app.get("/calendar", response_class=HTMLResponse)
    def calendar(profile: str | None = None, view: str = "month", date: str | None = None):
        p = profile_or_404(profile)
        try:
            ref = date_fromiso(date)
        except ValueError:
            ref = None
        return render("calendar.html", p, "/calendar", "calendar", "Calendar", d=pages.calendar_page(sv, p, view, ref))

    @app.get("/calendar/day/{day}", response_class=HTMLResponse)
    def day_panel(day: str, profile: str):
        p = profile_or_404(profile)
        try:
            when = date_fromiso(day)
        except ValueError:
            raise HTTPException(404, "no such day") from None
        return fragment("partials/day_panel.html", p=p, d=pages.day_panel(sv, p, when))

    @app.get("/calendar/post/{post_id}", response_class=HTMLResponse)
    def post_panel(post_id: int, profile: str, back: str = ""):
        p = profile_or_404(profile)
        data = pages.post_panel(sv, p, post_id)
        if data is None:
            raise HTTPException(404, "no such post")
        return fragment("partials/post_panel.html", p=p, d=data, back=back if back.startswith("/calendar/day/") else "")


    @app.get("/insights", response_class=HTMLResponse)
    def insights(profile: str | None = None):
        p = profile_or_404(profile)
        return render("insights.html", p, "/insights", "insights", "Insights", d=pages.insights_page(sv, p))

    @app.get("/settings", response_class=HTMLResponse)
    def settings(profile: str | None = None, engine_message: str = ""):
        p = profile_or_404(profile)
        return render("settings.html", p, "/settings", "settings", "Settings", d={**pages.settings_page(sv, p), "engine_message": engine_message})

    @app.post("/settings/moment-ai")
    def moment_ai_switch(request: Request, use_claude: str = Form("1")):
        from .. import engine_ai

        _only_local(request, "changes")
        engine_ai.save(sv.db, use_claude=use_claude == "1")
        if use_claude == "1":
            try:
                engine_ai.sync_key()
            except Exception:  # shown below as "no key" when it cannot be given
                pass
        message = engine_manager.manager.restart() if engine_manager.manager.supervised else "Saved. Restart the app so the clip engine starts with it."
        return RedirectResponse(f"/settings?engine_message={quote(message)}#moment-ai", status_code=303)

    @app.post("/api/engine/{action}")
    def engine_action(action: str, request: Request):
        _only_local(request, "changes")
        if action not in ("start", "restart"):
            raise HTTPException(404, "no such action")
        message = getattr(engine_manager.manager, action)()
        return RedirectResponse(f"/settings?engine_message={quote(message)}#engine", status_code=303)

    @app.get("/engine/log", response_class=HTMLResponse)
    def engine_log(profile: str | None = None):
        path = engine_manager.manager.log_path
        return render("engine_log.html", profile_or_404(profile), "/settings", "settings", "Clip engine log",
                      d={"text": engine_manager.tail(path), "path": str(path)})

    @app.get("/api/search")
    def search(profile: str, q: str = ""):
        p = profile_or_404(profile)
        return JSONResponse({"results": pages.search(sv, p, q) if len(q.strip()) >= 2 else []})

    # ---- old addresses keep working ---------------------------------------------------------------------------

    def moved(target: str):
        def go(profile: str | None = None):
            return RedirectResponse(f"{target}{'&' if '?' in target else '?'}profile={profile_or_404(profile).id}", status_code=307)
        return go

    app.get("/accounts", include_in_schema=False)(moved("/channels"))
    app.get("/studio", include_in_schema=False)(moved("/clips?tab=progress"))
    app.get("/review", include_in_schema=False)(moved("/clips?tab=review"))
    app.get("/posts", include_in_schema=False)(moved("/calendar?view=list"))
    app.get("/manual", include_in_schema=False)(moved("/calendar?view=list"))
    app.get("/learning", include_in_schema=False)(moved("/insights"))

    # ---- changes ----------------------------------------------------------------------------------------------

    @app.post("/api/clip-decision")
    def clip_decision(request: Request, profile: str = Form(...), clip_id: int = Form(...), decision: str = Form(...)):
        _only_local(request, "decisions")
        in_profile(profile)
        try:
            clipper.decide(sv.db, profile, clip_id, decision)
            if decision == "approve":
                planned = routing.assign_and_plan(sv.db, jev_factory, sv.profiles, profile, clip_id)
                for post in (planned or {}).get("posts", []):
                    if post["status"] == "queued":
                        tasks.enqueue(sv.db, "copy", {"profile": post["profile"], "post_id": post["id"]}, dedupe_key=f"copy:{post['id']}")
        except LookupError as e:
            raise HTTPException(404, str(e)) from None
        except ValueError as e:
            raise HTTPException(422, str(e)) from None
        return JSONResponse({"ok": True})

    @app.post("/api/spend-approve")
    def spend_approve(request: Request):
        _only_local(request, "changes")
        spend.approve(sv.db)
        store.event(sv.db, "info", "spend", "You approved more spending: paused work continues")
        return JSONResponse({"ok": True})

    @app.post("/api/account-state")
    def account_state(request: Request, profile: str = Form(...), platform: str = Form(...), action: str = Form(...)):
        _only_local(request, "changes")
        in_profile(profile, platform)
        if action == "pause":
            accounts.pause(sv.db, profile, platform, "paused by you")
        elif action == "resume":
            accounts.resume(sv.db, profile, platform)
        else:
            raise HTTPException(422, "action must be pause or resume")
        return JSONResponse({"ok": True})

    @app.post("/api/audit")
    def audit(request: Request, profile: str = Form(...), platform: str = Form(...), passed: str = Form(...)):
        _only_local(request, "changes")
        in_profile(profile, platform)
        if passed not in ("0", "1"):
            raise HTTPException(422, "passed must be 0 or 1")
        accounts.set_audit(sv.db, profile, platform, passed == "1", "recorded from the dashboard")
        return JSONResponse({"ok": True})

    @app.post("/api/post-copy")
    def post_copy(request: Request, profile: str = Form(...), post_id: int = Form(...), option_id: int = Form(...)):
        _only_local(request, "choices")
        in_profile(profile)
        try:
            copywriter.choose(sv.db, profile, post_id, option_id)
        except LookupError as e:
            raise HTTPException(404, str(e)) from None
        except ValueError as e:
            raise HTTPException(422, str(e)) from None
        return JSONResponse({"ok": True})

    @app.post("/api/post-copy-write")
    def post_copy_write(request: Request, profile: str = Form(...), post_id: int = Form(...)):
        _only_local(request, "requests")
        in_profile(profile)
        try:
            status = copywriter.generate_with_keys(sv.db, sv.profiles[profile], post_id, jev=sv.jev,
                                                   breaker=sv.app.spend_breaker)
        except LookupError as e:
            raise HTTPException(404, str(e)) from None
        except Exception as e:  # Claude or Jev unreachable, or a key missing: say so, change nothing
            provider = spend.engine_of(e)
            if provider:
                spend.note_failure(sv.db, provider, e)  # "no balance" pauses work and shows the banner
            raise HTTPException(502, f"could not write options: {type(e).__name__}: {str(e)[:160]}") from None
        return JSONResponse({"ok": True, "status": status})

    @app.post("/api/clip-post")
    def clip_post(request: Request, profile: str = Form(...), clip_id: int = Form(...)):
        """Post clip: plan the approved clip's post if it has none yet, then send it now."""
        _only_local(request, "posts")
        in_profile(profile)
        with sv.db.conn() as c:
            clip = c.execute("SELECT id, state, profile_id FROM clips_ready WHERE id=? AND (profile_id=? OR id IN "
                             "(SELECT clip_id FROM posts WHERE profile_id=?))", (clip_id, profile, profile)).fetchone()
        if clip is None:
            raise HTTPException(404, "no such clip")
        if clip["state"] != "approved":
            raise HTTPException(422, "only an approved clip can be posted")

        def current():
            with sv.db.conn() as c:
                return c.execute("SELECT * FROM posts WHERE clip_id=? AND status!='cancelled' ORDER BY id LIMIT 1", (clip_id,)).fetchone()

        post = current()
        if post is None:
            routing.assign_and_plan(sv.db, jev_factory, sv.profiles, clip["profile_id"], clip_id)
            post = current()
            if post is None:
                with sv.db.conn() as c:
                    why = c.execute("SELECT reason FROM clips_ready WHERE id=?", (clip_id,)).fetchone()["reason"] or "no channel can post it"
                raise HTTPException(422, why)
        if post["status"] in ("uploaded", "posted"):
            raise HTTPException(422, "this clip is already on YouTube")
        if post["status"] == "blocked":
            raise HTTPException(422, "its post is blocked: " + (post["error"] or "see the post's page"))
        poster = sv.profiles.get(post["profile_id"])
        if poster is None or (why := posting.why_not_sending(sv.db, poster, post["platform"])):
            raise HTTPException(422, why if poster else "its channel no longer exists")
        tasks.enqueue(sv.db, "send_post", {"profile": post["profile_id"], "post_id": post["id"]},
                      dedupe_key=f"send_post:{post['id']}:{store.now():%Y%m%d%H%M}", priority=10)
        return JSONResponse({"ok": True})

    @app.post("/api/clip-handin")
    def clip_handin(request: Request, profile: str = Form(...), clip_id: int = Form(...)):
        """Hand in now: queue this posted campaign clip's link for Content Rewards again."""
        _only_local(request, "posts")
        in_profile(profile)
        with sv.db.conn() as c:
            row = c.execute("SELECT po.id FROM posts po JOIN clips_ready cl ON cl.id=po.clip_id WHERE po.clip_id=? AND cl.campaign_id IS NOT NULL "
                            "AND po.status IN ('uploaded','posted') AND (cl.profile_id=? OR po.profile_id=?) ORDER BY po.id LIMIT 1", (clip_id, profile, profile)).fetchone()
        if row is None:
            raise HTTPException(422, "only a posted clip made for a campaign can be handed in")
        if not rewards.settings(sv.db)["auto"]:
            rewards.set_auto(sv.db, True)  # pressing Hand in now means you want it handed in
        rewards.queue(sv.db, row["id"])
        return JSONResponse({"ok": True})

    @app.post("/api/clips/remake-without-effects")
    def clip_remake_plain(request: Request, profile: str = Form(...), clip_id: int = Form(...)):
        """Remake one clip that is waiting for review with the same look and no picture effects, for when an effect misfires."""
        _only_local(request, "changes")
        in_profile(profile)
        with sv.db.conn() as c:
            row = c.execute("SELECT id, template_id, category, grade_json FROM clips_ready WHERE id=? AND state='review' AND profile_id=?",
                            (clip_id, profile)).fetchone()
        if row is None:
            raise HTTPException(404, "only a clip waiting for your review can be remade")
        grade = json.loads(row["grade_json"] or "{}")
        base = grade.get("look_design")
        if base is None:
            template = format_templates.get(sv.db, row["template_id"]) if row["template_id"] else format_templates.for_category(sv.db, row["category"])
            if template is None:
                raise HTTPException(422, "there is no format to base the remake on: add one on the Formats page")
            base = template["settings"]
        design = format_designs.normalize({**base, "effects": {**format_designs.normalize(base)["effects"], "intensity": "off"}})
        if not relook.queue(sv.db, clip_id, design=design):
            raise HTTPException(409, "this clip is already being made in a format: wait for it to finish")
        return JSONResponse({"ok": True})

    def own_clip(clip_id: int, profile: str):
        with sv.db.conn() as c:
            row = c.execute("SELECT id, state, category, profile_id FROM clips_ready WHERE id=? AND state NOT IN ('removed','superseded') "
                            "AND (profile_id=? OR id IN (SELECT clip_id FROM posts WHERE profile_id=?))", (clip_id, profile, profile)).fetchone()
        if row is None:
            raise HTTPException(404, "no such clip")
        return row

    @app.post("/api/clip-relook")
    def clip_relook(request: Request, profile: str = Form(...), clip_id: int = Form(...), template_id: str = Form(""), remember: str = Form("")):
        """Apply a format: a clip with no format is cut again in a format (the one chosen, or its category's), then waits for your review.
        With `remember`, the chosen format also becomes the one its category uses from now on."""
        _only_local(request, "changes")
        in_profile(profile)
        row = own_clip(clip_id, profile)
        if row["state"] != "review":
            raise HTTPException(422, "only a clip waiting for your review can be given a format")
        owner = sv.profiles.get(row["profile_id"])
        category = row["category"] or (owner.category if owner else None)
        if template_id:
            if format_templates.get(sv.db, template_id) is None:
                raise HTTPException(422, "that format is gone: pick another")
            if remember and category:
                format_templates.assign(sv.db, category, template_id)
        elif format_templates.for_category(sv.db, category) is None:
            raise HTTPException(422, f"{category or 'this'} has no format yet: pick one to apply")
        queued = relook.queue(sv.db, clip_id, template_id=template_id or None)
        return JSONResponse({"ok": True, "queued": queued})

    @app.get("/clips/{clip_id}/format-status")
    def clip_format_status(clip_id: int, profile: str):
        """Where applying a format to this clip stands, for the clip panel's progress."""
        profile_or_404(profile)
        own_clip(clip_id, profile)
        return JSONResponse(relook.state(sv.db, clip_id), headers={"Cache-Control": "no-store"})

    @app.post("/api/clips/apply-formats")
    def clips_apply_formats(request: Request, profile: str = Form(...)):
        """Give every waiting clip of this channel that has no format the format its category uses."""
        _only_local(request, "changes")
        in_profile(profile)
        queued = relook.queue_waiting(sv, profile_id=profile, retry_failed=True)
        if not queued:
            raise HTTPException(422, "no waiting clip has a format to apply: a clip's category needs one on the Formats page")
        return JSONResponse({"ok": True, "queued": queued})

    @app.get("/api/clips/format-progress")
    def clips_format_progress(profile: str):
        profile_or_404(profile)
        return JSONResponse({"pending": relook.in_progress(sv, profile)}, headers={"Cache-Control": "no-store"})

    # ---- formats: templates, presets, assignments and the builder's preview ----------------------------------------

    def refuse(message: str, status: int = 422) -> JSONResponse:
        return JSONResponse({"error": message}, status_code=status)

    @app.get("/formats", response_class=HTMLResponse)
    def formats_page(profile: str | None = None, clip: int | None = None):
        p = profile_or_404(profile)
        return render("formats.html", p, "/formats", "formats", "Formats", d=pages.formats_page(sv, p, clip))

    @app.post("/api/assets/logo")
    async def assets_logo_upload(request: Request, name: str = ""):
        """A logo, sent as the raw file (PNG, WebP or GIF, 2 MB at most). Stored once per picture, checked before it is kept."""
        _only_local(request, "changes")
        try:
            row = format_assets.save_logo(sv.db, name, await request.body())
        except format_assets.AssetError as e:
            return refuse(str(e))
        return JSONResponse({"ok": True, "id": row["id"], "name": row["name"]})

    @app.post("/api/music/import")
    def music_import(request: Request):
        """File every shortlisted track found in the Downloads folder."""
        _only_local(request, "changes")
        try:
            result = format_music.import_downloads(sv.db)
        except OSError as e:
            return refuse(f"The Downloads folder could not be read: {e}")
        return JSONResponse({"ok": True, **result})

    @app.post("/api/music/upload")
    async def music_upload(request: Request, filename: str = "", title: str = "", artist: str = "", mood: str = "", genre: str = "",
                           categories: str = "", source: str = "", licence: str = ""):
        """One track, sent as the raw file with its details in the address."""
        _only_local(request, "changes")
        body = await request.body()
        suffix = pathlib.Path(filename).suffix.lower()
        if suffix not in format_music.TYPES:
            return refuse("Music must be an MP3 or WAV file.")
        if len(body) > format_music.MAX_BYTES:
            return refuse("A track must be 40 MB or smaller.")
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / f"upload{suffix}"
            path.write_bytes(body)
            try:
                track = format_music.add_track(sv.db, path, title=title, artist=artist, mood=mood, genre=genre,
                                               categories=[c for c in categories.split(",") if c], source=source, licence=licence,
                                               original_name=pathlib.Path(filename).name)
            except format_music.MusicError as e:
                return refuse(str(e))
        return JSONResponse({"ok": True, "id": track["id"]})

    @app.post("/api/music/update")
    def music_update(request: Request, body: dict = Body(...)):
        _only_local(request, "changes")
        try:
            format_music.update_track(sv.db, int(body.get("id", 0)), mood=str(body.get("mood", "")), categories=list(body.get("categories") or []))
        except format_music.MusicError as e:
            return refuse(str(e))
        return JSONResponse({"ok": True})

    @app.post("/api/music/delete")
    def music_delete(request: Request, body: dict = Body(...)):
        _only_local(request, "changes")
        format_music.delete_track(sv.db, int(body.get("id", 0)))
        return JSONResponse({"ok": True})

    @app.get("/music/{track_id}/file")
    def music_file(track_id: int):
        track = format_music.get(sv.db, track_id)
        path = format_music.file_path(track) if track else None
        if path is None or not path.exists():
            raise HTTPException(404, "no such track")
        return FileResponse(path, media_type=format_music.MEDIA[f".{track['ext']}"], headers={"Cache-Control": "no-store"})

    @app.post("/api/assets/delete")
    def assets_delete(request: Request, body: dict = Body(...)):
        _only_local(request, "changes")
        try:
            format_assets.delete(sv.db, int(body.get("id", 0)))
        except format_assets.AssetError as e:
            return refuse(str(e))
        return JSONResponse({"ok": True})

    @app.get("/assets/{asset_id}/file")
    def assets_file(asset_id: int):
        asset = format_assets.get(sv.db, asset_id)
        path = format_assets.file_path(asset) if asset else None
        if path is None or not path.exists():
            raise HTTPException(404, "no such file")
        return FileResponse(path, media_type=format_assets.MEDIA[asset["ext"]], headers={"Cache-Control": "no-store"})

    @app.post("/api/fonts/upload")
    async def fonts_upload(request: Request, name: str = "", source: str = "", licence: str = "", filename: str = ""):
        """One font, sent as the raw file (TTF or OTF, 3 MB at most) with where it came from and its licence."""
        _only_local(request, "changes")
        try:
            row = format_font_uploads.save(sv.db, name, await request.body(), ext=pathlib.Path(filename).suffix, source=source,
                                           licence=licence)
        except format_font_uploads.FontUploadError as e:
            return refuse(str(e))
        return JSONResponse({"ok": True, "id": row["id"], "label": row["label"]})

    @app.post("/api/fonts/delete")
    def fonts_delete(request: Request, body: dict = Body(...)):
        _only_local(request, "changes")
        try:
            format_font_uploads.delete(sv.db, int(body.get("id", 0)))
        except format_font_uploads.FontUploadError as e:
            return refuse(str(e))
        return JSONResponse({"ok": True})

    @app.post("/api/formats/save")
    def formats_save(request: Request, body: dict = Body(...)):
        _only_local(request, "changes")
        try:
            format_font_uploads.refresh(sv.db)
            design = format_designs.normalize(body.get("design") or {})
            format_assets.for_design(sv.db, design)
            format_placement.check_design(sv.db, design)
            template = format_templates.save_design(sv.db, name=str(body.get("name", "")), design=body.get("design") or {},
                                                    template_id=body.get("id") or None)
            wanted = {str(c).strip().lower() for c in body.get("categories") or [] if str(c).strip()}
            for category, template_id in format_templates.assignments(sv.db).items():
                if template_id == template["id"] and category not in wanted:
                    format_templates.assign(sv.db, category, None)
            for category in wanted:
                format_templates.assign(sv.db, category, template["id"])
        except (format_templates.TemplateError, format_designs.DesignError) as e:
            return refuse(str(e))
        queued = sum(relook.queue_waiting(sv, category=c) for c in wanted)  # clips already waiting for this category get it too
        return JSONResponse({"ok": True, "id": template["id"], "queued": queued})

    @app.post("/api/formats/delete")
    def formats_delete(request: Request, body: dict = Body(...)):
        _only_local(request, "changes")
        lost = format_templates.delete(sv.db, str(body.get("id", "")))
        return JSONResponse({"ok": True, "lost": lost})

    @app.post("/api/formats/preset")
    def formats_preset(request: Request, body: dict = Body(...)):
        _only_local(request, "changes")
        preset = format_designs.preset(sv.db, str(body.get("preset_id", "")))
        if preset is None:
            return refuse("That preset is gone.", 404)
        try:
            template = format_templates.save_design(sv.db, name=preset["name"], design=preset["design"])
            queued = 0
            if body.get("action") == "use":
                format_templates.assign(sv.db, str(body.get("category", "")), template["id"])
                queued = relook.queue_waiting(sv, category=str(body.get("category", "")))
        except format_templates.TemplateError as e:
            return refuse(str(e))
        return JSONResponse({"ok": True, "id": template["id"], "queued": queued})

    @app.post("/api/formats/preset-delete")
    def formats_preset_delete(request: Request, body: dict = Body(...)):
        _only_local(request, "changes")
        try:
            format_designs.delete_preset(sv.db, str(body.get("preset_id", "")))
        except format_designs.DesignError as e:
            return refuse(str(e), 404)
        return JSONResponse({"ok": True})

    @app.post("/api/formats/suggest-webcam")
    def formats_suggest_webcam(request: Request, body: dict = Body(...)):
        """A webcam box drawn around the streamer's face in a downloaded video. Nothing is saved."""
        _only_local(request, "changes")
        from core.paths import cached_source

        from ..formats import DOWNLOADS
        from ..formats import webcam as format_webcam

        video_id = str(body.get("video_id", ""))
        source = cached_source(DOWNLOADS, video_id) or DOWNLOADS / f"{video_id}.mp4"
        if not source.exists():
            return refuse("That video is not downloaded: pick another one above.")
        box = format_webcam.suggest(source)
        if box is None:
            return refuse("No face was found in the top-left of that video. Set the box by hand.")
        return JSONResponse({"ok": True, **box})

    @app.post("/api/formats/preview")
    def formats_preview(request: Request, body: dict = Body(...)):
        _only_local(request, "changes")
        try:
            out = format_preview.make_preview(sv.db, profile_or_404(body.get("profile")), body.get("design") or {},
                                              str(body.get("video_id", "")), PREVIEWS)
        except (format_designs.DesignError, format_preview.PreviewError) as e:
            return refuse(str(e))
        return JSONResponse({"ok": True, "url": f"/formats/preview/{out.name}"})

    @app.post("/api/formats/check")
    def formats_check(request: Request, body: dict = Body(...)):
        """What the builder's placement covers, in plain words, while things are dragged. Nothing is saved."""
        _only_local(request, "changes")
        try:
            design = format_designs.normalize(body.get("design") or {})
        except format_designs.DesignError as e:
            return refuse(str(e))
        return JSONResponse({"ok": True, "problems": format_placement.problems_for(sv.db, design)})

    @app.post("/api/formats/generate")
    def formats_generate(request: Request, body: dict = Body(...)):
        """Three AI-made formats from a few answers, each with a preview. Nothing is saved until one is picked."""
        _only_local(request, "changes")
        from ..formats import generate as format_generate
        try:
            result = format_generate.run(sv.db, claude._client(), body, profile_or_404(body.get("profile")), PREVIEWS)
        except (format_generate.GenerateError, format_designs.DesignError) as e:
            return refuse(str(e))
        except Exception as e:   # an AI or render problem is said in plain words, never as a crash
            return refuse(f"The AI could not make the formats right now ({type(e).__name__}). Try again.", 502)
        return JSONResponse({"ok": True, **result})

    @app.post("/api/formats/generate/feedback")
    def formats_generate_feedback(request: Request, body: dict = Body(...)):
        """A look the owner liked or did not: remembered, so the next generate for this kind of clip leans the right way."""
        _only_local(request, "changes")
        from ..formats import generate as format_generate
        try:
            answers = format_generate.check_answers(body)
            format_generate.record_feedback(sv.db, answers, body.get("design") or {}, str(body.get("verdict", "")))
        except (format_generate.GenerateError, format_designs.DesignError) as e:
            return refuse(str(e))
        return JSONResponse({"ok": True})

    @app.post("/api/formats/generate/pick")
    def formats_generate_pick(request: Request, body: dict = Body(...)):
        """Save the option the owner picked, as a template, after checking it again."""
        _only_local(request, "changes")
        from ..formats import generate as format_generate
        try:
            template = format_generate.save(sv.db, body, str(body.get("name", "")), body.get("design") or {},
                                            [str(c) for c in body.get("categories") or []])
        except (format_generate.GenerateError, format_designs.DesignError, format_templates.TemplateError) as e:
            return refuse(str(e))
        queued = sum(relook.queue_waiting(sv, category=str(c)) for c in body.get("categories") or [])
        return JSONResponse({"ok": True, "id": template["id"], "queued": queued})

    live_preview = format_live.LivePreview(PREVIEWS)   # the pieces of the builder's live preview are made one at a time, in the background

    @app.post("/api/formats/preview/start")
    def formats_preview_start(request: Request, body: dict = Body(...)):
        """A new live preview round for the design as it is now: the piece under the playhead is made first, then the ones beside it,
        then the rest, and anything an earlier round still had waiting is dropped. Returns which pieces are ready already."""
        _only_local(request, "changes")
        try:
            real, owner = None, profile_or_404(body.get("profile"))
            if body.get("clip_id"):
                real, row = builder_clip(owner.id, body["clip_id"])
                owner = sv.profiles.get(row["profile_id"], owner)
            prepared = format_preview.prepare(sv.db, owner, body.get("design") or {}, str(body.get("video_id", "")),
                                              draft=bool(body.get("draft")), clip=real)
            playhead = float(body.get("playhead") or 0)
        except (format_designs.DesignError, format_preview.PreviewError) as e:
            return refuse(str(e))
        except (TypeError, ValueError):
            return refuse("The playhead must be a number of seconds.")
        return JSONResponse({"ok": True, **live_preview.submit(prepared, playhead)}, headers={"Cache-Control": "no-store"})

    @app.get("/api/formats/preview/status")
    def formats_preview_status(round: int):
        """Which pieces of a live preview round are ready. `superseded` means a newer round replaced it."""
        return JSONResponse({"ok": True, **live_preview.status(round)}, headers={"Cache-Control": "no-store"})

    @app.get("/formats/preview/{name}")
    def formats_preview_file(name: str):
        path = PREVIEWS / name
        if not re.fullmatch(r"format-(preview|seg)-[0-9a-f]{12}\.mp4", name) or not path.exists():
            raise HTTPException(404, "no such preview")
        return FileResponse(path, media_type="video/mp4", headers={"Cache-Control": "no-store"})

    @app.post("/api/formats/stage")
    def formats_stage(request: Request, body: dict = Body(...)):
        """The builder's live stage for one sample video: a still frame framed the way the layout frames a clip, and the
        spoken words of the sample. Only the layout, crop limit and webcam box of the design matter here."""
        _only_local(request, "changes")
        try:
            real = builder_clip(profile_or_404(body.get("profile")).id, body["clip_id"])[0] if body.get("clip_id") else None
            result = format_preview.stage(sv.db, str(body.get("video_id", "")), PREVIEWS, body.get("design") or {}, clip=real)
        except (format_designs.DesignError, format_preview.PreviewError) as e:
            return refuse(str(e))
        return JSONResponse(result, headers={"Cache-Control": "no-store"})

    @app.get("/formats/still/{name}")
    def formats_still_file(name: str):
        path = PREVIEWS / name
        if not re.fullmatch(r"format-still-[0-9a-f]{12}\.jpg", name) or not path.exists():
            raise HTTPException(404, "no such still")
        return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "no-store"})

    @app.get("/formats/font/{font_id}")
    def formats_font_file(font_id: str):
        """A font the caption and headline pickers offer, so the stage can draw it the same way."""
        from ..formats import fonts as format_fonts
        font = format_fonts.FONTS.get(font_id)
        if font is None:
            raise HTTPException(404, "no such font")
        path = pathlib.Path(font.folder or format_fonts.FONTS_DIR) / font.file
        media = "font/otf" if path.suffix.lower() == ".otf" else "font/ttf"
        return FileResponse(path, media_type=media, headers={"Cache-Control": "public, max-age=86400"})

    def builder_clip(profile: str, clip_id):
        """The clip the builder was opened on (Edit look), as the preview needs it, with its row and the channel that owns it."""
        with sv.db.conn() as c:
            row = c.execute("SELECT * FROM clips_ready WHERE id=? AND (profile_id=? OR id IN (SELECT clip_id FROM posts WHERE profile_id=?))",
                            (int(clip_id), profile, profile)).fetchone()
        if row is None:
            raise format_preview.PreviewError("There is no such clip.")
        return format_preview.RealClip.from_row(row), row

    @app.post("/api/formats/apply-clip")
    def formats_apply_clip(request: Request, body: dict = Body(...)):
        """Edit look: re-make one clip in a chosen template, or in a design made for just this clip. A clip that has gone out is refused."""
        _only_local(request, "changes")
        profile, clip_id = str(body.get("profile", "")), int(body.get("clip_id", 0))
        in_profile(profile)
        with sv.db.conn() as c:
            row = c.execute("SELECT * FROM clips_ready WHERE id=? AND profile_id=?", (clip_id, profile)).fetchone()
        if row is None:
            return refuse("There is no such clip.", 404)
        if (problem := relook.edit_problem(sv.db, row)):
            return refuse(problem, 409)
        design = None
        if body.get("design"):
            try:
                design = format_designs.normalize(body["design"])
            except format_designs.DesignError as e:
                return refuse(str(e))
        if not relook.queue(sv.db, clip_id, template_id=body.get("template_id") or None, design=design):
            return refuse("This clip is already being made in a format: wait for it to finish, then try again.", 409)
        return JSONResponse({"ok": True})

    @app.post("/api/clip-repost")
    def clip_repost(request: Request, profile: str = Form(...), clip_id: int = Form(...)):
        """Post again: a clip whose post already went out (its video was deleted from YouTube, say) goes out once more."""
        _only_local(request, "posts")
        in_profile(profile)
        with sv.db.conn() as c:
            post = c.execute("SELECT po.id, po.profile_id FROM posts po JOIN clips_ready cl ON cl.id=po.clip_id WHERE po.clip_id=? "
                             "AND po.status IN ('uploaded','posted') AND cl.state='approved' AND (cl.profile_id=? OR po.profile_id=?) "
                             "ORDER BY po.id LIMIT 1", (clip_id, profile, profile)).fetchone()
        if post is None:
            raise HTTPException(404, "this clip has no post that went out")
        poster = sv.profiles.get(post["profile_id"])
        if poster is None:
            raise HTTPException(422, "its channel no longer exists")
        try:
            posting.repost(sv.db, poster, post["id"])
        except ValueError as e:
            raise HTTPException(422, str(e)) from None
        tasks.enqueue(sv.db, "send_post", {"profile": post["profile_id"], "post_id": post["id"]},
                      dedupe_key=f"send_post:{post['id']}:{store.now():%Y%m%d%H%M}", priority=10)
        return JSONResponse({"ok": True})

    @app.post("/api/post-send-now")
    def post_send_now(request: Request, profile: str = Form(...), post_id: int = Form(...)):
        _only_local(request, "posts")
        p = in_profile(profile)
        with sv.db.conn() as c:
            row = c.execute("SELECT platform, status FROM posts WHERE profile_id=? AND id=?", (profile, post_id)).fetchone()
        if row is None:
            raise HTTPException(404, "no such post")
        if row["status"] in ("uploaded", "posted"):
            raise HTTPException(422, "this post is already out")
        if (why := posting.why_not_sending(sv.db, p, row["platform"])):
            raise HTTPException(422, why)
        tasks.enqueue(sv.db, "send_post", {"profile": profile, "post_id": post_id}, dedupe_key=f"send_post:{post_id}:{store.now():%Y%m%d%H%M}", priority=10)
        return JSONResponse({"ok": True})

    @app.post("/api/posts-send-waiting")
    def posts_send_waiting(request: Request, profile: str = Form(...), platform: str = Form(...)):
        _only_local(request, "posts")
        p = in_profile(profile, platform)
        try:
            n = posting.resend_waiting(sv.db, p, platform)
        except ValueError as e:
            raise HTTPException(422, str(e)) from None
        if n:
            tasks.enqueue(sv.db, "post", {"profile": profile}, dedupe_key=f"post-now:{profile}:{store.now():%Y%m%d%H%M}", priority=10)  # the schedule would pick them up too; this is sooner
        return JSONResponse({"ok": True, "queued": n})

    @app.post("/api/post-delete")
    def post_delete(request: Request, profile: str = Form(...), post_id: int = Form(...)):
        _only_local(request, "posts")
        in_profile(profile)
        try:
            removal.delete_post(sv.db, profile, post_id)
        except LookupError as e:
            raise HTTPException(404, str(e)) from None
        except removal.RemovalError as e:
            raise HTTPException(422, str(e)) from None
        return JSONResponse({"ok": True})

    @app.post("/api/post-done")
    def post_done(request: Request, profile: str = Form(...), post_id: int = Form(...), url: str = Form(...)):
        _only_local(request, "posts")
        in_profile(profile)
        try:
            posting.mark_posted(sv.db, profile, post_id, url)
        except LookupError as e:
            raise HTTPException(404, str(e)) from None
        except ValueError as e:
            raise HTTPException(422, str(e)) from None
        return JSONResponse({"ok": True})

    return app


def date_fromiso(text: str | None) -> date | None:
    return date.fromisoformat(text) if text else None
