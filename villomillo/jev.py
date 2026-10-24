"""A thin client for Jev (TypeSafe's System One API)."""

from __future__ import annotations

import time
from dataclasses import dataclass

import httpx

URL = "https://api.typesafe.ai/v1/systemone"
USD_PER_INPUT_TOKEN = 0.042 / 1_000_000  # output tokens are free of charge
RETRY_STATUS = {429, 500, 502, 503, 504, 529}


class JevError(RuntimeError):
    pass


@dataclass
class JevResult:
    model: str
    answers: dict
    usage: dict

    @property
    def cost_usd(self) -> float:
        return (self.usage.get("input_tokens") or 0) * USD_PER_INPUT_TOKEN


class Jev:
    def __init__(self, api_key: str, *, model: str = "jev-latest", client: httpx.Client | None = None,
                 retries: int = 4, sleep=time.sleep):
        self._key = api_key
        self.model = model
        self.http = client or httpx.Client(timeout=60)
        self.retries = retries
        self._sleep = sleep

    def ask(self, state: dict | str, questions: dict) -> JevResult:
        """Ask every question about `state` in one request (they are answered independently)."""
        body = {"model": self.model, "state": state, "questions": questions}
        headers = {"Authorization": f"Bearer {self._key}"}
        for attempt in range(self.retries + 1):
            try:
                r = self.http.post(URL, json=body, headers=headers)
            except httpx.HTTPError as e:
                if attempt == self.retries:
                    raise JevError(f"network error ({type(e).__name__})") from None
                self._sleep(2**attempt)
                continue
            if r.status_code == 200:
                data = r.json()
                return JevResult(data.get("model", self.model), data["answers"], data.get("usage", {}))
            if r.status_code in RETRY_STATUS and attempt < self.retries:
                self._sleep(2**attempt)
                continue
            detail = ""
            try:
                detail = str(r.json().get("detail", ""))[:200]
            except ValueError:
                pass
            raise JevError(f"HTTP {r.status_code} {detail}".strip())
        raise JevError("gave up after retries")  # not reachable; keeps the type checker honest
