"""
Process-wide event bus for pipeline progress.

WHY this exists: the ops layer (scheduler / orchestrator / pipeline) needs to
report progress — BATCH_STARTED, ACCOUNT_STAGE, ACCOUNT_COMPLETED,
ACCOUNT_FAILED, ACCOUNT_QUEUED, BATCH_COMPLETED, TURN_CREDITED, LOG — but it
must not import `conso.app` / `conso.monitor` to do so. That import would make
the dependency direction circular (app imports ops to run the work, ops would
import app to report it). Instead ops only *emits* here and the app layer
*subscribes*, keeping the direction one-way: app -> ops.

A listener must never be able to break a run: `emit` snapshots the subscriber
list under the lock, then invokes each listener outside the lock inside
try/except, so a raising or slow subscriber cannot propagate into the pipeline
or deadlock the bus.
"""

from __future__ import annotations

import threading
from typing import Callable

_subscribers: list[Callable[[str, dict], None]] = []
_lock = threading.Lock()


def subscribe(fn: Callable[[str, dict], None]) -> Callable[[], None]:
    """Register a listener; returns an unsubscribe callable (idempotent)."""
    with _lock:
        _subscribers.append(fn)

    def _unsubscribe() -> None:
        with _lock:
            try:
                _subscribers.remove(fn)
            except ValueError:
                pass  # already removed; unsubscribing twice is harmless

    return _unsubscribe


def emit(name: str, **data: object) -> None:
    """Publish an event to every subscriber. NEVER raises."""
    with _lock:
        listeners = list(_subscribers)
    for fn in listeners:
        try:
            fn(name, data)
        except Exception:
            pass
