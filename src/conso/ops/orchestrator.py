"""
Full pipeline orchestrator — one entry point for the whole chain.

Phases (each optional):
  register : provision N accounts (parallel via the adaptive engine)
  earn     : claim missions + farm turns for new accounts
  loop     : recurring daily earn cycle for all stored accounts

Resumable: registration state is checkpointed in the store. All progress
events go through the shared bus (:mod:`conso.ops.events`), so this module
stays free of application/TUI imports.
"""

from __future__ import annotations

import signal
import threading
from dataclasses import dataclass
from typing import Callable

from ..core.config import Settings
from ..net.transport import Transport
from ..solver import build_solver
from ..store.storage import Store
from .events import emit
from .pipeline import run_registration
from .scheduler import DailyLoop, LoopConfig
from .verifiers import build_verifier

LogFn = Callable[[str], None]


@dataclass
class PipelineConfig:
    register_count: int = 0        # 0 = skip registration
    earn: bool = True              # earn for newly registered accounts
    turns: int = 10                # turns per account per earn pass
    loop: bool = False             # run the recurring daily loop after
    interval_hours: float = 24.0
    loop_cycles: int = 0           # 0 = forever (only if loop=True)
    referral: str = ""
    workers: int = 1               # farm parallelism
    daily_cap: float = 0.0
    only_new: bool = False         # earn only the accounts registered this run
    solver_concurrent: int = 0     # 0 = inherit SOLVER_MAX_CONCURRENT


class Orchestrator:
    """Drives register -> earn -> loop with a single config and graceful stop."""

    def __init__(self, settings: Settings, *, config: PipelineConfig,
                 logger: LogFn | None = None) -> None:
        self.settings = settings
        self.config = config
        self.logger = logger
        self.store = Store(settings.data_dir)
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    # -- phases ------------------------------------------------------------
    def run(self) -> dict:
        summary: dict = {"registered": 0, "active": 0, "earned_zaps": 0.0}
        if self.config.solver_concurrent:
            import os
            os.environ["SOLVER_MAX_CONCURRENT"] = str(self.config.solver_concurrent)
        solver = build_solver(Transport(self.settings))
        verifier = build_verifier(self.settings)

        new_emails: set[str] = set()
        if self.config.register_count > 0 and not self._stop.is_set():
            reg = self._phase_register(solver, verifier)
            summary.update(reg)
            new_emails = reg.get("emails", set())

        if self.config.earn and not self._stop.is_set():
            summary["earned_zaps"] += self._phase_earn(solver, verifier, only=new_emails)

        if self.config.loop and not self._stop.is_set():
            self._phase_loop(solver, verifier)

        emit("BATCH_COMPLETED", status="STOPPED" if self._stop.is_set() else "COMPLETED")
        return summary

    def _phase_register(self, solver, verifier) -> dict:
        referral = self.config.referral or self.settings.default_referral_code
        self._log(f"pipeline: registering {self.config.register_count} account(s)")
        results = run_registration(
            self.settings, self.config.register_count,
            referral_code=referral, store=self.store,
            verifier=verifier, solver=solver, logger=self.logger,
        )
        active = sum(1 for r in results if r.status == "active")
        emails = {r.email for r in results if r.status == "active"}
        self._log(f"pipeline: register done {active}/{len(results)} active")
        return {"registered": len(results), "active": active, "emails": emails}

    def _phase_earn(self, solver, verifier, *, only: set[str] | None = None) -> float:
        """Earn for accounts that have not earned today (uses DailyLoop ledger).

        `only` (when non-empty) restricts earning to those emails — used by
        --only-new so a run does not re-farm the whole store.
        """
        runner = DailyLoop(
            self.settings, self.store,
            config=LoopConfig(turns=self.config.turns, parallel_workers=self.config.workers,
                              daily_cap=self.config.daily_cap, max_cycles=1),
            solver=solver, verifier=verifier, logger=self.logger,
        )
        if only:
            runner.only_emails = set(only)
            self._log(f"pipeline: earning {len(only)} new account(s)")
        else:
            self._log("pipeline: earning (missions + turns)")
        result = runner.run_cycle()
        self._log(f"pipeline: earn done +{round(result.zaps, 2)} zaps")
        return result.zaps

    def _phase_loop(self, solver, verifier) -> None:
        runner = DailyLoop(
            self.settings, self.store,
            config=LoopConfig(
                turns=self.config.turns,
                interval_seconds=int(self.config.interval_hours * 3600),
                parallel_workers=self.config.workers,
                daily_cap=self.config.daily_cap,
                max_cycles=self.config.loop_cycles,
            ),
            solver=solver, verifier=verifier, logger=self.logger,
        )
        # Graceful stop: SIGINT/SIGTERM set the loop's event first so an
        # in-flight cycle finishes its account, then stop this orchestrator.
        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, lambda *_: (self._log("pipeline: stop requested"), runner.stop(), self.stop()))
        self._log(f"pipeline: daily loop start (interval={self.config.interval_hours}h)")
        runner.run_forever()

    def _log(self, msg: str) -> None:
        if self.logger:
            self.logger(msg)
        emit("LOG", message=msg)
