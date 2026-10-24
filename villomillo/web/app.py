"""The dashboard: Home, Research, Clips, Calendar, Accounts, Insights and Settings.

It only answers requests addressed to 127.0.0.1 or localhost, and every change (a decision, a pause, a choice) refuses a
request that comes from a page on any other site. Pages are server-rendered from template files; see pages.py for the
data each one shows and docs/DASHBOARD-REDESIGN.md for the design.
"""

from __future__ import annotations

import pathlib
import subprocess
from datetime import date
from html import escape as html_escape
from urllib.parse import urlsplit

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, FileSystemLoader
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .. import accounts, clipper, copywriter, posting, routing, secrets, store, tasks, timefmt
from ..services import Services
from . import pages

LOCAL_HOSTS = {"127.0.0.1", "localhost"}
HERE = pathlib.Path(__file__).parent
THUMBS = pathlib.Path(clipper.OUTBOX).parent / "thumbs"


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
    def home(profile: str | None = None):
        p = profile_or_404(profile)
        return render("home.html", p, "/", "home", "Home", d=pages.home_page(sv, p))

    @app.get("/research", response_class=HTMLResponse)
    def research(profile: str | None = None, gate: str = "all", q: str = "", page: int = 1):
        p = profile_or_404(profile)
        return render("research.html", p, "/research", "research", "Research", d=pages.research_page(sv, p, gate, q, max(1, page)))

    @app.get("/clips", response_class=HTMLResponse)
    def clips(profile: str | None = None, tab: str = "all"):
        p = profile_or_404(profile)
        return render("clips.html", p, "/clips", "clips", "Clips", d=pages.clips_page(sv, p, tab))

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

    @app.get("/calendar/post/{post_id}", response_class=HTMLResponse)
    def post_panel(post_id: int, profile: str):
        p = profile_or_404(profile)
        data = pages.post_panel(sv, p, post_id)
        if data is None:
            raise HTTPException(404, "no such post")
        return fragment("partials/post_panel.html", p=p, d=data)

    @app.get("/accounts", response_class=HTMLResponse)
    def accounts_page(profile: str | None = None):
        p = profile_or_404(profile)
        return render("accounts.html", p, "/accounts", "accounts", "Accounts", d=pages.accounts_page(sv, p))

    @app.get("/insights", response_class=HTMLResponse)
    def insights(profile: str | None = None):
        p = profile_or_404(profile)
        return render("insights.html", p, "/insights", "insights", "Insights", d=pages.insights_page(sv, p))

    @app.get("/settings", response_class=HTMLResponse)
    def settings(profile: str | None = None):
        p = profile_or_404(profile)
        return render("settings.html", p, "/settings", "settings", "Settings", d=pages.settings_page(sv, p))

    @app.get("/api/search")
    def search(profile: str, q: str = ""):
        p = profile_or_404(profile)
        return JSONResponse({"results": pages.search(sv, p, q) if len(q.strip()) >= 2 else []})

    # ---- old addresses keep working ---------------------------------------------------------------------------

    def moved(target: str):
        def go(profile: str | None = None):
            return RedirectResponse(f"{target}{'&' if '?' in target else '?'}profile={profile_or_404(profile).id}", status_code=307)
        return go

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
                if planned and planned["status"] == "queued":
                    tasks.enqueue(sv.db, "copy", {"profile": planned["profile"], "post_id": planned["id"]},
                                  dedupe_key=f"copy:{planned['id']}")
        except LookupError as e:
            raise HTTPException(404, str(e)) from None
        except ValueError as e:
            raise HTTPException(422, str(e)) from None
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
                                                   budget_usd=sv.app.budgets.claude_usd_per_month)
        except LookupError as e:
            raise HTTPException(404, str(e)) from None
        except Exception as e:  # Claude or Jev unreachable, or a key missing: say so, change nothing
            raise HTTPException(502, f"could not write options: {type(e).__name__}: {str(e)[:160]}") from None
        return JSONResponse({"ok": True, "status": status})

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
