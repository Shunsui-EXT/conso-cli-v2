"""
Conso automation CLI.

Builds the Typer application and registers every subcommand from
``conso.app.commands``. Subcommands:

  test       Pre-flight: transport, backend reachability, economy sanity.
  register   Provision N accounts (adaptive concurrency, resumable).
  farm       Submit synthetic AI-usage turns for stored accounts to earn zaps.
  export     Dump the account store to JSON/CSV.
  verify     Health-check the proxy pool.
  solve      Solve one Turnstile challenge (solver sanity check).
  session    Show/refresh stored sessions without re-authenticating.
  earn       Claim daily/bonus missions and codes.
  status     Show daily-limit / earning state.
  loop       Run the daily earn cycle on a schedule.
  recover    Recover a session (refresh -> password -> OTP).
  doctor     Report dependency / provider readiness.
  proxies    Health-check a proxy list.
  pipeline   Full pipeline: register -> earn -> daily loop.
  notify     Send a test Telegram message.
  report     One-shot status report (headless).
"""

from __future__ import annotations

import sys

import typer

from ..net.client import ConsoAPIError
from . import ui
from .commands import register_all

app = typer.Typer(add_completion=False, help="Conso AI Usage Tracker automation CLI.")

register_all(app)


@app.callback(invoke_without_command=True)
def _default(ctx: typer.Context) -> None:
    """No subcommand -> dashboard on a TTY, plain help everywhere else.

    The TTY check is what keeps pipes, cron and CI from hanging on a menu:
    with no interactive terminal the dashboard could never read a choice, so
    those callers keep the historical banner+help behaviour.
    """
    if ctx.invoked_subcommand is None:
        if sys.stdin.isatty() and sys.stdout.isatty():
            from .dashboard import run_dashboard

            raise typer.Exit(run_dashboard())
        ui.banner("help")
        typer.echo(ctx.get_help())
        raise typer.Exit(0)


def main() -> None:
    try:
        app()
    except ConsoAPIError as exc:
        ui.error(str(exc))
        sys.exit(1)


if __name__ == "__main__":
    main()
