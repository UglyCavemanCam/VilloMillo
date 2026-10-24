"""API keys, read from Windows Credential Manager. Keys never live in files or logs."""

from __future__ import annotations

import keyring

SERVICE = "villomillo"
YOUTUBE = "youtube_api_key"
JEV = "typesafe_api_key"
CLAUDE = "anthropic_api_key"


class MissingSecret(RuntimeError):
    pass


def get(name: str) -> str:
    value = keyring.get_password(SERVICE, name)
    if not value:
        raise MissingSecret(
            f"No {name!r} stored. Store it with:  .venv\\Scripts\\python.exe -c "
            f"\"import getpass, keyring; keyring.set_password('{SERVICE}', '{name}', getpass.getpass('Key: '))\""
        )
    return value.strip()
