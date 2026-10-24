"""API keys, read from Windows Credential Manager. Keys never live in files or logs."""

from __future__ import annotations

import keyring

SERVICE = "villomillo"
YOUTUBE = "youtube_api_key"
JEV = "typesafe_api_key"
CLAUDE = "anthropic_api_key"
WHOP = "whop_api_key"

# What `python -m villomillo key <name>` can store, by the short name you type.
KEYS = {"whop": WHOP, "youtube": YOUTUBE, "jev": JEV, "claude": CLAUDE}


class MissingSecret(RuntimeError):
    pass


def get_first(*names: str) -> str:
    """The first of these keys that is stored: a channel's own key, then the shared one."""
    for name in names:
        if keyring.get_password(SERVICE, name):
            return get(name)
    return get(names[-1])  # none stored: raises the usual explanatory error for the shared key


def is_stored(name: str) -> bool:
    return bool((keyring.get_password(SERVICE, name) or "").strip())


def store(name: str, value: str) -> None:
    """Put a key in the operating system's credential store. It is never written to a file."""
    value = value.strip()
    if not value:
        raise ValueError("the key is empty")
    keyring.set_password(SERVICE, name, value)


def get(name: str) -> str:
    value = keyring.get_password(SERVICE, name)
    if not value:
        raise MissingSecret(
            f"No {name!r} stored. Store it with:  .venv\\Scripts\\python.exe -c "
            f"\"import getpass, keyring; keyring.set_password('{SERVICE}', '{name}', getpass.getpass('Key: '))\""
        )
    return value.strip()
