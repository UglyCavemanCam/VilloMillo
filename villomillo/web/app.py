"""The dashboard: an overview page and the Research list where you give thumbs up or down.

It only answers requests addressed to 127.0.0.1 or localhost, and refuses a rating that
comes from a page on any other site.
"""

from __future__ import annotations

import json
from urllib.parse import urlsplit

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from jinja2 import DictLoader, Environment
from starlette.middleware.trustedhost import TrustedHostMiddleware

from publish.quota import pacific_day

from .. import store
from ..services import Services
from .templates import TEMPLATES

GATES = ("all", "pass", "escalate", "fail")
LOCAL_HOSTS = {"127.0.0.1", "localhost"}


def _age(published_at: str) -> str:
    hours = (store.now() - store.parse_time(published_at)).total_seconds() / 3600
    return f"{int(hours)}h" if hours < 48 else f"{int(hours // 24)}d"


def _row(r: dict) -> dict:
    comps = json.loads(r["components_json"])
    feats = comps.pop("_features", {})
    comps.pop("_risk", None)
    outlier = feats.get("outlier_ratio")
    reasons = [f"{k} {v:.0%}" for k, v in comps.items()]
    if outlier is not None:
        reasons.insert(0, f"views x{outlier:.2f} of usual")
    return {
        **r,
        "age": _age(r["published_at"]),
        "reasons": reasons,
        "flags": json.loads(r["flags_json"]),
        "thumb": f"https://i.ytimg.com/vi/{r['video_id']}/mqdefault.jpg",
        "url": f"https://www.youtube.com/watch?v={r['video_id']}",
        "minutes": round((r["duration_s"] or 0) / 60),
    }


def create_app(sv: Services) -> FastAPI:
    app = FastAPI(title="VilloMillo", docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=sorted(LOCAL_HOSTS))
    env = Environment(loader=DictLoader(TEMPLATES), autoescape=True)

    def page(name: str, **ctx) -> HTMLResponse:
        return HTMLResponse(env.get_template(name).render(profiles=sv.profiles, **ctx))

    def profile_or_404(profile_id: str | None):
        pid = profile_id or next(iter(sv.profiles), None)
        if pid not in sv.profiles:
            raise HTTPException(404, "no such channel profile")
        return sv.profiles[pid]

    @app.get("/health")
    def health():
        return {"ok": True}

    @app.get("/", response_class=HTMLResponse)
    def overview(profile: str | None = None):
        p = profile_or_404(profile)
        rows = store.ranked(sv.db, p.id, limit=500)
        with sv.db.conn() as c:
            events = [dict(r) for r in c.execute("SELECT * FROM events ORDER BY id DESC LIMIT 12")]
            tasks = {r["status"]: r["n"] for r in c.execute("SELECT status, COUNT(*) AS n FROM tasks GROUP BY 1")}
        return page(
            "overview.html", current=p, states=store.counts_by_state(sv.db, p.id), events=events, tasks=tasks,
            precision=store.precision_at(rows, 20), day=pacific_day(),
            quota={"general": (sv.quota.used("general"), sv.quota.caps["general"]),
                   "search": (sv.quota.used("search"), sv.quota.caps["search"])},
            jev=(store.ai_spend_today(sv.db, "jev"), sv.app.budgets.jev_usd_per_day),
            gates={g: sum(1 for r in rows if r["gate"] == g) for g in GATES[1:]},
        )

    @app.get("/research", response_class=HTMLResponse)
    def research(profile: str | None = None, gate: str = "all", limit: int = 100):
        p = profile_or_404(profile)
        gate = gate if gate in GATES else "all"
        everything = store.ranked(sv.db, p.id, limit=500)
        rows = [_row(r) for r in everything if gate == "all" or r["gate"] == gate][: max(1, min(limit, 500))]
        return page("research.html", current=p, rows=rows, gate=gate, gates=GATES,
                    precision=store.precision_at(everything, 20))

    @app.post("/api/rate")
    def rate(request: Request, profile: str = Form(...), video_id: str = Form(...), rating: int = Form(...)):
        origin = request.headers.get("origin")
        if origin and urlsplit(origin).hostname not in LOCAL_HOSTS:
            raise HTTPException(403, "ratings only come from this dashboard")
        if profile not in sv.profiles:
            raise HTTPException(404, "no such channel profile")
        if not store.VIDEO_ID.match(video_id) or rating not in (-1, 0, 1):
            raise HTTPException(422, "bad video id or rating")
        store.rate(sv.db, profile, video_id, rating)
        rows = store.ranked(sv.db, profile, limit=500)
        return JSONResponse({"ok": True, "precision": store.precision_at(rows, 20)})

    return app
