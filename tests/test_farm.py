"""Farm tests — daily-cap clamp and zero-credit stop (no network)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from conso.core import constants as C  # noqa: E402
from conso.net.client import ConsoAPIError  # noqa: E402
from conso.core.config import Settings  # noqa: E402
from conso.ops import pipeline  # noqa: E402
from conso.auth.session import SessionResult  # noqa: E402
from conso.net.client import Session  # noqa: E402
from conso.store.storage import AccountRecord, Store  # noqa: E402


class MemStore(Store):
    def __init__(self):
        import threading

        self._accounts = []
        self._state = {}
        self._lock = threading.Lock()

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

    def save_state(self, state):
        with self._lock:
            self._state = state

    def load_state(self):
        with self._lock:
            return dict(self._state)


class FakeManager:
    def __init__(self, *a, **k):
        pass

    def ensure_session(self, record, solver=None, force_refresh=False):
        return SessionResult(Session(access_token="t", refresh_token="r", user_id="u",
                                     email=record.email), "cached")


def _run(monkeypatch, credited_seq):
    """Run farm with a scripted sequence of credited values."""
    store = MemStore()
    rec = AccountRecord(email="a@b.c", status="active")
    store.add(rec)

    monkeypatch.setattr(pipeline, "SessionManager", FakeManager)

    calls = {"n": 0}

    class _FakeResp:
        status_code = 200

        def json(self):
            # consousers row: a fresh day, nothing earned yet
            return [{"total_zaps": 0, "daily_zaps_earned": 0,
                     "daily_zaps_date": "", "current_streak": 0,
                     "longest_streak": 0, "is_banned": False, "boost_factor": 1.0}]

    class _FakeTransport:
        def request(self, *a, **k):
            return _FakeResp()

    class FakeClient:
        def __init__(self, *a, **k):
            self.session = None
            # farm_turns_for_account now reads the stale-aware daily status,
            # which goes through the transport rather than a client method.
            self.transport = _FakeTransport()
            self.settings = Settings()

        def append_prompt(self, entry, zaps, spend):
            i = calls["n"]
            calls["n"] += 1
            return credited_seq[i]

        def close(self):
            pass

    monkeypatch.setattr(pipeline, "ConsoClient", FakeClient)
    monkeypatch.setattr(pipeline.Pacer, "wait", lambda self: None)

    return pipeline.farm_turns_for_account(
        Settings(), rec, 50, solver=None, store=store
    ), calls


def test_clamp_limits_requests_to_daily_cap(monkeypatch):
    # always credited -> should stop at the clamp (DAILY_TURN_CAP), not 50
    (ok, zaps), calls = _run(monkeypatch, [0.1] * 100)
    assert calls["n"] == C.DAILY_TURN_CAP
    assert ok == C.DAILY_TURN_CAP


def test_zero_credit_stops_early(monkeypatch):
    # credited for 3 turns, then 0 -> stop at the zero
    seq = [0.1, 0.1, 0.1, 0.0]
    (ok, zaps), calls = _run(monkeypatch, seq)
    assert calls["n"] == 4  # 3 credited + 1 zero
    assert ok == 4


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-v"]))
