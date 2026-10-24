"""Ask Whop what your API key can see, so we know how much of Campaigns can be automatic.

Read-only. It only sends GET requests, never changes anything, never saves your key and never prints it. The output is
shortened and anything that looks personal or secret (emails, tokens, keys, addresses, phone numbers) is replaced with
"[hidden]" before it is shown or written.

Run it from the project folder:

    .\\.venv\\Scripts\\python.exe scripts\\whop_probe.py

It asks for the key (what you type is not shown). The result is printed and also written to
data/villomillo/whop-probe.json; paste the printed text back to Claude.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import pathlib
import re
import sys

import httpx

BASE = "https://api.whop.com/api/v1"
CAMPAIGN_ID = "6bdc8e4b-3ebf-44f1-aedb-067589ca9c89"  # the Content Rewards campaign you joined (from its web address)
LISTS = ["/bounties", "/bounty_submissions", "/content_rewards", "/content_reward_campaigns", "/campaigns", "/companies",
         "/apps"]
ONE = ["/bounties/{id}", "/content_rewards/{id}", "/content_reward_campaigns/{id}", "/campaigns/{id}"]
HIDE = ("email", "token", "secret", "password", "phone", "address", "api_key", "apikey", "authorization", "ip_address")
NAME = re.compile(r"^[a-z][a-z0-9_]*(?::[a-z0-9_]+)+$")  # a permission name such as bounty:basic:read
INTERESTING = ("bounty", "campaign", "reward", "submission", "content", "company:basic", "clip")
OUT = pathlib.Path(__file__).resolve().parent.parent / "data" / "villomillo" / "whop-probe.json"


def sanitize(value, depth: int = 0):
    """A short, safe picture of a response: secret-looking fields hidden, long text and long lists cut."""
    if isinstance(value, dict):
        if depth >= 4:
            return {"...": f"{len(value)} fields"}
        return {k: ("[hidden]" if any(h in k.lower() for h in HIDE) else sanitize(v, depth + 1))
                for k, v in list(value.items())[:40]}
    if isinstance(value, list):
        return {"count": len(value), "first": [sanitize(v, depth + 1) for v in value[:2]]}
    if isinstance(value, str):
        return value if len(value) <= 120 else value[:117] + "..."
    return value


def permission_names(value) -> list[str]:
    """Every permission-looking name anywhere in an answer, so the catalog can be shown as a plain list."""
    found: set[str] = set()
    if isinstance(value, dict):
        for v in value.values():
            found.update(permission_names(v))
    elif isinstance(value, list):
        for v in value:
            found.update(permission_names(v))
    elif isinstance(value, str) and NAME.match(value):
        found.add(value)
    return sorted(found)


def catalog(client: httpx.Client) -> dict:
    """The names Whop's own permission picker offers, from its documented permission catalog."""
    try:
        r = client.get(BASE + "/api_keys/permissions")
        names = permission_names(r.json()) if r.status_code == 200 else []
        out = {"status": r.status_code, "count": len(names)}
    except (httpx.HTTPError, ValueError):
        return {"status": None, "count": 0, "names_that_look_relevant": [], "all_names": []}
    out["names_that_look_relevant"] = [n for n in names if any(k in n for k in INTERESTING)]
    out["all_names"] = names
    return out


def probe(client: httpx.Client, path: str) -> dict:
    """GET one path and describe what came back. Never raises: a failure is part of the answer."""
    try:
        r = client.get(BASE + path)
    except httpx.HTTPError as e:
        return {"path": path, "error": f"could not reach Whop ({type(e).__name__})"}
    try:
        body = r.json()
    except ValueError:
        body = None
    out = {"path": path, "status": r.status_code}
    if body is not None:
        out["body"] = sanitize(body)
    elif r.text:
        out["text"] = r.text[:120]
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--campaign-id", default=CAMPAIGN_ID)
    parser.add_argument("--path", action="append", default=[], help="an extra path to try, such as /bounties")
    args = parser.parse_args(argv)

    key = os.environ.get("WHOP_API_KEY") or getpass.getpass("Whop API key (typing is hidden): ").strip()
    if not key:
        print("No key given.")
        return 2
    paths = LISTS + [p.format(id=args.campaign_id) for p in ONE] + args.path
    with httpx.Client(headers={"Authorization": f"Bearer {key}"}, timeout=20) as client:
        results = [probe(client, p) for p in paths]
        permissions = catalog(client)
    text = json.dumps({"base": BASE, "permissions": permissions, "results": results}, indent=2, ensure_ascii=False)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(text, encoding="utf-8")
    print(text)
    print(f"\nAlso saved to {OUT}. Paste the text above to Claude. Your key was not printed or saved.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
