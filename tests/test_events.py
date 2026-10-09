"""
Event-bus + monitor wiring tests — the one-way ``app -> ops`` seam.

The architecture claims progress is reported through ``conso.ops.events`` and
rendered by ``app/monitor.py`` via a subscription. Nothing here touches the
network or a browser: the bus is pure in-process plumbing.

``start_monitor`` / ``stop_monitor`` mutate a process-wide singleton, so an
autouse fixture stops the monitor before and after every test. That keeps the
module hermetic: no subscription can leak into the other test modules.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from conso.app import monitor as monitor_mod  # noqa: E402
from conso.ops import events as bus  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_monitor():
    """Guarantee the process-wide monitor is stopped around every test."""
    monitor_mod.stop_monitor()
    yield
    monitor_mod.stop_monitor()


def _subscriber_count() -> int:
    # The bus exposes no public counter; the test needs the raw list to prove
    # subscribe/unsubscribe actually add and remove listeners.
    return len(bus._subscribers)


# ---------------------------------------------------------------------------
# subscribe / unsubscribe
# ---------------------------------------------------------------------------


def test_subscribe_returns_unsubscribe_and_double_unsubscribe_is_harmless():
    seen: list[tuple[str, dict]] = []
    unsub = bus.subscribe(lambda name, data: seen.append((name, data)))
    assert _subscriber_count() == 1

    bus.emit("PING", n=1)
    assert seen == [("PING", {"n": 1})]

    unsub()
    assert _subscriber_count() == 0
    unsub()  # second call must not raise
    assert _subscriber_count() == 0

    bus.emit("PING", n=2)
    assert seen == [("PING", {"n": 1})]  # no longer listening


def test_emit_reaches_every_subscriber_with_name_and_data():
    calls: list[tuple[str, str, dict]] = []
    unsub_a = bus.subscribe(lambda name, data: calls.append(("a", name, data)))
    unsub_b = bus.subscribe(lambda name, data: calls.append(("b", name, data)))
    try:
        bus.emit("ACCOUNT_STAGE", email="a@b.c", stage="captcha")
        assert calls == [
            ("a", "ACCOUNT_STAGE", {"email": "a@b.c", "stage": "captcha"}),
            ("b", "ACCOUNT_STAGE", {"email": "a@b.c", "stage": "captcha"}),
        ]
    finally:
        unsub_a()
        unsub_b()


def test_raising_subscriber_does_not_break_emit_or_later_subscribers():
    calls: list[str] = []

    def boom(name, data):
        raise RuntimeError("subscriber exploded")

    unsub_boom = bus.subscribe(boom)
    unsub_ok = bus.subscribe(lambda name, data: calls.append(name))
    try:
        bus.emit("LOG", message="survives boom")  # must not raise
        assert calls == ["LOG"]  # later subscriber still ran
    finally:
        unsub_boom()
        unsub_ok()


def test_emit_with_zero_subscribers_is_a_noop():
    assert _subscriber_count() == 0
    bus.emit("BATCH_COMPLETED", status="COMPLETED")  # must not raise


# ---------------------------------------------------------------------------
# monitor subscription lifecycle
# ---------------------------------------------------------------------------


def test_start_monitor_subscribes_and_stop_unsubscribes_idempotently():
    mon = monitor_mod.start_monitor("run", target=3)
    assert _subscriber_count() == 1
    assert monitor_mod.get_monitor() is mon

    monitor_mod.stop_monitor("DONE")
    assert _subscriber_count() == 0
    assert monitor_mod.get_monitor() is None

    monitor_mod.stop_monitor()  # idempotent
    assert _subscriber_count() == 0
    assert monitor_mod.get_monitor() is None


def test_second_start_monitor_reuses_single_subscription():
    mon = monitor_mod.start_monitor("run", target=3)
    again = monitor_mod.start_monitor("register", target=99)
    assert again is mon
    assert _subscriber_count() == 1
    assert mon.state.target == 3  # reuse keeps the first call's state


# ---------------------------------------------------------------------------
# events -> Monitor.state
# ---------------------------------------------------------------------------


def test_monitor_state_updates_from_emitted_events():
    mon = monitor_mod.start_monitor("run", target=1)
    st = mon.state

    bus.emit("BATCH_STARTED", target=3)
    assert st.status == "RUNNING"
    assert st.phase == "registering"
    assert st.target == 3

    bus.emit("ACCOUNT_STAGE", stage="solving")
    assert st.phase == "solving"

    bus.emit("ACCOUNT_COMPLETED", email="a@b.c", zaps=0.75)
    assert (st.done, st.ok) == (1, 1)
    assert st.zaps == pytest.approx(0.75)

    bus.emit("ACCOUNT_FAILED", email="x@y.z", reason="turnstile timeout")
    assert (st.done, st.failed) == (2, 1)
    assert st.logs[-1] == ("fail", "x@y.z turnstile timeout")

    bus.emit("TURN_CREDITED", zaps=0.25)
    assert st.zaps == pytest.approx(1.0)

    bus.emit("LOG", message="hello pipeline")
    assert ("info", "hello pipeline") in st.logs

    bus.emit("BATCH_COMPLETED", status="COMPLETED")
    assert st.status == "COMPLETED"
    assert st.phase == "done"


def test_monitor_log_tail_is_capped():
    mon = monitor_mod.start_monitor("run")
    for i in range(10):
        bus.emit("LOG", message=f"line {i}")
    assert len(mon.state.logs) == 6  # _TAIL
    assert mon.state.logs[0] == ("info", "line 4")
    assert mon.state.logs[-1] == ("info", "line 9")
