"""Keys go to the credential store, never to a file, and are never printed."""

from __future__ import annotations

import pytest

from villomillo import cli, secrets


@pytest.fixture
def store(monkeypatch):
    saved: dict[tuple[str, str], str] = {}
    monkeypatch.setattr(secrets.keyring, "get_password", lambda service, name: saved.get((service, name)))
    monkeypatch.setattr(secrets.keyring, "set_password", lambda service, name, value: saved.__setitem__((service, name), value))
    return saved


def test_a_key_is_stored_trimmed_and_read_back(store):
    secrets.store(secrets.WHOP, "  whop_abc123 \n")
    assert store[(secrets.SERVICE, secrets.WHOP)] == "whop_abc123"
    assert secrets.get(secrets.WHOP) == "whop_abc123" and secrets.is_stored(secrets.WHOP)


def test_an_empty_key_is_refused(store):
    with pytest.raises(ValueError):
        secrets.store(secrets.WHOP, "   ")
    assert not secrets.is_stored(secrets.WHOP)


def test_the_key_command_stores_without_printing_the_key(store, monkeypatch, capsys):
    monkeypatch.setattr("getpass.getpass", lambda prompt="": "whop_secret_value")
    cli.main(["key", "whop"])
    out = capsys.readouterr()
    assert "whop_secret_value" not in out.out + out.err
    assert secrets.get(secrets.WHOP) == "whop_secret_value"


def test_the_key_command_with_no_name_lists_what_is_stored(store, capsys):
    secrets.store(secrets.WHOP, "x")
    cli.main(["key"])
    lines = capsys.readouterr().out.splitlines()
    assert any(line.startswith("whop") and line.endswith("stored") and "not" not in line for line in lines)
    assert any(line.startswith("claude") and "not stored" in line for line in lines)


def test_an_empty_paste_stores_nothing_and_says_so(store, monkeypatch):
    monkeypatch.setattr("getpass.getpass", lambda prompt="": "  ")
    with pytest.raises(SystemExit) as stopped:
        cli.main(["key", "whop"])
    assert "Nothing stored" in str(stopped.value) and not secrets.is_stored(secrets.WHOP)
