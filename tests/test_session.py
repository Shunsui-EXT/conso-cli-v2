"""Session manager tests — token reuse, refresh, and rotation persistence."""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from conso.auth import session as session_mod  # noqa: E402
from conso.auth.session import SessionManager, jwt_expiry  # noqa: E402
from conso.core.config import Settings  # noqa: E402
from conso.net.client import Session  # noqa: E402
from conso.store.storage import AccountRecord, Store  # noqa: E402


class FakeStore(Store):
    def __init__(self):
        self._accounts = []
        self._lock = __import__("threading").Lock()

    def add(self, account):
        with self._lock:
            self._accounts.append(account)

    def update(self, email, **fields):
        with self._lock:
            for a in self._accounts:
                if a.email == email:
                    for k, v in fields.items():
                        setattr(a, k, v)

    def all(self):
        with self._lock:
            return list(self._accounts)


def _manager(monkeypatch, store):
    settings = Settings()
    m = SessionManager(settings, store)
    return m


def test_jwt_expiry_reads_exp():
    # header.payload.signature with exp in payload
    import base64, json

    payload = base64.urlsafe_b64encode(json.dumps({"exp": 1234567890}).encode()).decode().rstrip("=")
    token = f"h.{payload}.s"
    assert jwt_expiry(token) == 1234567890
    assert jwt_expiry("not-a-jwt") == 0


def test_cached_when_valid(monkeypatch):
    store = FakeStore()
    rec = AccountRecord(email="a@b.c", access_token="h.e.s", refresh_token="rt",
                        expires_at=int(time.time()) + 3600)
    store.add(rec)
    m = _manager(monkeypatch, store)
    # patch expiry reader to treat token as valid
    monkeypatch.setattr(session_mod, "jwt_expiry", lambda t: int(time.time()) + 3600)
    result = m.ensure_session(rec)
    assert result.source == "cached"


def test_refresh_persists_rotated_token(monkeypatch):
    store = FakeStore()
    rec = AccountRecord(email="a@b.c", access_token="", refresh_token="old", expires_at=0)
    store.add(rec)
    m = _manager(monkeypatch, store)

    def fake_refresh(self, refresh_token):
        assert refresh_token == "old"
        return Session(access_token="newat", refresh_token="newrt", user_id="u1",
                       email="a@b.c", expires_at=int(time.time()) + 3600)

    monkeypatch.setattr(session_mod.ConsoClient, "refresh", fake_refresh)
    result = m.ensure_session(rec)
    assert result.source == "refreshed"
    saved = store.all()[0]
    assert saved.refresh_token == "newrt"
    assert saved.access_token == "newat"
    assert saved.expires_at > time.time()


def test_recover_falls_back_to_otp(monkeypatch):
    """When refresh + password fail, recover uses the email OTP path."""
    store = FakeStore()
    rec = AccountRecord(email="a@b.c", access_token="", refresh_token="bad", password="pw")
    store.add(rec)
    m = _manager(monkeypatch, store)

    # refresh fails
    def fail_refresh(self, rt):
        raise session_mod.ConsoAPIError("refresh failed")

    # password fails
    def fail_login(self, email, pw, captcha_token=None):
        raise session_mod.ConsoAPIError("login failed")

    # otp send ok; verify returns a session
    def fake_otp(self, email, captcha_token=None):
        return {}

    def fake_verify(self, email, token):
        assert token == "123456"
        return Session(access_token="newat", refresh_token="newrt", user_id="u",
                       email=email, expires_at=int(time.time()) + 3600)

    monkeypatch.setattr(session_mod.ConsoClient, "refresh", fail_refresh)
    monkeypatch.setattr(session_mod.ConsoClient, "sign_in_password", fail_login)
    monkeypatch.setattr(session_mod.ConsoClient, "sign_in_otp", fake_otp)
    monkeypatch.setattr(session_mod.ConsoClient, "verify_otp", fake_verify)

    class FakeVerifier:
        def fetch_latest_otp(self, address):
            return "123456"

    result = m.recover(rec, verifier=FakeVerifier(), otp_timeout=5)
    assert result.source == "otp"
    assert store.all()[0].access_token == "newat"


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-v"]))
