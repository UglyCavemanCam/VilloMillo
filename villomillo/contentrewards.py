"""Reads Content Rewards' public pages: the campaign list on Discover, and one campaign's own page.

Whop's API has nothing a clipper can use for this (the key test showed it), but the public pages are server-rendered and carry
their data inside the page (a React "dehydrated state"). Nothing is logged in to and nothing is changed: these are the same pages
anyone can open, read once an hour. If the pages change shape, the parsers return nothing and the Campaigns page says so; pasting
a campaign link by hand still works whenever one campaign's page can be read.
"""

from __future__ import annotations

import json
import re

import httpx

BASE = "https://contentrewards.com/c"
DISCOVER = f"{BASE}/discover"
HEADERS = {"User-Agent": "Mozilla/5.0 (VilloMillo; reading public campaign pages)", "Accept": "text/html"}
UUID = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
_PUSH = re.compile(r'self\.__next_f\.push\(\[1,"(.*?)"\]\)</script>', re.S)
_DECODER = json.JSONDecoder()


class CampaignPageError(RuntimeError):
    """A campaign page could not be read. The message is safe to show."""


def campaign_id(text: str) -> str | None:
    """The campaign id in a pasted link (or a bare id)."""
    m = re.search(rf"campaigns/({UUID})", text or "") or re.fullmatch(rf"\s*({UUID})\s*", text or "")
    return m.group(1).lower() if m else None


def campaign_url(cid: str) -> str:
    return f"{BASE}/campaigns/{cid}"


def payload(html: str) -> str:
    """All the data the page carries, as one string of JSON fragments."""
    parts = []
    for chunk in _PUSH.findall(html):
        try:
            parts.append(json.loads('"' + chunk + '"'))
        except ValueError:
            continue
    return "".join(parts)


def _objects(text: str, opener: str):
    """Every JSON object that starts at `opener` (the opener's first character is the object's own brace)."""
    start = 0
    while (i := text.find(opener, start)) != -1:
        start = i + 1
        try:
            obj, _ = _DECODER.raw_decode(text, i)
        except ValueError:
            continue
        yield obj


def parse_list(html: str) -> list[dict]:
    """The campaigns on a Discover page, one summary each (duplicates removed, order kept)."""
    seen, out = set(), []
    for obj in _objects(payload(html), '{"campaign":{'):
        c = obj.get("campaign") or {}
        cid = str(c.get("id") or "").lower()
        if cid and cid not in seen and ("ratePer1kLabel" in c or "payoutSortRaw" in c):
            seen.add(cid)
            out.append(_summary(c))
    return out


def _cents(dollars) -> int | None:
    return None if dollars is None else round(float(dollars) * 100)


def _summary(c: dict) -> dict:
    return {
        "id": str(c["id"]).lower(), "title": c.get("title") or "", "brand": c.get("brand") or "", "category": c.get("category") or "",
        "platforms": [str(p).lower() for p in c.get("platforms") or []], "type": c.get("type") or "",
        "requires_application": bool(c.get("requiresApplication")), "description": c.get("description") or "",
        "best_rate_cents": _cents(c.get("payoutSortRaw")), "budget_total_cents": _cents(c.get("budgetTotalRaw")),
        "budget_spent_cents": _cents(c.get("budgetSpentRaw")), "budget_left_cents": _cents(c.get("availableBudgetRaw")),
        "creators": c.get("creatorCountRaw"), "status": "active",  # the Discover list shows only campaigns taking clips
    }


def parse_detail(html: str, cid: str) -> dict:
    """One campaign's own page: its status, categories, budget and the pay for each platform."""
    cid = cid.lower()
    for obj in _data_objects(payload(html)):
        if str(obj.get("id", "")).lower() == cid and "configuration" in obj:
            return _detail(obj)
    raise CampaignPageError("this campaign's page could not be read (it may be private, removed, or the page has changed)")


def _data_objects(text: str):
    start = 0
    while (i := text.find('"data":{', start)) != -1:
        start = i + 1
        try:
            obj, _ = _DECODER.raw_decode(text, i + len('"data":'))
        except ValueError:
            continue
        yield obj


def _detail(d: dict) -> dict:
    cfg = d.get("configuration") or {}
    pay = {str(p.get("platform")).lower(): p for p in cfg.get("payoutModel") or []}
    metrics = d.get("metrics") or {}
    categories = [c.get("name") for c in (cfg.get("content") or {}).get("contentCategories", []) if c.get("name")]
    budget = cfg.get("budget")
    spent = metrics.get("budgetSpentCents")
    return {
        "id": str(d["id"]).lower(), "title": d.get("name") or "", "brand": (d.get("organization") or {}).get("name") or "",
        "category": categories[0] if categories else "", "platforms": sorted(pay), "type": d.get("type") or "",
        "status": d.get("status") or "", "status_reason": d.get("statusReason") or "",
        "requires_application": bool(d.get("requiresApplication")), "private": bool(d.get("private")),
        "pre_approval": bool((cfg.get("requirement") or {}).get("preApproval")), "description": d.get("description") or "",
        "rates": {p: {"rate_cents": v.get("rate"), "min_payout_cents": v.get("minPayout"), "max_payout_cents": v.get("maxPayout")} for p, v in pay.items()},
        "budget_total_cents": budget, "budget_spent_cents": spent,
        "budget_left_cents": (budget - spent) if isinstance(budget, int) and isinstance(spent, int) else None,
        "creators": metrics.get("creatorCount"), "views": metrics.get("totalViews"),
        "rule_counts": d.get("briefCounts") or {},
    }


def _get(client: httpx.Client, url: str) -> str:
    try:
        r = client.get(url, headers=HEADERS, follow_redirects=True, timeout=30)
    except httpx.HTTPError as e:
        raise CampaignPageError(f"could not reach Content Rewards ({type(e).__name__})") from None
    if r.status_code != 200:
        raise CampaignPageError(f"Content Rewards answered HTTP {r.status_code}")
    return r.text


def new_client() -> httpx.Client:
    return httpx.Client()


def fetch_list(client: httpx.Client | None = None) -> list[dict]:
    return parse_list(_get(client or new_client(), DISCOVER))


def fetch_detail(cid: str, client: httpx.Client | None = None) -> dict:
    return parse_detail(_get(client or new_client(), campaign_url(cid)), cid)
