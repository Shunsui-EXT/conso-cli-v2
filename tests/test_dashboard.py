"""
Offline tests for the interactive dashboard.

Everything here is deterministic and network-free: user input goes through the
dashboard's ``_choose``/``_ask``/``_confirm``/``_int_ask`` seams (monkeypatched
per test), command callables are replaced with spies, and the store lives in
``tmp_path`` via ``DATA_DIR``. The point is the loop's contract — it never
dies, it always repaints, and Ctrl+C is only fatal at the menu itself.
"""

from __future__ import annotations

import sys
from pathlib import Path

import typer

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from conso.app import dashboard  # noqa: E402
from conso.app import ui  # noqa: E402
from conso.net.client import ConsoAPIError  # noqa: E402
from conso.store.storage import AccountRecord, Store  # noqa: E402


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------
class _Scripted:
    """Feeds scripted answers to the dashboard's input seams.

    ``menus`` is the queue of answers for ``_choose``; ``prompts`` the queue
    for every ``_ask``/``_confirm``/``_int_ask`` call inside an action. When a
    queue runs dry the last value repeats (handy for "quit after N renders").
    """

    def __init__(self, menus: list[str], prompts: list[object] | None = None) -> None:
        self.menus = list(menus)
        self.prompts = list(prompts or [])

    def choose(self, prompt: str = "select") -> str:
        return self.menus.pop(0) if len(self.menus) > 1 else self.menus[0]

    def _next(self) -> object:
        return self.prompts.pop(0) if self.prompts else ""

    def ask(self, prompt: str, default: str = "") -> str:
        value = self._next()
        return default if value == "" else str(value)

    def confirm(self, prompt: str, default: bool = False) -> bool:
        value = self._next()
        return default if value == "" else bool(value)

    def int_ask(self, prompt: str, default: int) -> int:
        value = self._next()
        return default if value == "" else int(value)


def _patch_io(monkeypatch, script: _Scripted) -> None:
    monkeypatch.setattr(dashboard, "_choose", script.choose)
    monkeypatch.setattr(dashboard, "_ask", script.ask)
    monkeypatch.setattr(dashboard, "_confirm", script.confirm)
    monkeypatch.setattr(dashboard, "_int_ask", script.int_ask)


def _use_tmp_store(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("DATA_DIR", str(tmp_path))


def _seed(tmp_path: Path, *emails: str) -> None:
    store = Store(str(tmp_path))
    for email in emails:
        store.add(AccountRecord.now(email=email, status="active", total_zaps=1.0))


# ---------------------------------------------------------------------------
# Loop contract
# ---------------------------------------------------------------------------
def test_immediate_quit_exits_zero(tmp_path, monkeypatch):
    _use_tmp_store(tmp_path, monkeypatch)
    _patch_io(monkeypatch, _Scripted(["q"]))
    assert dashboard.run_dashboard() == 0


def test_quit_accepts_letter_and_word(tmp_path, monkeypatch):
    _use_tmp_store(tmp_path, monkeypatch)
    _patch_io(monkeypatch, _Scripted(["Q", "quit"]))
    assert dashboard.run_dashboard() == 0


def test_unknown_choice_rerenders_and_continues(tmp_path, monkeypatch):
    _use_tmp_store(tmp_path, monkeypatch)
    _patch_io(monkeypatch, _Scripted(["zz", "q"]))
    renders: list[object] = []
    monkeypatch.setattr(dashboard, "_render", lambda state: renders.append(state))
    assert dashboard.run_dashboard() == 0
    # one render for the bad choice, one after it was rejected
    assert len(renders) == 2


def test_render_shows_store_state_and_recent_line(tmp_path, monkeypatch):
    """The panel must be rebuilt from disk after each action (fresh counts)."""
    _use_tmp_store(tmp_path, monkeypatch)
    _seed(tmp_path, "a@example.invalid", "b@example.invalid")
    state = dashboard._DashboardState(settings=dashboard.Settings.from_env(),
                                      store=Store(str(tmp_path)))
    dashboard._set_result(state, "farm", "done")
    panel = dashboard._panel(state)
    assert panel.title.plain == "CONSO MONITOR"
    text = dashboard.render_plain(state)
    assert "a@example.invalid" not in text  # counts only, no email dump
    assert "2" in text and "ready" in text
    assert "farm: done" in text              # session log line
    assert "daily run" in text and "quit" in text


def test_keyboard_interrupt_at_menu_exits_zero(tmp_path, monkeypatch):
    _use_tmp_store(tmp_path, monkeypatch)
    monkeypatch.setattr(dashboard, "_choose", lambda prompt="select": (_ for _ in ()).throw(KeyboardInterrupt))
    assert dashboard.run_dashboard() == 0


def test_eof_at_menu_exits_zero(tmp_path, monkeypatch):
    """A closed stdin (the dashboard ran without a TTY) is a clean exit."""
    _use_tmp_store(tmp_path, monkeypatch)
    monkeypatch.setattr(dashboard, "_choose", lambda prompt="select": (_ for _ in ()).throw(EOFError))
    assert dashboard.run_dashboard() == 0


def test_render_failure_does_not_end_the_session(tmp_path, monkeypatch):
    """A store hiccup while repainting must not kill an in-flight session."""
    _use_tmp_store(tmp_path, monkeypatch)
    _patch_io(monkeypatch, _Scripted(["9", "q"]))
    calls: list[bool] = []

    def flaky(state):
        calls.append(True)
        raise OSError("store vanished")

    monkeypatch.setattr(dashboard, "_render", flaky)
    monkeypatch.setattr(dashboard, "doctor_cmd", lambda: None)
    assert dashboard.run_dashboard() == 0
    # rendered (and failed) twice: before action 9 and before q
    assert len(calls) == 2


# ---------------------------------------------------------------------------
# Action guard
# ---------------------------------------------------------------------------
def test_action_conso_error_returns_to_menu(tmp_path, monkeypatch):
    _use_tmp_store(tmp_path, monkeypatch)
    # prompts: turns, email, dry-run=n, missions=y, confirm=y
    _patch_io(monkeypatch, _Scripted(["2", "q"], prompts=[1, "all", False, True, True]))
    calls: list[dict] = []

    def boom(**kwargs):
        calls.append(kwargs)
        raise ConsoAPIError("backend down")

    monkeypatch.setattr(dashboard, "farm_cmd", boom)
    assert dashboard.run_dashboard() == 0
    assert calls == [{"turns": 1, "email": "", "earn": True, "dry_run": False}]


def test_action_typer_exit_returns_to_menu(tmp_path, monkeypatch):
    _use_tmp_store(tmp_path, monkeypatch)
    _patch_io(monkeypatch, _Scripted(["13", "q"], prompts=["json"]))
    monkeypatch.setattr(dashboard, "export_cmd",
                        lambda **k: (_ for _ in ()).throw(typer.Exit(code=1)))
    assert dashboard.run_dashboard() == 0


def test_action_generic_exception_returns_to_menu(tmp_path, monkeypatch):
    _use_tmp_store(tmp_path, monkeypatch)
    _patch_io(monkeypatch, _Scripted(["9", "q"]))
    monkeypatch.setattr(dashboard, "doctor_cmd",
                        lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    assert dashboard.run_dashboard() == 0


def test_keyboard_interrupt_inside_action_returns_to_menu(tmp_path, monkeypatch):
    _use_tmp_store(tmp_path, monkeypatch)
    _patch_io(monkeypatch, _Scripted(["9", "q"]))
    monkeypatch.setattr(dashboard, "doctor_cmd",
                        lambda: (_ for _ in ()).throw(KeyboardInterrupt))
    assert dashboard.run_dashboard() == 0


def test_stop_monitor_called_around_every_action(tmp_path, monkeypatch):
    _use_tmp_store(tmp_path, monkeypatch)
    _patch_io(monkeypatch, _Scripted(["9", "q"]))
    stops: list[str] = []
    monkeypatch.setattr(dashboard, "stop_monitor", stops.append)
    monkeypatch.setattr(dashboard, "doctor_cmd", lambda: None)
    assert dashboard.run_dashboard() == 0
    assert stops == ["DONE"]


# ---------------------------------------------------------------------------
# Prompt mapping (only the values each action needs, Enter = default)
# ---------------------------------------------------------------------------
def test_farm_defaults_to_ten_turns_all_accounts(tmp_path, monkeypatch):
    _use_tmp_store(tmp_path, monkeypatch)
    # Enter for every prompt: turns=10, email=all, dry-run=n, missions=y, go
    _patch_io(monkeypatch, _Scripted(["2", "q"], prompts=["", "", "", "", True]))
    calls: list[dict] = []
    monkeypatch.setattr(dashboard, "farm_cmd", lambda **k: calls.append(k))
    assert dashboard.run_dashboard() == 0
    assert calls == [{"turns": 10, "email": "", "earn": True, "dry_run": False}]


def test_loop_once_maps_defaults_and_env_min_remaining(tmp_path, monkeypatch):
    _use_tmp_store(tmp_path, monkeypatch)
    # Enter for every prompt: force=n, workers=1, turns=10, min-remaining=-1
    _patch_io(monkeypatch, _Scripted(["1", "q"], prompts=["", "", "", "", True]))
    calls: list[dict] = []
    monkeypatch.setattr(dashboard, "loop_cmd", lambda **k: calls.append(k))
    assert dashboard.run_dashboard() == 0
    assert calls == [{"turns": 10, "once": True, "force": False,
                      "workers": 1, "min_remaining": -1.0}]


def test_register_asks_count_referral_earn_turns(tmp_path, monkeypatch):
    _use_tmp_store(tmp_path, monkeypatch)
    monkeypatch.setenv("DEFAULT_REFERRAL_CODE", "REF123")
    # prompts: count, referral, earn=y, turns, dry-run=n, confirm=y
    _patch_io(monkeypatch, _Scripted(["8", "q"], prompts=[2, "REF123", True, 7, False, True]))
    calls: list[dict] = []
    monkeypatch.setattr(dashboard, "register_cmd", lambda **k: calls.append(k))
    assert dashboard.run_dashboard() == 0
    assert calls == [{"count": 2, "referral": "REF123", "earn": True, "turns": 7,
                      "dry_run": False}]


def test_register_without_earn_skips_turns_prompt(tmp_path, monkeypatch):
    _use_tmp_store(tmp_path, monkeypatch)
    monkeypatch.setenv("DEFAULT_REFERRAL_CODE", "")
    # prompts: count, referral, earn=n -> no turns question, dry-run=n, confirm=y
    _patch_io(monkeypatch, _Scripted(["8", "q"], prompts=[1, "", False, False, True]))
    calls: list[dict] = []
    monkeypatch.setattr(dashboard, "register_cmd", lambda **k: calls.append(k))
    assert dashboard.run_dashboard() == 0
    assert calls == [{"count": 1, "referral": "", "earn": False, "turns": 10,
                      "dry_run": False}]


def test_earn_one_accepts_number_and_email(tmp_path, monkeypatch):
    _use_tmp_store(tmp_path, monkeypatch)
    monkeypatch.setenv("DEFAULT_REFERRAL_CODE", "")
    _seed(tmp_path, "a@example.invalid", "b@example.invalid")
    _patch_io(monkeypatch, _Scripted(["4", "4", "q"], prompts=["2", "", True, "b@example.invalid", "", True]))
    calls: list[dict] = []
    monkeypatch.setattr(dashboard, "earn_cmd", lambda **k: calls.append(k))
    assert dashboard.run_dashboard() == 0
    assert calls == [
        {"email": "b@example.invalid", "referral": ""},
        {"email": "b@example.invalid", "referral": ""},
    ]


def test_earn_one_unknown_pick_does_not_call_command(tmp_path, monkeypatch):
    _use_tmp_store(tmp_path, monkeypatch)
    _seed(tmp_path, "a@example.invalid")
    _patch_io(monkeypatch, _Scripted(["4", "q"], prompts=["99"]))
    calls: list[dict] = []
    monkeypatch.setattr(dashboard, "earn_cmd", lambda **k: calls.append(k))
    assert dashboard.run_dashboard() == 0
    assert calls == []


def test_report_defaults_to_table_without_refresh(tmp_path, monkeypatch):
    _use_tmp_store(tmp_path, monkeypatch)
    _patch_io(monkeypatch, _Scripted(["14", "q"], prompts=["", False]))
    calls: list[dict] = []
    monkeypatch.setattr(dashboard, "report_cmd", lambda **k: calls.append(k))
    assert dashboard.run_dashboard() == 0
    assert calls == [{"fmt": "table", "refresh": False}]


def test_export_rejects_unknown_format_without_calling(tmp_path, monkeypatch):
    _use_tmp_store(tmp_path, monkeypatch)
    _patch_io(monkeypatch, _Scripted(["13", "q"], prompts=["xml"]))
    calls: list[dict] = []
    monkeypatch.setattr(dashboard, "export_cmd", lambda **k: calls.append(k))
    assert dashboard.run_dashboard() == 0
    assert calls == []


def test_verify_maps_to_verify_command(tmp_path, monkeypatch):
    _use_tmp_store(tmp_path, monkeypatch)
    _patch_io(monkeypatch, _Scripted(["15", "q"]))
    calls: list[bool] = []
    monkeypatch.setattr(dashboard, "verify_cmd", lambda: calls.append(True))
    assert dashboard.run_dashboard() == 0
    assert calls == [True]


def test_notify_confirm_then_calls(tmp_path, monkeypatch):
    _use_tmp_store(tmp_path, monkeypatch)
    _patch_io(monkeypatch, _Scripted(["16", "q"], prompts=[True]))
    calls: list[bool] = []
    monkeypatch.setattr(dashboard, "notify_cmd", lambda: calls.append(True))
    assert dashboard.run_dashboard() == 0
    assert calls == [True]


def test_notify_cancel_does_not_call(tmp_path, monkeypatch):
    _use_tmp_store(tmp_path, monkeypatch)
    _patch_io(monkeypatch, _Scripted(["16", "q"], prompts=[False]))
    calls: list[bool] = []
    monkeypatch.setattr(dashboard, "notify_cmd", lambda: calls.append(True))
    assert dashboard.run_dashboard() == 0
    assert calls == []


def test_pipeline_maps_every_flag(tmp_path, monkeypatch):
    _use_tmp_store(tmp_path, monkeypatch)
    monkeypatch.setenv("DEFAULT_REFERRAL_CODE", "REF9")
    # prompts: register, earn=y, loop=y, turns, workers, interval, cycles,
    #          referral, daily-cap, only-new=y, solver-concurrent, confirm=y
    _patch_io(monkeypatch, _Scripted(
        ["17", "q"],
        prompts=[3, True, True, 5, 2, 12.0, 1, "REF9", 21.0, True, 4, True],
    ))
    calls: list[dict] = []
    monkeypatch.setattr(dashboard, "pipeline_cmd", lambda **k: calls.append(k))
    assert dashboard.run_dashboard() == 0
    assert calls == [{
        "register": 3, "earn": True, "loop": True, "turns": 5,
        "interval_hours": 12.0, "cycles": 1, "referral": "REF9",
        "workers": 2, "daily_cap": 21.0, "only_new": True,
        "solver_concurrent": 4,
    }]


def test_pipeline_defaults(tmp_path, monkeypatch):
    """Enter for everything: register=0, no earn/loop, defaults elsewhere."""
    _use_tmp_store(tmp_path, monkeypatch)
    monkeypatch.setenv("DEFAULT_REFERRAL_CODE", "")
    _patch_io(monkeypatch, _Scripted(
        ["17", "q"],
        prompts=["", "", "", "", "", "", "", "", "", "", "", True],
    ))
    calls: list[dict] = []
    monkeypatch.setattr(dashboard, "pipeline_cmd", lambda **k: calls.append(k))
    assert dashboard.run_dashboard() == 0
    assert calls == [{
        "register": 0, "earn": False, "loop": False, "turns": 10,
        "interval_hours": 24.0, "cycles": 0, "referral": "",
        "workers": 1, "daily_cap": 0.0, "only_new": False,
        "solver_concurrent": 0,
    }]


def test_pipeline_cancel_does_not_call(tmp_path, monkeypatch):
    _use_tmp_store(tmp_path, monkeypatch)
    # 11 answers, then "n" at the confirmation
    _patch_io(monkeypatch, _Scripted(
        ["17", "q"],
        prompts=["", "", "", "", "", "", "", "", "", "", "", False],
    ))
    calls: list[dict] = []
    monkeypatch.setattr(dashboard, "pipeline_cmd", lambda **k: calls.append(k))
    assert dashboard.run_dashboard() == 0
    assert calls == []


def test_farm_dry_run_and_no_missions_flags(tmp_path, monkeypatch):
    _use_tmp_store(tmp_path, monkeypatch)
    # prompts: turns, email, dry-run=y, missions=n, confirm=y
    _patch_io(monkeypatch, _Scripted(["2", "q"], prompts=[3, "a@x.invalid", True, False, True]))
    calls: list[dict] = []
    monkeypatch.setattr(dashboard, "farm_cmd", lambda **k: calls.append(k))
    assert dashboard.run_dashboard() == 0
    assert calls == [{"turns": 3, "email": "a@x.invalid", "earn": False, "dry_run": True}]


def test_missions_forwards_ids_and_access_code(tmp_path, monkeypatch):
    _use_tmp_store(tmp_path, monkeypatch)
    monkeypatch.setenv("DEFAULT_REFERRAL_CODE", "")
    # prompts: email, referral, missions, access, confirm
    _patch_io(monkeypatch, _Scripted(["3", "q"],
                                     prompts=["all", "", "daily_1,bonus_2", "ACCESS-X", True]))
    calls: list[dict] = []
    monkeypatch.setattr(dashboard, "earn_cmd", lambda **k: calls.append(k))
    assert dashboard.run_dashboard() == 0
    assert calls == [{"email": "", "referral": "", "access": "ACCESS-X",
                      "missions": "daily_1,bonus_2"}]


def test_register_dry_run_flag(tmp_path, monkeypatch):
    _use_tmp_store(tmp_path, monkeypatch)
    monkeypatch.setenv("DEFAULT_REFERRAL_CODE", "")
    # prompts: count, referral, earn=n, dry-run=y, confirm=y
    _patch_io(monkeypatch, _Scripted(["8", "q"], prompts=[2, "", False, True, True]))
    calls: list[dict] = []
    monkeypatch.setattr(dashboard, "register_cmd", lambda **k: calls.append(k))
    assert dashboard.run_dashboard() == 0
    assert calls == [{"count": 2, "referral": "", "earn": False, "turns": 10,
                      "dry_run": True}]


def test_test_forwards_platform_and_model(tmp_path, monkeypatch):
    _use_tmp_store(tmp_path, monkeypatch)
    # prompts: platform, model
    _patch_io(monkeypatch, _Scripted(["10", "q"], prompts=["openai", "gpt-5"]))
    calls: list[dict] = []
    monkeypatch.setattr(dashboard, "test_cmd", lambda **k: calls.append(k))
    assert dashboard.run_dashboard() == 0
    assert calls == [{"platform": "openai", "model": "gpt-5"}]


def test_test_defaults_platform_and_model(tmp_path, monkeypatch):
    _use_tmp_store(tmp_path, monkeypatch)
    _patch_io(monkeypatch, _Scripted(["10", "q"], prompts=["", ""]))
    calls: list[dict] = []
    monkeypatch.setattr(dashboard, "test_cmd", lambda **k: calls.append(k))
    assert dashboard.run_dashboard() == 0
    assert calls == [{"platform": "claude", "model": "claude-opus-4-8"}]


def test_solve_defaults_to_turnstile_constants(tmp_path, monkeypatch):
    _use_tmp_store(tmp_path, monkeypatch)
    _patch_io(monkeypatch, _Scripted(["11", "q"], prompts=["", ""]))
    calls: list[dict] = []
    monkeypatch.setattr(dashboard, "solve_cmd", lambda **k: calls.append(k))
    assert dashboard.run_dashboard() == 0
    assert calls == [{"sitekey": dashboard.TURNSTILE_SITEKEY,
                      "url": dashboard.TURNSTILE_PAGE_URL}]


def test_solve_accepts_overrides(tmp_path, monkeypatch):
    _use_tmp_store(tmp_path, monkeypatch)
    _patch_io(monkeypatch, _Scripted(["11", "q"], prompts=["0xSITEKEY", "https://x.invalid/p"]))
    calls: list[dict] = []
    monkeypatch.setattr(dashboard, "solve_cmd", lambda **k: calls.append(k))
    assert dashboard.run_dashboard() == 0
    assert calls == [{"sitekey": "0xSITEKEY", "url": "https://x.invalid/p"}]


def test_menu_lists_all_eighteen_entries_plus_shortcuts(tmp_path, monkeypatch):
    """The rendered panel must offer every command (1-18) plus S and Q."""
    _use_tmp_store(tmp_path, monkeypatch)
    state = dashboard._DashboardState(settings=dashboard.Settings.from_env(),
                                      store=Store(str(tmp_path)))
    text = dashboard.render_plain(state)
    # Every numeric choice is dispatched...
    for key, _label, _handler in dashboard._MENU:
        assert key in dashboard._MENU_BY_CHOICE
    assert {k for k, _, _ in dashboard._MENU} == {str(n) for n in range(1, 19)} | {"s"}
    # ...and every numeric choice is visible on the panel as a "N " prefix.
    for n in range(1, 19):
        assert f"{n} " in text, f"menu entry {n} missing from the panel"
    assert "summary/settings" in text
    assert "quit" in text
    assert "pipeline" in text and "verify" in text and "notify" in text
    assert "backup" in text


def test_keyboard_interrupt_during_render_exits_zero(tmp_path, monkeypatch):
    """Ctrl+C landing exactly on a repaint is quit, not a 130 crash."""
    _use_tmp_store(tmp_path, monkeypatch)
    _patch_io(monkeypatch, _Scripted(["9", "q"]))
    monkeypatch.setattr(dashboard, "doctor_cmd", lambda: None)
    calls: list[int] = []

    def flaky(state):
        calls.append(1)
        if len(calls) >= 2:
            raise KeyboardInterrupt

    monkeypatch.setattr(dashboard, "_render", flaky)
    assert dashboard.run_dashboard() == 0
    assert len(calls) == 2


def test_system_exit_inside_action_returns_to_menu(tmp_path, monkeypatch):
    """A command calling sys.exit() is a dead end, not a dashboard kill."""
    _use_tmp_store(tmp_path, monkeypatch)
    _patch_io(monkeypatch, _Scripted(["9", "q"]))
    calls: list[bool] = []

    def bail():
        calls.append(True)
        raise SystemExit(3)

    monkeypatch.setattr(dashboard, "doctor_cmd", bail)
    assert dashboard.run_dashboard() == 0
    assert calls == [True]


def test_sessions_forwards_force_flag(tmp_path, monkeypatch):
    _use_tmp_store(tmp_path, monkeypatch)
    _patch_io(monkeypatch, _Scripted(["6", "q"], prompts=["all", True]))
    calls: list[dict] = []
    monkeypatch.setattr(dashboard, "session_cmd", lambda **k: calls.append(k))
    assert dashboard.run_dashboard() == 0
    assert calls == [{"email": "", "force": True}]


def test_destructive_actions_cancel_on_no(tmp_path, monkeypatch):
    """Answering "n" to the confirmation must not reach the command."""
    _use_tmp_store(tmp_path, monkeypatch)
    # prompts: turns, email, dry-run=n, missions=y, confirm=n
    _patch_io(monkeypatch, _Scripted(["2", "q"], prompts=[5, "all", False, True, False]))
    calls: list[dict] = []
    monkeypatch.setattr(dashboard, "farm_cmd", lambda **k: calls.append(k))
    assert dashboard.run_dashboard() == 0
    assert calls == []


def test_summary_renders_settings_panel(tmp_path, monkeypatch):
    from rich.panel import Panel

    _use_tmp_store(tmp_path, monkeypatch)
    _patch_io(monkeypatch, _Scripted(["s", "q"]))
    printed: list[object] = []
    monkeypatch.setattr(ui.console, "print", lambda *a, **k: printed.append(a[0] if a else None))
    assert dashboard.run_dashboard() == 0
    panels = [p for p in printed if isinstance(p, Panel)]
    settings_panel = next(p for p in panels if p.title.plain == "SETTINGS")
    text = settings_panel.renderable.plain
    assert "verifier" in text
    assert "solver" in text


# ---------------------------------------------------------------------------
# CLI wiring
# ---------------------------------------------------------------------------
def test_cli_no_args_launches_dashboard_on_tty(monkeypatch):
    """`python main.py` with a TTY goes straight into the dashboard.

    The callback is invoked directly (not through CliRunner): CliRunner
    replaces ``sys.stdin``/``sys.stdout`` with its own capture streams, which
    would hide the very ``isatty`` the TTY gate reads.
    """
    from conso.app import cli

    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    launched: list[bool] = []
    monkeypatch.setattr(dashboard, "run_dashboard", lambda: launched.append(True) or 0)

    ctx = typer.Context(typer.main.get_command(cli.app))
    try:
        cli._default(ctx)
    except typer.Exit as exc:
        assert exc.exit_code == 0
    else:  # pragma: no cover - the callback is documented to raise typer.Exit
        raise AssertionError("_default must exit through typer.Exit")
    assert launched == [True]


def test_cli_no_args_prints_help_without_tty():
    from typer.testing import CliRunner

    from conso.app import cli

    result = CliRunner().invoke(cli.app, [])
    assert result.exit_code == 0
    assert "Usage" in result.output


def test_cli_subcommand_is_untouched_by_the_dashboard(monkeypatch):
    from typer.testing import CliRunner

    from conso.app import cli

    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    launched: list[bool] = []
    monkeypatch.setattr(dashboard, "run_dashboard", lambda: launched.append(True) or 0)
    result = CliRunner().invoke(cli.app, ["--help"])
    assert result.exit_code == 0
    assert launched == []
    assert "Usage" in result.output

# ---------------------------------------------------------------------------
# Typer marker defaults (regression: menu 1 crashed with
# "unsupported operand type(s) for *: 'OptionInfo' and 'int'")
# ---------------------------------------------------------------------------
def test_declared_defaults_resolve_typer_markers():
    """Every command's marker defaults must resolve to plain values."""
    from conso.app.commands.accounts import session as session_cmd
    from conso.app.commands.earning import loop as loop_cmd

    loop_defaults = dashboard._declared_defaults(loop_cmd)
    assert loop_defaults["interval_hours"] == 24.0  # the value the crash needed
    assert loop_defaults["cycles"] == 0
    assert isinstance(loop_defaults["once"], bool)

    session_defaults = dashboard._declared_defaults(session_cmd)
    assert session_defaults == {"email": "", "force": False}


def test_call_binds_every_parameter_so_markers_never_reach_the_body():
    """The regression itself: loop() must not receive an OptionInfo."""
    from conso.app.commands.earning import loop as loop_cmd

    seen: dict[str, object] = {}

    def fake_loop(**kwargs):  # mirrors the real signature's parameters
        seen.update(kwargs)

    # Swap in a recorder shaped like the real command, then bind like _call does.
    bound = dashboard._declared_defaults(loop_cmd)
    bound.update({"once": True})
    fake_loop(**bound)
    assert bound["interval_hours"] == 24.0
    assert all(not k.startswith("_") for k in seen)
    assert all(type(v).__name__ not in ("OptionInfo", "ArgumentInfo") for v in seen.values())


def test_loop_action_passes_real_numbers_not_markers(tmp_path, monkeypatch):
    """Menu 1 end-to-end (mocked command): the exact call that used to crash."""
    _use_tmp_store(tmp_path, monkeypatch)
    from conso.app.commands.earning import loop as loop_cmd

    calls: list[dict] = []
    monkeypatch.setattr(dashboard, "loop_cmd", lambda **kw: calls.append(kw))
    # Patch the resolver so the recorder (lambda **kw) binds cleanly.
    monkeypatch.setattr(dashboard, "_call", lambda fn, **kw: fn(**kw))
    _patch_io(monkeypatch, _Scripted(["1", "q"], [False, 1, 10, 1.0, True]))
    dashboard.run_dashboard()
    assert calls == [{"turns": 10, "once": True, "force": False, "workers": 1,
                      "min_remaining": 1.0}]


def test_declared_defaults_skips_required_markers():
    """A required argument (Typer marks it ``...``) must stay unbound.

    ``register``'s ``count`` is a required Argument: resolving it to Ellipsis
    would pass ``...`` into the command body when a call site forgot it.
    """
    from conso.app.commands.accounts import register_cmd

    defaults = dashboard._declared_defaults(register_cmd)
    assert "count" not in defaults  # required: the caller must supply it
    assert defaults["referral"] == "" and defaults["earn"] is False
    assert defaults["turns"] == 10 and defaults["dry_run"] is False


def _grid_plain(table) -> str:
    """Plain text of a Rich grid/table (they have no ``.plain``)."""
    import io

    from rich.console import Console

    console = Console(file=io.StringIO(), width=110, highlight=False)
    console.print(table)
    return console.file.getvalue()


# ---------------------------------------------------------------------------
# Panel layout (the grouped/restyled render)
# ---------------------------------------------------------------------------
def test_render_groups_menu_by_section(tmp_path, monkeypatch):
    """The panel must read as sections, not one flat list."""
    _use_tmp_store(tmp_path, monkeypatch)
    state = dashboard._DashboardState(settings=dashboard.Settings.from_env(),
                                      store=Store(str(tmp_path)))
    panel = dashboard._panel(state)
    text = dashboard.render_plain(state)
    for section in ("EARN", "ACCOUNTS", "TOOLS", "INFO"):
        assert section in text, f"section {section} missing"
    assert "\u2500" in text  # the divider rule between status and menu
    assert panel.subtitle is not None
    assert "quit" in panel.subtitle.plain


def test_accounts_block_flags_problems_only_when_present(tmp_path, monkeypatch):
    """The account block stays quiet on a clean store and warns on failures."""
    _use_tmp_store(tmp_path, monkeypatch)
    _seed(tmp_path, "a@example.invalid", "b@example.invalid")

    clean = dashboard._accounts_block(Store(str(tmp_path)).all())
    rendered_clean = _grid_plain(clean)
    assert "all clean" in rendered_clean
    assert "needs attention" not in rendered_clean

    store = Store(str(tmp_path))
    store.update("a@example.invalid", status="failed")
    store.update("b@example.invalid", status="banned")
    flagged = _grid_plain(dashboard._accounts_block(store.all()))
    assert "needs attention" in flagged and "will not earn" in flagged
    assert "all clean" not in flagged


def test_rule_width_follows_the_console(tmp_path, monkeypatch):
    """The divider adapts to the terminal instead of a hard-coded width."""
    from rich.console import Console
    import io

    monkeypatch.setattr(ui, "console", Console(width=80, file=io.StringIO()))
    assert dashboard._rule_width() == 76
    monkeypatch.setattr(ui, "console", Console(width=120, file=io.StringIO()))
    assert dashboard._rule_width() == 116


# ---------------------------------------------------------------------------
# TODAY block (daily-loop ledger)
# ---------------------------------------------------------------------------
def _write_ledger(tmp_path: Path, day: str, entries: dict) -> None:
    store = Store(str(tmp_path))
    state = store.load_state()
    state.setdefault("daily_loop", {})[day] = entries
    store.save_state(state)


def _today() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).date().isoformat()


def test_today_block_reports_idle_without_a_ledger(tmp_path, monkeypatch):
    _use_tmp_store(tmp_path, monkeypatch)
    state = dashboard._DashboardState(settings=dashboard.Settings.from_env(),
                                      store=Store(str(tmp_path)))
    text = _grid_plain(dashboard._today_block(state))
    assert "idle" in text
    assert "menu 1" in text


def test_today_block_summarises_the_ledger(tmp_path, monkeypatch):
    """zaps/turns/accounts come from the scheduler's own ledger."""
    _use_tmp_store(tmp_path, monkeypatch)
    _write_ledger(tmp_path, _today(), {
        "a@x.com": {"status": "ok", "zaps": 20.0, "turns": 10},
        "b@x.com": {"status": "ok", "zaps": 15.0, "turns": 7},
        "c@x.com": {"status": "capped", "zaps": 1.0, "turns": 0},
    })
    state = dashboard._DashboardState(settings=dashboard.Settings.from_env(),
                                      store=Store(str(tmp_path)))
    text = _grid_plain(dashboard._today_block(state))
    assert "36.00" in text          # 20 + 15 + 1
    assert "17" in text             # 10 + 7 turns
    assert "2 ok" in text and "1 capped" in text
    assert "2.12" in text           # per-turn average 36/17


def test_today_block_ignores_other_days(tmp_path, monkeypatch):
    _use_tmp_store(tmp_path, monkeypatch)
    _write_ledger(tmp_path, "2000-01-01", {"a@x.com": {"status": "ok", "zaps": 99.0}})
    state = dashboard._DashboardState(settings=dashboard.Settings.from_env(),
                                      store=Store(str(tmp_path)))
    text = _grid_plain(dashboard._today_block(state))
    assert "99" not in text and "idle" in text


def test_bar_clamps_and_scales():
    assert dashboard._bar(0.0) == "░" * dashboard._BAR_WIDTH
    assert dashboard._bar(1.0) == "█" * dashboard._BAR_WIDTH
    assert dashboard._bar(2.5) == "█" * dashboard._BAR_WIDTH   # clamped
    assert dashboard._bar(-1.0) == "░" * dashboard._BAR_WIDTH  # clamped
    half = dashboard._bar(0.5)
    assert half.count("█") == dashboard._BAR_WIDTH // 2


def test_panel_shows_runtime_facts(tmp_path, monkeypatch):
    """Solver/verifier/proxy/store must be visible without truncation."""
    _use_tmp_store(tmp_path, monkeypatch)
    state = dashboard._DashboardState(settings=dashboard.Settings.from_env(),
                                      store=Store(str(tmp_path)))
    text = dashboard.render_plain(state)
    assert "solver" in text and "verifier" in text
    assert "proxies" in text and "store" in text
    assert "\u2026" not in text  # no ellipsis-truncated facts


# ---------------------------------------------------------------------------
# Monitoring-console additions (sparkline / health / log / uptime / status)
# ---------------------------------------------------------------------------
def test_sparkline_scales_to_the_series():
    flat = dashboard._spark([0.0, 0.0, 0.0])
    assert flat == dashboard._SPARK[0] * 3          # no divide-by-zero
    assert dashboard._spark([]) == dashboard._SPARK[0]
    rising = dashboard._spark([0.0, 50.0, 100.0])
    assert rising[0] < rising[-1]                    # shape is preserved
    assert dashboard._spark([100.0]) == dashboard._SPARK[-1]


def test_recent_days_includes_empty_days_as_zero(tmp_path, monkeypatch):
    """Gaps must show as flat days, not be compressed away."""
    _use_tmp_store(tmp_path, monkeypatch)
    from datetime import datetime, timedelta, timezone

    today = datetime.now(timezone.utc).date()
    store = Store(str(tmp_path))
    state = store.load_state()
    state["daily_loop"] = {
        today.isoformat(): {"a@x.com": {"zaps": 10.0}},
        (today - timedelta(days=2)).isoformat(): {"b@x.com": {"zaps": 5.0}},
    }
    store.save_state(state)
    dash = dashboard._DashboardState(settings=dashboard.Settings.from_env(), store=store)

    series = dashboard._recent_days(dash, 3)
    assert [round(z) for _day, z in series] == [5, 0, 10]


def test_trend_block_reports_total_and_best(tmp_path, monkeypatch):
    _use_tmp_store(tmp_path, monkeypatch)
    from datetime import datetime, timezone

    store = Store(str(tmp_path))
    state = store.load_state()
    state["daily_loop"] = {
        datetime.now(timezone.utc).date().isoformat(): {"a@x.com": {"zaps": 42.0}},
    }
    store.save_state(state)
    dash = dashboard._DashboardState(settings=dashboard.Settings.from_env(), store=store)
    text = _grid_plain(dashboard._trend_block(dash))
    assert "TREND 7d" in text
    assert "42" in text and "best" in text


def test_health_block_shows_provider_indicators(tmp_path, monkeypatch):
    _use_tmp_store(tmp_path, monkeypatch)
    monkeypatch.setenv("CAPTCHA_PROVIDER", "camoufox")
    monkeypatch.setattr(dashboard, "_provider_state", lambda name: (True, "ready"))
    state = dashboard._DashboardState(settings=dashboard.Settings.from_env(),
                                      store=Store(str(tmp_path)))
    text = _grid_plain(dashboard._health_block(state, state.store.all()))
    assert "HEALTH" in text and "1/1 providers ready" in text
    assert "camoufox" in text and "\u25cf" in text


def test_health_block_marks_unready_providers(tmp_path, monkeypatch):
    _use_tmp_store(tmp_path, monkeypatch)
    monkeypatch.setenv("CAPTCHA_PROVIDER", "capsolver,camoufox")
    monkeypatch.setattr(dashboard, "_provider_state", lambda name: (name == "camoufox", ""))
    state = dashboard._DashboardState(settings=dashboard.Settings.from_env(),
                                      store=Store(str(tmp_path)))
    text = _grid_plain(dashboard._health_block(state, state.store.all()))
    assert "1/2 providers ready" in text


def test_set_result_appends_to_the_session_log(tmp_path, monkeypatch):
    _use_tmp_store(tmp_path, monkeypatch)
    state = dashboard._DashboardState(settings=dashboard.Settings.from_env(),
                                      store=Store(str(tmp_path)))
    dashboard._set_result(state, "farm", "done")
    dashboard._set_result(state, "doctor", "done")
    assert [message for _stamp, message in state.history] == ["farm: done", "doctor: done"]
    assert state.last_result == "doctor: done"


def test_log_block_caps_the_tail(tmp_path, monkeypatch):
    _use_tmp_store(tmp_path, monkeypatch)
    state = dashboard._DashboardState(settings=dashboard.Settings.from_env(),
                                      store=Store(str(tmp_path)))
    for index in range(dashboard._LOG_TAIL + 4):
        dashboard._set_result(state, "action", str(index))
    assert len(state.history) == dashboard._LOG_TAIL
    assert state.history[-1][1] == f"action: {dashboard._LOG_TAIL + 3}"


def test_log_block_handles_an_empty_session(tmp_path, monkeypatch):
    _use_tmp_store(tmp_path, monkeypatch)
    state = dashboard._DashboardState(settings=dashboard.Settings.from_env(),
                                      store=Store(str(tmp_path)))
    text = _grid_plain(dashboard._log_block(state))
    assert "no actions yet this session" in text


def test_uptime_formats_seconds_minutes_hours(monkeypatch):
    import time as _time

    state = dashboard._DashboardState(settings=dashboard.Settings.from_env(),
                                      store=Store("."))
    monkeypatch.setattr(dashboard.time, "monotonic", lambda: state.started_at + 42)
    assert dashboard._uptime(state) == "42s"
    monkeypatch.setattr(dashboard.time, "monotonic", lambda: state.started_at + 125)
    assert dashboard._uptime(state) == "2m05s"
    monkeypatch.setattr(dashboard.time, "monotonic", lambda: state.started_at + 3725)
    assert dashboard._uptime(state) == "1h02m"


def test_panel_subtitle_reports_system_status(tmp_path, monkeypatch):
    """READY / ATTENTION / EMPTY drive the header indicator."""
    _use_tmp_store(tmp_path, monkeypatch)
    _seed(tmp_path, "a@example.invalid")
    state = dashboard._DashboardState(settings=dashboard.Settings.from_env(),
                                      store=Store(str(tmp_path)))
    assert "READY" in dashboard._panel(state).subtitle.plain

    Store(str(tmp_path)).update("a@example.invalid", status="failed")
    state = dashboard._DashboardState(settings=dashboard.Settings.from_env(),
                                      store=Store(str(tmp_path)))
    assert "ATTENTION" in dashboard._panel(state).subtitle.plain

    # A different DATA_DIR: _panel refreshes the store from settings, so
    # swapping the Store object alone would not empty the fleet.
    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()
    monkeypatch.setenv("DATA_DIR", str(empty_dir))
    empty = dashboard._DashboardState(settings=dashboard.Settings.from_env(),
                                      store=Store(str(empty_dir)))
    assert "EMPTY" in dashboard._panel(empty).subtitle.plain


def test_panel_renders_on_a_narrow_terminal(tmp_path, monkeypatch):
    """Everything must survive an 80-column terminal without truncation."""
    _use_tmp_store(tmp_path, monkeypatch)
    _seed(tmp_path, "a@example.invalid")
    state = dashboard._DashboardState(settings=dashboard.Settings.from_env(),
                                      store=Store(str(tmp_path)))
    text = dashboard.render_plain(state, width=80)
    for marker in ("FLEET", "TODAY", "HEALTH", "TREND", "LOG", "EARN", "TOOLS", "INFO"):
        assert marker in text


# ---------------------------------------------------------------------------
# Menu layout (section rules, aligned columns, narrow fallback)
# ---------------------------------------------------------------------------
def _menu_lines(width: int) -> list[str]:
    """Render the menu for a GIVEN width.

    ``_menu_table`` takes the width explicitly (the panel passes its own), so
    this is deterministic instead of depending on the ambient console.
    """
    import io

    from rich.console import Console

    console = Console(file=io.StringIO(), width=width, highlight=False)
    console.print(dashboard._menu_table(width))
    return [l for l in console.file.getvalue().split("\n") if l.strip()]


def test_menu_section_headings_carry_a_filling_rule():
    """Each group is anchored by 'SECTION ────' so the block reads as a table."""
    lines = _menu_lines(110)
    for section in ("EARN", "ACCOUNTS", "TOOLS", "INFO"):
        heading = next((l for l in lines if l.strip().startswith(section)), None)
        assert heading is not None, f"{section} heading missing"
        assert "─" in heading, f"{section} heading has no rule"


def test_menu_rows_align_keys_and_labels():
    """Keys are right-aligned and labels share one column across all rows."""
    lines = _menu_lines(110)
    row_1 = next(l for l in lines if "daily run" in l)
    row_9 = next(l for l in lines if "doctor" in l)
    row_17 = next(l for l in lines if "pipeline" in l)
    # every label starts at the same column
    for row in (row_1, row_9, row_17):
        assert row.index("d") if "daily" in row else row.index(row.split("  ", 1)[-1]) > 0
    assert row_1.index("daily run") == row_9.index("doctor") == row_17.index("pipeline")


def test_menu_switches_to_one_column_when_narrow():
    """Below the two-up threshold each entry gets its own row."""
    wide = "\n".join(_menu_lines(110))
    assert "1  daily run" in wide and "2  farm turns" in wide.split("3  claim")[0]

    narrow = "\n".join(_menu_lines(dashboard._MENU_TWO_UP_WIDTH - 1))
    assert "1  daily run" in narrow
    assert "2  farm turns" in narrow
    # they must NOT share a line any more
    assert not any("daily run" in l and "farm turns" in l for l in narrow.split("\n"))


def test_menu_two_up_threshold_matches_its_content():
    """The threshold is derived from the labels, not hard-coded."""
    assert dashboard._MENU_TWO_UP_WIDTH == 12 + dashboard._MENU_LEFT_WIDTH + dashboard._MENU_RIGHT_WIDTH
    assert dashboard._MENU_LEFT_WIDTH == len("pipeline (register -> earn -> loop)")


# ---------------------------------------------------------------------------
# Tabs (one per category)
# ---------------------------------------------------------------------------
def _tab_lines(tab: str, width: int = 110) -> list[str]:
    import io

    from rich.console import Console

    console = Console(file=io.StringIO(), width=width, highlight=False)
    console.print(dashboard._menu_table(width, tab))
    return [l for l in console.file.getvalue().split("\n") if l.strip()]


def test_tabs_cover_all_then_each_category():
    assert dashboard._TABS[0] == "ALL"
    assert set(dashboard._TABS[1:]) == {s for s, _e in dashboard._MENU_SECTIONS}


def test_tab_bar_marks_the_active_tab():
    text = _tab_lines("TOOLS")[0]
    for tab in dashboard._TABS:
        assert tab in text
    assert "switch tab" in text


def test_each_tab_shows_only_its_own_section():
    earn = "\n".join(_tab_lines("EARN"))
    assert "daily run" in earn and "farm turns" in earn
    assert "doctor" not in earn and "register new accounts" not in earn

    tools = "\n".join(_tab_lines("TOOLS"))
    assert "doctor" in tools and "pipeline" in tools
    assert "daily run" not in tools


def test_all_tab_still_shows_every_entry():
    text = "\n".join(_tab_lines("ALL"))
    for _key, _label, _handler in dashboard._MENU:
        pass
    for key in [k for k, _l, _h in dashboard._MENU]:
        assert key in text
    assert "daily run" in text and "pipeline" in text and "quit" in text


def test_switch_tab_cycles_and_jumps(tmp_path, monkeypatch):
    _use_tmp_store(tmp_path, monkeypatch)
    state = dashboard._DashboardState(settings=dashboard.Settings.from_env(),
                                      store=Store(str(tmp_path)))
    assert state.tab == "ALL"

    assert dashboard._switch_tab(state, "") == "tab EARN"      # cycles
    assert state.tab == "EARN"
    assert dashboard._switch_tab(state, "tools") == "tab TOOLS"  # case-insensitive jump
    assert state.tab == "TOOLS"
    assert dashboard._switch_tab(state, "acc") == "tab ACCOUNTS"  # unique prefix
    assert dashboard._switch_tab(state, "nope").startswith("unknown tab")
    assert state.tab == "ACCOUNTS"  # a bad name leaves the tab alone


def test_tab_command_switches_without_running_a_command(tmp_path, monkeypatch):
    """Typing ``t`` must switch the tab, log it, and run nothing itself."""
    _use_tmp_store(tmp_path, monkeypatch)
    calls: list[str] = []
    monkeypatch.setattr(dashboard, "doctor_cmd", lambda: calls.append("doctor"))
    logged: list[tuple[str, str]] = []
    real_set_result = dashboard._set_result

    def spy(state, label, outcome):
        real_set_result(state, label, outcome)
        logged.append((label, outcome))

    monkeypatch.setattr(dashboard, "_set_result", spy)
    _patch_io(monkeypatch, _Scripted(["t earn", "9", "q"]))
    assert dashboard.run_dashboard() == 0
    assert ("menu", "tab EARN") in logged      # the switch was recorded
    assert calls == ["doctor"]                 # and only entry 9 ran a command


def test_number_keys_work_on_any_tab(tmp_path, monkeypatch):
    """Tabs are presentation only: dispatch stays global."""
    _use_tmp_store(tmp_path, monkeypatch)
    calls: list[dict] = []
    monkeypatch.setattr(dashboard, "farm_cmd", lambda **k: calls.append(k))
    monkeypatch.setattr(dashboard, "_call", lambda fn, **kw: fn(**kw))
    _patch_io(monkeypatch, _Scripted(["t tools", "2", "q"], [1, "all", False, True, True]))
    assert dashboard.run_dashboard() == 0
    assert calls and calls[0]["turns"] == 1
