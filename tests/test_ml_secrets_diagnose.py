"""Tests for SECRETS_KEY diagnosis + normalisation (tools/mirror_leech/Secrets.py).

Regression: a key pasted with surrounding quotes (common in platform env
UIs and hand-edited .env files) failed Fernet parsing and the admin panel
reported it as "missing" — sending the operator hunting for an env var
they had already set.
"""

from cryptography.fernet import Fernet

from config import Config
from tools.mirror_leech import Secrets

VALID_KEY = Fernet.generate_key().decode()


def _with_key(monkeypatch, value):
    monkeypatch.setattr(Config, "SECRETS_KEY", value)


def test_diagnose_missing(monkeypatch):
    _with_key(monkeypatch, None)
    assert Secrets.diagnose() == "missing"
    _with_key(monkeypatch, "   ")
    assert Secrets.diagnose() == "missing"


def test_diagnose_invalid(monkeypatch):
    _with_key(monkeypatch, "definitely-not-a-fernet-key")
    assert Secrets.diagnose() == "invalid"
    assert Secrets.is_available() is False


def test_diagnose_ok(monkeypatch):
    _with_key(monkeypatch, VALID_KEY)
    assert Secrets.diagnose() == "ok"
    assert Secrets.is_available() is True


def test_quoted_key_is_accepted(monkeypatch):
    # .env files / platform config UIs love to smuggle in quotes.
    _with_key(monkeypatch, f'"{VALID_KEY}"')
    assert Secrets.diagnose() == "ok"
    assert Secrets.is_available() is True

    _with_key(monkeypatch, f"'{VALID_KEY}'")
    assert Secrets.diagnose() == "ok"


def test_whitespace_key_is_accepted(monkeypatch):
    _with_key(monkeypatch, f"  {VALID_KEY}\n")
    assert Secrets.diagnose() == "ok"


def test_roundtrip_with_quoted_key(monkeypatch):
    _with_key(monkeypatch, f'"{VALID_KEY}"')
    token = Secrets.encrypt("hello world")
    assert Secrets.decrypt(token) == "hello world"
