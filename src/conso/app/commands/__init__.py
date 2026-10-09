"""
CLI subcommand modules.

Each module owns one theme (accounts / earning / maintenance) and exposes a
``register(app)`` that attaches its commands with ``app.command()(fn)``. Typer
derives the command name from the function name, so the functions are named
exactly as the commands they implement.

Nothing in here may import ``conso.app.cli`` — the CLI imports this package,
and the reverse edge would be an import cycle.
"""

from __future__ import annotations

import typer

from . import accounts, earning, maintenance


def register_all(app: typer.Typer) -> None:
    """Attach every subcommand to ``app`` (order = order in --help)."""
    accounts.register(app)
    earning.register(app)
    maintenance.register(app)


__all__ = ["register_all", "accounts", "earning", "maintenance"]
