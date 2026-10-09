"""
Earning subcommands: farm, earn, loop, pipeline, report.

Ported from the reference ``conso/cli.py``. The daily-loop and pipeline
commands still install SIGINT/SIGTERM handlers for a graceful stop, but the
live progress panel now arrives through the ops event bus (see
``conso.app.monitor.start_monitor``) instead of direct monitor calls.
"""

from __future__ import annotations

import json
import os
import random
import signal
import socket
from datetime import datetime, timezone

import typer

from .. import ui
from ...auth.session import SessionManager
from ...core import economy
from ...core.config import Settings
from ...net.client import ConsoAPIError, ConsoClient
from ...net.transport import Transport
from ...ops.earnings import MISSIONS, get_account_row, run_earnings
from ...ops.notify import notify as tg_notify
from ...ops.pipeline import farm_turns_for_account, make_synthetic_turn
from ...ops.scheduler import DailyLoop, LoopConfig
from ...ops.verifiers import build_verifier
from ...solver import build_solver, ensure_solver_ready
from ...store.storage import Store
from ..monitor import start_monitor, stop_monitor

_log = ui.log_auto
_mirror = ui.mirror


def register(app: typer.Typer) -> None:
    """Attach this module's commands to ``app``."""
    app.command()(farm)
    app.command()(earn)
    app.command()(loop)
    app.command()(pipeline)
    app.command()(report)


def farm(
    turns: int = typer.Option(10, help="Turns per account."),
    email: str = typer.Option("", help="Farm a single account by email."),
    earn: bool = typer.Option(True, "--earn/--no-earn", help="Also claim daily/bonus missions."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Compute payloads only, no network."),
) -> None:
    """Submit synthetic AI-usage turns to earn zaps."""
    settings = Settings.from_env()

    if dry_run:
        rng = random.Random()
        for i in range(turns):
            spec = make_synthetic_turn(rng, i)
            account = economy.account_turn(
                platform=spec.platform, model=spec.model,
                prompt_text=spec.prompt_text, response_text=spec.response_text,
                has_non_image_attachment=spec.has_non_image_attachment,
            )
            entry = economy.build_entry(account, timestamp=economy.js_isoformat(datetime.now(timezone.utc)))
            _log(f"[dry-run] {spec.platform}/{spec.model} zaps={account.zaps} entry={json.dumps(entry)}")
        return

    store = Store(settings.data_dir)
    records = store.all()
    if email:
        records = [r for r in records if r.email == email]
    if not records:
        _log("farm: no accounts in store")
        raise typer.Exit(code=1)

    total_ok = 0
    total_zaps = 0.0
    solver = build_solver(Transport(settings))
    verifier = build_verifier(settings)
    manager = SessionManager(settings, store, verifier=verifier)
    for record in records:
        # Claim missions first (cheap zaps), then farm turns.
        if earn:
            client = ConsoClient(settings)
            try:
                client.session = manager.ensure_session(record, solver=solver).session
                summary = run_earnings(client, logger=_log)
                mission_zaps = summary.total_zaps_after - summary.total_zaps_before
                total_zaps += mission_zaps
                _log(f"farm: {record.email} missions +{round(mission_zaps, 2)} zaps")
            except ConsoAPIError as exc:
                _log(f"farm: {record.email} earn skipped: {exc}")
            finally:
                client.close()

        ok, zaps = farm_turns_for_account(
            settings, record, turns, solver=solver, store=store,
            verifier=verifier, logger=_log,
        )
        total_ok += ok
        total_zaps += zaps
        store.update(record.email, total_zaps=zaps, status="farmed" if ok else record.status)
    _log(f"farm done: {total_ok} turns accepted, ~{round(total_zaps, 2)} zaps total")


def earn(
    email: str = typer.Option("", help="Account email (empty = all accounts)."),
    referral: str = typer.Option("", help="Redeem a referral code."),
    access: str = typer.Option("", help="Redeem an access code."),
    missions: str = typer.Option("", help="Comma-separated mission ids (default: all)."),
    list_only: bool = typer.Option(False, "--list", help="List available missions and exit."),
) -> None:
    """Claim daily/bonus missions and codes to earn zaps."""
    if list_only:
        for mid, meta in MISSIONS.items():
            _log(f"{mid} :: +{meta['reward']} zaps :: {meta['kind']} :: {meta['label']}")
        return

    settings = Settings.from_env()
    store = Store(settings.data_dir)
    manager = SessionManager(settings, store)
    solver = build_solver(Transport(settings))
    records = store.all()
    if email:
        records = [r for r in records if r.email == email]
    if not records:
        _log("earn: no accounts")
        raise typer.Exit(code=1)

    target_missions = [m.strip() for m in missions.split(",") if m.strip()] or None
    for record in records:
        client = ConsoClient(settings)
        try:
            client.session = manager.ensure_session(record, solver=solver).session
        except ConsoAPIError as exc:
            _log(f"earn: {record.email} no session: {exc}")
            continue
        try:
            summary = run_earnings(
                client, missions=target_missions, referral_code=referral,
                access_code=access, logger=_log,
            )
            delta = summary.total_zaps_after - summary.total_zaps_before
            _log(
                f"earn: {record.email} +{round(delta, 2)} zaps "
                f"(total {round(summary.total_zaps_after, 2)}) "
                f"rank {summary.rank_before}->{summary.rank_after}"
            )
            store.update(record.email, total_zaps=summary.total_zaps_after)
        finally:
            client.close()


def loop(
    turns: int = typer.Option(10, help="Turns per account per day (<= daily cap)."),
    interval_hours: float = typer.Option(24.0, help="Hours between cycles."),
    cycles: int = typer.Option(0, help="Number of cycles (0 = forever)."),
    once: bool = typer.Option(False, "--once", help="Run a single cycle now and exit."),
    force: bool = typer.Option(False, "--force", help="Re-run accounts already done today."),
    no_missions: bool = typer.Option(False, "--no-missions", help="Skip mission claims."),
    daily_cap: float = typer.Option(0.0, help="Stop an account at this daily zaps (0 = off)."),
    email: str = typer.Option("", help="Only run this account (empty = all)."),
    workers: int = typer.Option(1, help="Farm this many accounts concurrently (HTTP-only)."),
    min_remaining: float = typer.Option(
        -1.0, "--min-remaining",
        help="Skip farming an account with less than this daily headroom (0 = off, -1 = .env)."),
) -> None:
    """Run the daily earn cycle (missions + turns) on a schedule."""
    settings = Settings.from_env()
    store = Store(settings.data_dir)
    solver = build_solver(Transport(settings))
    config = LoopConfig(
        turns=turns,
        interval_seconds=int(interval_hours * 3600),
        claim_missions=not no_missions,
        max_cycles=1 if once else cycles,
        daily_cap=daily_cap,
        parallel_workers=workers,
        min_remaining_zaps=(
            min_remaining if min_remaining >= 0
            else float(os.environ.get("MIN_REMAINING_ZAPS", "1.0") or 0.0)
        ),
    )
    runner = DailyLoop(settings, store, config=config, solver=solver,
                       verifier=build_verifier(settings), logger=_mirror(_log))
    if email:
        runner.only_emails = {email}

    def _handle(signum, frame):  # noqa: ANN001, ARG001
        _log("loop: stop requested, finishing current account...")
        runner.stop()

    signal.signal(signal.SIGINT, _handle)
    signal.signal(signal.SIGTERM, _handle)

    ensure_solver_ready(logger=_log)

    start_monitor("loop", target=len(store.all()))
    try:
        if once:
            result = runner.run_cycle(force=force)
        else:
            runner.run_forever()
    finally:
        stop_monitor("DONE")
    if once:
        ui.summary("LOOP DONE", [
            ("zaps", round(result.zaps, 2)),
            ("accounts", len(result.per_account)),
        ], color="green")
        # Ratio against the practical daily budget (21 zaps/account).
        target = 21.0 * max(1, len(result.per_account))
        tg_notify("LOOP DONE", [
            ("zaps", round(result.zaps, 2)),
            ("accounts", len(result.per_account)),
            ("avg / account", round(result.zaps / max(1, len(result.per_account)), 2)),
        ], progress=("daily budget", min(1.0, result.zaps / target)),
           footer=f"host: {socket.gethostname()}", logger=_log)
    else:
        ui.log("loop stopped", level="warn")


def pipeline(
    register: int = typer.Option(0, "--register", help="Accounts to provision (0 = skip)."),
    earn: bool = typer.Option(True, "--earn/--no-earn", help="Earn for new accounts."),
    loop: bool = typer.Option(False, "--loop", help="Run the recurring daily loop after."),
    turns: int = typer.Option(10, help="Turns per account per earn pass."),
    interval_hours: float = typer.Option(24.0, help="Daily-loop interval (hours)."),
    cycles: int = typer.Option(0, help="Daily-loop cycles (0 = forever)."),
    referral: str = typer.Option("", help="Referral code (default from .env)."),
    workers: int = typer.Option(1, help="Farm parallelism (accounts earned concurrently)."),
    daily_cap: float = typer.Option(0.0, help="Stop an account at this daily zaps."),
    only_new: bool = typer.Option(False, "--only-new", help="Earn only the accounts registered this run."),
    solver_concurrent: int = typer.Option(0, help="Solver concurrency (0 = from .env)."),
) -> None:
    """Full pipeline: register -> earn -> daily loop (one entry point)."""
    from ...ops.orchestrator import Orchestrator, PipelineConfig

    settings = Settings.from_env()
    config = PipelineConfig(
        register_count=register, earn=earn, turns=turns, loop=loop,
        interval_hours=interval_hours, loop_cycles=cycles, referral=referral,
        workers=workers, daily_cap=daily_cap, only_new=only_new,
        solver_concurrent=solver_concurrent,
    )
    orch = Orchestrator(settings, config=config, logger=_mirror(_log))
    ensure_solver_ready(logger=_log)

    def _handle(signum, frame):  # noqa: ANN001, ARG001
        _log("pipeline: stop requested")
        orch.stop()

    signal.signal(signal.SIGINT, _handle)
    signal.signal(signal.SIGTERM, _handle)

    start_monitor("pipeline", target=register)
    summary = orch.run()
    stop_monitor("DONE" if not orch._stop.is_set() else "STOPPED")
    ui.summary("PIPELINE DONE", [
        ("registered", summary.get("registered", 0)),
        ("active", summary.get("active", 0)),
        ("earned zaps", round(summary.get("earned_zaps", 0.0), 2)),
    ], color="green")
    _reg = int(summary.get("registered", 0) or 0)
    tg_notify("PIPELINE DONE", [
        ("registered", _reg),
        ("active", summary.get("active", 0)),
        ("earned zaps", round(summary.get("earned_zaps", 0.0), 2)),
    ], progress=("success rate", (summary.get("active", 0) / _reg) if _reg else 0.0),
       footer=f"host: {socket.gethostname()}", logger=_log)


def report(
    fmt: str = typer.Option("table", help="table|json|csv"),
    refresh: bool = typer.Option(False, "--refresh", help="Poll server stats first."),
) -> None:
    """One-shot status report (headless; for cron/logs/piping)."""
    settings = Settings.from_env()
    store = Store(settings.data_dir)
    records = store.all()
    if not records:
        _log("report: no accounts")
        raise typer.Exit(code=1)

    rows: list[dict] = []
    if refresh:
        manager = SessionManager(settings, store)
        for record in records:
            client = ConsoClient(settings)
            try:
                client.session = manager.ensure_session(record).session
                row = get_account_row(client) or {}
                rows.append({
                    "status": "banned" if row.get("is_banned") else record.status,
                    "email": record.email,
                    "total_zaps": float(row.get("total_zaps") or 0),
                    "today": float(row.get("daily_zaps_earned") or 0),
                    "streak": int(row.get("current_streak") or 0),
                    "boost": float(row.get("boost_factor") or 1.0),
                    "proxy": (record.proxy.split("@")[-1] if record.proxy else ""),
                })
            except Exception:  # noqa: BLE001
                rows.append({"status": record.status, "email": record.email,
                             "total_zaps": float(record.total_zaps or 0), "today": 0.0,
                             "streak": 0, "boost": 1.0, "proxy": ""})
            finally:
                client.close()
    else:
        for record in records:
            rows.append({"status": record.status, "email": record.email,
                         "total_zaps": float(record.total_zaps or 0), "today": 0.0,
                         "streak": 0, "boost": 1.0, "proxy": record.proxy or ""})

    active = sum(1 for r in rows if r["status"] == "active")
    banned = sum(1 for r in rows if r["status"] == "banned")
    failed = sum(1 for r in rows if r["status"] == "failed")
    total = sum(r["total_zaps"] for r in rows)

    if fmt == "json":
        typer.echo(json.dumps({
            "summary": {"accounts": len(rows), "active": active, "failed": failed,
                        "banned": banned, "total_zaps": round(total, 2)},
            "accounts": rows,
        }, indent=2, ensure_ascii=False))
    elif fmt == "csv":
        import csv as _csv
        import io

        buf = io.StringIO()
        writer = _csv.DictWriter(buf, fieldnames=["status", "email", "total_zaps",
                                                  "today", "streak", "boost", "proxy"])
        writer.writeheader()
        writer.writerows(rows)
        typer.echo(buf.getvalue().strip())
    else:
        ui.accounts_table(sorted(rows, key=lambda x: x["total_zaps"], reverse=True))
        ui.summary("SUMMARY", [
            ("accounts", len(rows)), ("active", active),
            ("failed", failed), ("banned", banned),
            ("total zaps", f"{total:.1f}"),
        ], color="green" if not failed and not banned else "yellow")
