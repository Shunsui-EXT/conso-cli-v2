"""
Offline smoke tests for the Conso CLI surface.

Nothing here touches the network or the account store: `--help` renders the
command list, `farm --dry-run` builds payloads from the local economy model,
and `doctor` only inspects installed packages / env.
"""

from __future__ import annotations

import sys
from pathlib import Path

from typer.testing import CliRunner

# Match the sibling tests style: inject the project's ``src`` on sys.path so
# ``conso.app`` imports without a package install.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from conso.app.cli import app  # noqa: E402

runner = CliRunner()

#: Every subcommand the CLI must expose (parity with the reference cli.py).
EXPECTED_COMMANDS = {
    "test", "register", "farm", "export", "verify", "solve", "session",
    "earn", "status", "loop", "recover", "doctor", "proxies", "pipeline",
    "notify", "report", "backup",
}


def _command_names() -> set[str]:
    """Registered command names, robust to Typer leaving ``name`` unset."""
    from typer.main import get_command, get_command_name

    group = get_command(app)
    names = set(group.commands)
    for cmd in app.registered_commands:
        if cmd.name:
            names.add(cmd.name)
        elif cmd.callback is not None:
            names.add(get_command_name(cmd.callback.__name__))
    return names


def test_help_lists_all_commands():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0, result.output
    missing = {c for c in EXPECTED_COMMANDS if c not in _command_names()}
    assert not missing, f"commands not registered: {sorted(missing)}"
    assert len(EXPECTED_COMMANDS) == 17


def test_no_subcommand_prints_help():
    result = runner.invoke(app, [])
    assert result.exit_code == 0, result.output
    assert "Usage" in result.output


def test_farm_dry_run_emits_payloads():
    result = runner.invoke(app, ["farm", "--dry-run", "--turns", "1"])
    assert result.exit_code == 0, result.output
    # NOTE: the "[dry-run] " prefix is a Rich markup label and is swallowed when
    # stdout is not a TTY (CliRunner), exactly as in the reference CLI — so the
    # assertion targets the payload the command is required to print.
    assert "zaps=" in result.output
    assert "entry=" in result.output
    assert '"inputTokens"' in result.output


def test_doctor_reports_readiness():
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0, result.output
    assert "doctor" in result.output
    # The chain report names at least one provider and states browser-freeness.
    assert "solver chain" in result.output
    assert "browser-free" in result.output


def test_export_uses_dataclass_fields(tmp_path, monkeypatch):
    """`export --fmt json` serialises AccountRecord via dataclasses.asdict."""
    from conso.store.storage import AccountRecord, Store

    store = Store(str(tmp_path))
    store.add(AccountRecord.now(email="a@example.invalid", status="active", total_zaps=3.5))

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    result = runner.invoke(app, ["export", "--fmt", "json"])
    assert result.exit_code == 0, result.output
    assert "a@example.invalid" in result.output
    assert '"total_zaps": 3.5' in result.output
