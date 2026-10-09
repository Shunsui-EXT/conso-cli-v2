"""
Daily earning loop.

Runs the full earn cycle for stored accounts on a schedule:

  every cycle (default daily):
    - ensure session (refresh; no Turnstile unless the chain broke)
    - claim missions (check-in, tweet, article) — idempotent per day
    - farm turns up to the daily cap, stopping at the first zero-credit turn
    - record the run in a local ledger

The loop is resumable: it persists last-run state per account and skips
accounts that already completed today's cycle, so a restart or overlap does not
double-submit or trip the anti-abuse cap.

Progress is published on the shared event bus (:mod:`conso.ops.events`);
this module never imports application/TUI code.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable

from ..auth.session import SessionManager
from ..core.config import Settings
from ..core.constants import DAILY_ZAP_CAP
from ..net.client import ConsoAPIError, ConsoClient
from ..store.storage import AccountRecord, Store
from .earnings import run_earnings
from .events import emit
from .limits import daily_headroom, get_daily_status
from .pipeline import farm_turns_for_account

LogFn = Callable[[str], None]


def _today_utc() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def _log(logger: LogFn | None, message: str) -> None:
    if logger:
        logger(message)


@dataclass
class LoopConfig:
    turns: int = 10                     # turns per account per day (<= DAILY_TURN_CAP)
    interval_seconds: int = 24 * 3600   # between cycles
    claim_missions: bool = True
    max_cycles: int = 0                 # 0 = run forever
    daily_cap: float = 0.0              # 0 = no cap
    parallel_workers: int = 1           # accounts farmed concurrently
    # Skip farming an account whose remaining daily budget is below this. An
    # account at 20.83/21 still accepts turns, but each one is sized down to
    # ~0.02 zaps, so filling the last fraction costs a dozen requests for
    # nothing. 0 disables the skip.
    min_remaining_zaps: float = 1.0
    # Snapshot data/ into backups/ when a cycle ends. The cycle ROTATES every
    # refresh token, so the newest tokens only exist in the store once it
    # returns: a crash before the next snapshot would strand the accounts on
    # the OTP path. 0 disables the snapshot.
    backup_keep: int = 20


@dataclass
class CycleResult:
    day: str
    per_account: dict[str, dict] = field(default_factory=dict)

    @property
    def zaps(self) -> float:
        return sum(v.get("zaps", 0.0) for v in self.per_account.values())


class DailyLoop:
    """Schedule and run the daily earn cycle, resumable across restarts."""

    def __init__(
        self,
        settings: Settings,
        store: Store,
        *,
        config: LoopConfig | None = None,
        solver=None,
        verifier=None,
        logger: LogFn | None = None,
    ) -> None:
        self.settings = settings
        self.store = store
        self.config = config or LoopConfig()
        self.solver = solver
        self.logger = logger
        self.verifier = verifier
        self.manager = SessionManager(settings, store, verifier=verifier)
        self.only_emails: set[str] = set()  # optional filter: run these accounts only
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    # -- ledger ------------------------------------------------------------
    def _ledger(self) -> dict:
        return self.store.load_state().get("daily_loop", {})

    def _mark_done(self, day: str, email: str, info: dict) -> None:
        state = self.store.load_state()
        loop = state.setdefault("daily_loop", {})
        loop.setdefault(day, {})[email] = info
        # keep only the last 14 days
        for old in sorted(loop)[:-14]:
            loop.pop(old, None)
        state["daily_loop"] = loop
        self.store.save_state(state)

    def _done_today(self, day: str, email: str) -> bool:
        return email in self._ledger().get(day, {})

    # -- one account -------------------------------------------------------
    def run_account(self, record: AccountRecord, *, force: bool = False) -> dict:
        day = _today_utc()
        emit("ACCOUNT_QUEUED", email=record.email)
        if not force and self._done_today(day, record.email):
            emit("ACCOUNT_STAGE", email=record.email, stage="skipped")
            return {"status": "skipped", "reason": "already ran today"}

        client = ConsoClient(self.settings)
        emit("ACCOUNT_STAGE", email=record.email, stage="session")
        try:
            result = self.manager.ensure_session(record, solver=self.solver)
            client.session = result.session
        except ConsoAPIError:
            # ensure_session exhausted cache+refresh+password -> full recovery
            # (adds the email-OTP path, reusing the stored signup address).
            try:
                result = self.manager.recover(
                    record, solver=self.solver, verifier=self.verifier
                )
                client.session = result.session
            except ConsoAPIError as exc:
                client.close()
                emit("ACCOUNT_FAILED", email=record.email, reason=f"session: {exc}")
                return {"status": "failed", "reason": f"session: {exc}"}

        info: dict = {"status": "ok", "session": result.source, "zaps": 0.0}
        try:
            status = get_daily_status(client)
            if status and status.is_banned:
                emit("ACCOUNT_FAILED", email=record.email, reason="account_banned")
                return {"status": "banned", "reason": "account_banned"}
            if self.config.daily_cap and status:
                if status.effective_daily >= self.config.daily_cap:
                    emit("ACCOUNT_STAGE", email=record.email, stage="capped")
                    return {"status": "capped", "reason": f"daily={status.effective_daily}"}

            before = status.total_zaps if status else 0.0

            if self.config.claim_missions:
                emit("ACCOUNT_STAGE", email=record.email, stage="missions")
                summary = run_earnings(client, logger=self.logger)
                info["missions"] = summary.zaps_earned
                before = summary.total_zaps_after or before
                # Missions add to the daily counter, so fold them in before
                # deciding whether there is anything left worth farming.
                if status:
                    status.daily_zaps_earned += summary.zaps_earned
        finally:
            client.close()

        # Skip accounts that are effectively full. At 20.83/21 the server still
        # accepts turns but sizes each one down to ~0.02 zaps, so closing the
        # last fraction costs a dozen requests and adds nothing.
        threshold = self.config.min_remaining_zaps
        if threshold > 0 and status:
            left = daily_headroom(status, DAILY_ZAP_CAP)
            if left is not None and left < threshold:
                _log(self.logger, f"loop: {record.email} skip farm "
                                  f"(headroom {left:.2f} < {threshold:.2f})")
                emit("ACCOUNT_STAGE", email=record.email, stage="capped")
                info["status"] = "capped"
                info["reason"] = f"headroom={left:.2f}"
                self._mark_done(day, record.email, info)
                emit("ACCOUNT_COMPLETED", email=record.email, zaps=info["zaps"],
                     proxy=record.proxy or "")
                return info

        emit("ACCOUNT_STAGE", email=record.email, stage="farm")
        ok, farmed = farm_turns_for_account(
            self.settings, record, self.config.turns,
            solver=self.solver, store=self.store, verifier=self.verifier, logger=self.logger,
        )
        info["turns"] = ok
        info["zaps"] = round(info.get("zaps", 0.0) + farmed, 2)
        self._mark_done(day, record.email, info)
        emit("ACCOUNT_COMPLETED", email=record.email, zaps=info["zaps"], proxy=record.proxy or "")
        return info

    # -- full cycle --------------------------------------------------------
    def run_cycle(self, *, force: bool = False) -> CycleResult:
        day = _today_utc()
        result = CycleResult(day=day)
        records = [
            r for r in self.store.all()
            if r.status in ("active", "farmed")
            and (not self.only_emails or r.email in self.only_emails)
        ]
        if not records:
            return result

        emit("BATCH_STARTED", target=len(records),
             solver=type(self.solver).__name__ if self.solver else "",
             referral="")
        try:
            return self._run_records(records, force, result)
        finally:
            emit("BATCH_COMPLETED", status="STOPPED" if self._stop.is_set() else "COMPLETED")
            self._snapshot()

    def _snapshot(self) -> None:
        """Copy the store aside once a cycle ends.

        Runs even when the cycle was stopped part-way: the accounts that DID
        run have already rotated their tokens, so their newest credentials are
        only in the store — snapshotting now is what keeps them reachable.
        A failure here must never fail the cycle.
        """
        if self.config.backup_keep <= 0:
            return
        try:
            from .backup import snapshot

            snapshot(self.store.data_dir, keep=self.config.backup_keep,
                     logger=lambda msg: _log(self.logger, msg))
        except Exception as exc:  # noqa: BLE001 - insurance must not break earning
            _log(self.logger, f"backup: skipped ({type(exc).__name__}: {exc})")

    def _run_records(self, records, force: bool, result: CycleResult) -> CycleResult:
        workers = max(1, self.config.parallel_workers)
        if workers == 1 or len(records) == 1:
            for record in records:
                if self._stop.is_set():
                    break
                result.per_account[record.email] = self._run_one(record, force)
            return result

        # Parallel farm/earn across accounts. This is HTTP-only (no solve), so a
        # worker pool genuinely scales — unlike registration, which serializes
        # on the solver lock.
        from concurrent.futures import ThreadPoolExecutor, as_completed

        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(self._run_one, r, force): r for r in records}
            for future in as_completed(futures):
                record = futures[future]
                try:
                    info = future.result()
                except Exception as exc:  # noqa: BLE001
                    info = {"status": "failed", "reason": f"{type(exc).__name__}: {exc}"[:160]}
                result.per_account[record.email] = info
        return result

    def _run_one(self, record: AccountRecord, force: bool) -> dict:
        try:
            info = self.run_account(record, force=force)
        except Exception as exc:  # noqa: BLE001
            info = {"status": "failed", "reason": f"{type(exc).__name__}: {exc}"[:160]}
        _log(self.logger, f"loop: {record.email} -> {info.get('status')} "
                          f"{info.get('reason', '')} +{info.get('zaps', 0)} zaps")
        return info

    def run_forever(self) -> None:
        cycles = 0
        while not self._stop.is_set():
            if self.config.max_cycles and cycles >= self.config.max_cycles:
                break
            _log(self.logger, f"loop: cycle {cycles + 1} start")
            result = self.run_cycle()
            _log(self.logger, f"loop: cycle {cycles + 1} done, +{round(result.zaps, 2)} zaps")
            cycles += 1
            if self.config.max_cycles and cycles >= self.config.max_cycles:
                break
            # interruptible sleep
            end = time.monotonic() + self.config.interval_seconds
            while time.monotonic() < end and not self._stop.is_set():
                time.sleep(min(5, end - time.monotonic()))
