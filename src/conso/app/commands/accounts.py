"""
Account subcommands: register, session, recover, export, status.

Ported from the reference ``conso/cli.py``; only the imports changed (the flat
module layout became ``conso.app`` + ``conso.ops`` + ``conso.auth`` ...).
"""

from __future__ import annotations

import json
import random
import time

import typer

from .. import ui
from ...auth.session import SessionManager
from ...core.config import Settings
from ...core.identity import build_identity
from ...net.client import ConsoAPIError, ConsoClient
from ...net.transport import Transport
from ...ops.limits import get_daily_status
from ...ops.notify import notify as tg_notify
from ...ops.pipeline import run_registration
from ...ops.scheduler import DailyLoop, LoopConfig
from ...ops.verifiers import build_verifier
from ...solver import build_solver, ensure_solver_ready
from ...store.storage import Store
from ..monitor import start_monitor, stop_monitor

_log = ui.log_auto
_mirror = ui.mirror


def register(app: typer.Typer) -> None:
    """Attach this module's commands to ``app``."""
    # `name=` is explicit here: the function is called `register_cmd` to avoid
    # shadowing this module's own `register(app)` hook, and Typer would
    # otherwise derive the command name `register-cmd` from it.
    app.command(name="register")(register_cmd)
    app.command(name="session")(session)
    app.command()(recover)
    app.command()(export)
    app.command()(status)


def register_cmd(
    count: int = typer.Argument(..., help="Number of accounts to create."),
    referral: str = typer.Option("", help="Referral code to redeem per account."),
    earn: bool = typer.Option(False, "--earn", help="Immediately earn zaps after each account registers."),
    turns: int = typer.Option(10, help="Turns per account when --earn is set."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Generate identities only, no network."),
) -> None:
    """Provision accounts (registration only; use --earn to farm right after)."""
    settings = Settings.from_env()
    if dry_run:
        rng = random.Random()
        verifier = build_verifier(settings)
        provision_inbox = callable(getattr(verifier, "create_inbox", None))
        for i in range(count):
            try:
                identity = build_identity(settings, rng=rng, index=i)
                _log(f"[dry-run] {identity.email} :: {identity.consoname} :: {identity.password}")
            except ValueError:
                if provision_inbox:
                    _log(f"[dry-run] verifier={type(verifier).__name__} will provision inbox at runtime")
                    break
                _log("dry-run: set EMAIL_DOMAIN or use VERIFIER=temptf/mailtm (self-provisioning)")
                break
        return

    store = Store(settings.data_dir)
    verifier = build_verifier(settings)
    solver = build_solver(Transport(settings))
    referral_code = referral or settings.default_referral_code
    ui.banner("register", solver=type(solver).__name__, verifier=type(verifier).__name__,
              proxy=f"{len(settings.proxy.urls)} proxies" if settings.proxy.urls else "direct",
              extra={"count": str(count), "referral": referral_code or "-"})
    ensure_solver_ready(logger=_log)
    start_monitor("register", target=count)
    try:
        results = run_registration(
            settings, count, referral_code=referral_code, store=store,
            verifier=verifier, solver=solver, logger=_mirror(_log),
        )
    finally:
        stop_monitor("DONE")
    ok = sum(1 for r in results if r.status == "active")
    failed = len(results) - ok
    ui.summary("REGISTER DONE", [
        ("active", f"{ok}/{len(results)}"),
        ("failed", failed),
        ("store", store.json_path),
    ], color="green" if ok else "red")
    tg_notify("REGISTER DONE", [
        ("active", f"{ok}/{len(results)}"),
        ("failed", failed),
    ], logger=_log)

    if earn and ok:
        _log(f"register: --earn set, farming {ok} new account(s)")
        start_monitor("register+earn", target=ok)
        try:
            runner = DailyLoop(settings, store, config=LoopConfig(turns=turns),
                               solver=solver, verifier=verifier, logger=_mirror(_log))
            for r in results:
                if r.status != "active":
                    continue
                record = next((a for a in store.all() if a.email == r.email), None)
                if record is None:
                    continue
                info = runner.run_account(record)
                _log(f"register: {r.email} earned +{info.get('zaps', 0)} zaps "
                     f"(status={info.get('status')})")
        finally:
            stop_monitor("DONE")


def session(
    email: str = typer.Option("", help="Account email (empty = all accounts)."),
    force: bool = typer.Option(False, "--force", help="Force a refresh now."),
) -> None:
    """Show/refresh stored sessions without re-authenticating."""
    settings = Settings.from_env()
    store = Store(settings.data_dir)
    manager = SessionManager(settings, store)
    solver = build_solver(Transport(settings))
    records = store.all()
    if email:
        records = [r for r in records if r.email == email]
    if not records:
        _log("session: no accounts")
        return
    for record in records:
        try:
            result = manager.ensure_session(record, solver=solver, force_refresh=force)
            fresh = next((r for r in store.all() if r.email == record.email), record)
            _log(
                f"{record.email} :: source={result.source} "
                f"expires_in={int((fresh.expires_at or 0) - time.time())}s"
            )
        except ConsoAPIError as exc:
            _log(f"{record.email} :: FAILED {exc}")


def recover(
    email: str = typer.Option("", help="Account email (empty = all accounts)."),
) -> None:
    """Recover a session (refresh -> password -> email OTP) without re-registering."""
    settings = Settings.from_env()
    store = Store(settings.data_dir)
    manager = SessionManager(settings, store)
    solver = build_solver(Transport(settings))
    verifier = build_verifier(settings)
    records = store.all()
    if email:
        records = [r for r in records if r.email == email]
    if not records:
        _log("recover: no accounts")
        raise typer.Exit(code=1)
    for record in records:
        try:
            result = manager.recover(record, solver=solver, verifier=verifier)
            _log(f"recover: {record.email} :: OK source={result.source}")
        except ConsoAPIError as exc:
            _log(f"recover: {record.email} :: FAILED {exc}")


def export(
    fmt: str = typer.Option("csv", help="csv|json"),
) -> None:
    """Dump the account store."""
    settings = Settings.from_env()
    store = Store(settings.data_dir)
    records = store.all()
    if fmt == "json":
        from dataclasses import asdict

        typer.echo(json.dumps([asdict(r) for r in records], indent=2, ensure_ascii=False))
    else:
        typer.echo(store.csv_path)
    _log(f"export: {len(records)} accounts -> {store.json_path if fmt == 'json' else store.csv_path}")


def status(
    email: str = typer.Option("", help="Account email (empty = all accounts)."),
) -> None:
    """Show daily-limit / earning state for stored accounts."""
    settings = Settings.from_env()
    store = Store(settings.data_dir)
    manager = SessionManager(settings, store)
    solver = build_solver(Transport(settings))
    records = store.all()
    if email:
        records = [r for r in records if r.email == email]
    if not records:
        _log("status: no accounts")
        return
    for record in records:
        client = ConsoClient(settings)
        try:
            client.session = manager.ensure_session(record, solver=solver).session
            st = get_daily_status(client)
            if st is None:
                _log(f"status: {record.email} :: no row")
                continue
            stale = " (stale, resets today)" if st.is_stale else ""
            _log(
                f"status: {record.email} :: total={round(st.total_zaps, 2)} "
                f"daily={round(st.effective_daily, 2)}{stale} "
                f"streak={st.current_streak}/{st.longest_streak} "
                f"boost={st.boost_factor} banned={st.is_banned}"
            )
        except ConsoAPIError as exc:
            _log(f"status: {record.email} :: FAILED {exc}")
        finally:
            client.close()
