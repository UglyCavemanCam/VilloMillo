"""Signing in to a provider instead of pasting a key (OAuth with PKCE).

OpenRouter's flow, for people who have never made an API key: the app sends
the user's own browser to openrouter.ai/auth with a code challenge; they sign
in and approve Clips Kitty; OpenRouter sends the browser back to the app's
callback on this PC with a one-time code, and the app trades the code and its
verifier for an API key of the user's own, which spends their OpenRouter
credits (POST /auth/keys). No app secret and no registration: the verifier
is the proof, it is made fresh for each sign-in, and it never leaves this PC
until the exchange. The key is then checked and kept exactly as a pasted one
is (server/ai_api.py). The website does the same from the browser
(web/lib/openrouter.ts).
"""

import base64
import hashlib
import secrets
from urllib.parse import urlencode

import requests

from llm.providers import http
from llm.providers.base import LLMError, ProviderSpec

# What the key is called in the user's OpenRouter key list, so they can see
# where it came from and revoke it there.
KEY_LABEL = "Clips Kitty"


def pkce_pair() -> tuple[str, str]:
    """(verifier, S256 challenge), fresh for one sign-in."""
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return verifier, base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def auth_url(spec: ProviderSpec, callback_url: str, challenge: str) -> str:
    """The page the user's browser opens to sign in and approve."""
    return spec.oauth["auth_url"] + "?" + urlencode({
        "callback_url": callback_url,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "key_label": KEY_LABEL,
    })


def exchange(spec: ProviderSpec, code: str, verifier: str) -> str:
    """The user's API key for the one-time code. Codes are single-use and
    expire after minutes, so a stale one reads as "sign in again"."""
    url = spec.base_url.rstrip("/") + spec.oauth["exchange_path"]
    try:
        response = http.transport(
            "POST", url, headers=spec.extra_headers(), params=None, files=None, data=None,
            json={"code": code, "code_verifier": verifier, "code_challenge_method": "S256"},
            timeout=http.TIMEOUT,
        )
    except requests.RequestException:
        raise LLMError("network", f"Couldn't reach {spec.label}. Check your internet connection.") from None
    body = http._body(response)
    if response.status_code in (400, 403):
        raise LLMError("invalid_key", "That sign-in was already used or has expired. Sign in again.")
    if response.status_code >= 300:
        raise http.error_from(spec, response.status_code, body)
    key = str((body or {}).get("key") or "").strip() if isinstance(body, dict) else ""
    if not key:
        raise LLMError("bad_response", f"{spec.label} signed you in but sent no key back. Sign in again.")
    return key
