"""The Whop probe is read-only, hides what looks secret and never fails on a bad answer."""

from __future__ import annotations

import pathlib
import sys

import httpx

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "scripts"))
import whop_probe


def test_secret_looking_fields_are_hidden_and_long_things_are_cut():
    out = whop_probe.sanitize({"id": "c1", "owner_email": "a@b.c", "api_key": "k", "note": "x" * 300,
                               "items": list(range(10)), "nested": {"access_token": "t", "rate": 1.25}})
    assert out["owner_email"] == "[hidden]" and out["api_key"] == "[hidden]"
    assert out["nested"] == {"access_token": "[hidden]", "rate": 1.25}
    assert len(out["note"]) == 120 and out["items"] == {"count": 10, "first": [0, 1]}


def test_a_probe_reports_status_and_a_shortened_body():
    client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"data": [{"id": 1}] * 5})))
    got = whop_probe.probe(client, "/bounties")
    assert got["status"] == 200 and got["body"]["data"]["count"] == 5


def test_an_error_answer_and_a_dead_connection_are_both_just_results():
    refused = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(403, json={"error": "no permission"})))
    assert whop_probe.probe(refused, "/campaigns")["status"] == 403

    def boom(request):
        raise httpx.ConnectError("down")

    assert "could not reach Whop" in whop_probe.probe(httpx.Client(transport=httpx.MockTransport(boom)), "/campaigns")["error"]


def test_the_probe_only_ever_asks_it_never_changes_anything():
    methods = []

    def record(request):
        methods.append(request.method)
        return httpx.Response(404)

    client = httpx.Client(transport=httpx.MockTransport(record))
    for path in whop_probe.LISTS:
        whop_probe.probe(client, path)
    assert set(methods) == {"GET"}
