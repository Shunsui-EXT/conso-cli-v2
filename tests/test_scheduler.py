"""Scheduler tests — ledger resumability and cycle accounting."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from conso.core.config import Settings  # noqa: E402
from conso.ops.scheduler import DailyLoop, LoopConfig  # noqa: E402
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


def test_ledger_roundtrip():
    store = MemStore()
    loop = DailyLoop(Settings(), store, config=LoopConfig())
    loop._mark_done("2026-10-02", "a@b.c", {"zaps": 1.5})
    assert loop._done_today("2026-10-02", "a@b.c")
    assert not loop._done_today("2026-10-02", "x@y.z")
    assert not loop._done_today("2026-10-03", "a@b.c")


def test_run_account_skips_when_done(monkeypatch):
    from conso.ops.scheduler import _today_utc

    store = MemStore()
    rec = AccountRecord(email="a@b.c", status="active")
    store.add(rec)
    loop = DailyLoop(Settings(), store, config=LoopConfig())
    loop._mark_done(_today_utc(), "a@b.c", {"zaps": 2.0})
    info = loop.run_account(rec)
    assert info["status"] == "skipped"


def test_run_cycle_records_per_account(monkeypatch):
    store = MemStore()
    rec = AccountRecord(email="a@b.c", status="active")
    store.add(rec)
    loop = DailyLoop(Settings(), store, config=LoopConfig())

    monkeypatch.setattr(loop, "run_account", lambda r, force=False: {"status": "ok", "zaps": 3.0})
    result = loop.run_cycle()
    assert result.per_account["a@b.c"]["zaps"] == 3.0
    assert result.zaps == 3.0


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-v"]))
