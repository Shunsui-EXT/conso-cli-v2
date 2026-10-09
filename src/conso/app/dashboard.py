"""
Interactive dashboard for the headless Conso CLI.

WHY this module exists: the pipeline is a set of one-shot Typer subcommands,
and day-to-day operation means remembering which flag belongs to which
command. This module renders one panel with the store state and a menu, asks
only for the values an action needs, and then calls the very same command
callables Typer exposes — pacing, daily caps and store writes stay identical
to ``python main.py <cmd>``.

The dashboard is the outermost shell, so it must outlive any single command:
every action body (its prompts included) runs inside a guard that absorbs
``typer.Exit``, ``ConsoAPIError``, ``KeyboardInterrupt`` and any other
exception. Ctrl+C at the menu itself exits cleanly with code 0. Long-running
or destructive actions are confirmed first, naming exactly what will run.

Testability: all user input flows through the module-level ``_choose`` /
``_ask`` / ``_confirm`` / ``_int_ask`` seams, so tests can script a whole
session without a TTY and without touching the network.
"""

from __future__ import annotations

import os
import signal
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

import typer
from rich.panel import Panel
from rich.prompt import Confirm, IntPrompt, Prompt
from rich.table import Table
from rich.text import Text

from ..core.config import Settings
from ..core.constants import DAILY_ZAP_CAP
from ..net.client import ConsoAPIError
from ..ops.notify import is_configured
from ..solver import TURNSTILE_PAGE_URL, TURNSTILE_SITEKEY
from ..solver.chain import _provider_names
from ..store.storage import AccountRecord, Store
from . import ui
from .commands.accounts import export as export_cmd
from .commands.accounts import recover as recover_cmd
from .commands.accounts import register_cmd
from .commands.accounts import session as session_cmd
from .commands.accounts import status as status_cmd
from .commands.earning import earn as earn_cmd
from .commands.earning import farm as farm_cmd
from .commands.earning import loop as loop_cmd
from .commands.earning import pipeline as pipeline_cmd
from .commands.earning import report as report_cmd
from .commands.maintenance import doctor as doctor_cmd
from .commands.maintenance import notify as notify_cmd
from .commands.maintenance import proxies as proxies_cmd
from .commands.maintenance import solve as solve_cmd
from .commands.maintenance import test as test_cmd
from .commands.maintenance import verify as verify_cmd
from .monitor import stop_monitor


def _declared_defaults(fn: Any) -> dict[str, Any]:
    """Resolve a command's parameter defaults to real values.

    WHY: Typer leaves every default it rewrote as a ``typer.Option(...)`` /
    ``typer.Argument(...)`` marker object (``OptionInfo``/``ArgumentInfo``) on
    the function signature. Calling such a function directly — as this
    dashboard does — would pass the marker into the body wherever the caller
    omitted the parameter: ``OptionInfo * 3600`` raised a TypeError in
    ``loop``, and a truthy marker silently turned ``earn`` into its
    ``--list`` mode. Parsing each marker's own ``default`` restores the value
    Typer itself would have supplied, and keeps working when a command grows
    a new option (the new parameter just falls back to its declared default).
    """
    import inspect

    resolved: dict[str, Any] = {}
    for name, param in inspect.signature(fn).parameters.items():
        # Variadics (*args/**kwargs) have no default to restore, and tests use
        # `lambda **kw` recorders — binding `_empty` into them would break.
        if param.kind in (param.VAR_POSITIONAL, param.VAR_KEYWORD):
            continue
        default = param.default
        if default is inspect.Parameter.empty:
            continue
        if default.__class__.__name__ in ("OptionInfo", "ArgumentInfo"):
            default = getattr(default, "default", inspect.Parameter.empty)
        if default is inspect.Parameter.empty:
            continue
        # Typer marks a required argument/option with ``...``; that is not a
        # value. Leaving it unbound makes a missing call-site argument fail
        # loudly (TypeError from the call) instead of reaching the body as
        # ``Ellipsis``.
        if default is Ellipsis:
            continue
        if default.__class__.__name__ in ("OptionInfo", "ArgumentInfo"):
            continue
        resolved[name] = default
    return resolved


def _call(fn: Any, **kwargs: Any) -> Any:
    """Invoke a Typer command callable with every parameter bound.

    See ``_declared_defaults`` for why unbound markers must never reach the
    command body.
    """
    bound = _declared_defaults(fn)
    bound.update(kwargs)
    return fn(**bound)

#: Menu as the panel prints it: ``(section, ((left key, left label, right key,
#: right label), ...))``. ``_MENU`` below is the dispatch table — this one is
#: presentational only, so the two are kept in sync by hand.
_MENU_SECTIONS: tuple[tuple[str, tuple[tuple[str, str, str, str], ...]], ...] = (
    ("EARN", (
        ("1", "daily run (loop --once)", "2", "farm turns"),
        ("3", "claim missions", "4", "earn on ONE account"),
    )),
    ("ACCOUNTS", (
        ("5", "accounts status", "6", "sessions (refresh)"),
        ("7", "recover sessions", "8", "register new accounts"),
    )),
    ("TOOLS", (
        ("9", "doctor (readiness)", "10", "test (pre-flight)"),
        ("11", "solve captcha", "12", "proxies"),
        ("13", "export store", "14", "report"),
        ("15", "verify proxies", "16", "notify test"),
        ("17", "pipeline (register -> earn -> loop)", "", ""),
    )),
    ("INFO", (
        ("S", "summary/settings", "Q", "quit"),
    )),
)

#: Fallback width for the in-panel dividers. The real width is derived from the
#: console at render time so the rule always meets the panel borders, on an
#: 80-column terminal as well as a wide one.
_RULE_WIDTH = 68


def _rule_width() -> int:
    """Inner width of the panel: console width minus borders and padding."""
    try:
        return max(20, ui.console.width - 4)
    except Exception:  # noqa: BLE001 - a weird console must not break a repaint
        return _RULE_WIDTH


@dataclass
class _DashboardState:
    """Everything the panel renders plus the session-scoped "recent" line."""

    settings: Settings
    store: Store
    last_result: str = "none yet"
    #: monotonic clock of the session start (uptime in the header)
    started_at: float = field(default_factory=time.monotonic)
    #: (HH:MM:SS, "label: outcome") per action, capped by _LOG_TAIL
    history: list[tuple[str, str]] = field(default_factory=list)
    #: active menu tab (one of _TABS); ALL shows every category at once
    tab: str = "ALL"

    def refresh(self) -> list[AccountRecord]:
        """Re-read the store from disk and return its accounts.

        WHY: every command builds its own ``Store`` over the same directory,
        so the dashboard's instance goes stale the moment an action writes
        (register, farm, recover). Rebuilding is cheap for a JSON file and
        keeps the counts honest without reaching into Store internals.
        """
        self.store = Store(self.settings.data_dir)
        return self.store.all()


# ---------------------------------------------------------------------------
# Prompt seams (the only places the dashboard reads user input)
# ---------------------------------------------------------------------------
def _choose(prompt: str = "select") -> str:
    """One menu answer; empty input is returned as ``""`` (not ``None``)."""
    answer = Prompt.ask(Text(prompt), default="", show_default=False, console=ui.console)
    return (answer or "").strip()


def _ask(prompt: str, default: str = "") -> str:
    """One text answer; empty input yields the bracketed default."""
    answer = Prompt.ask(Text(prompt), default=default, show_default=False, console=ui.console)
    return (answer or "").strip()


def _confirm(prompt: str, default: bool = False) -> bool:
    """One yes/no answer; ``default`` decides what Enter means."""
    return bool(Confirm.ask(Text(prompt), default=default, show_default=False, console=ui.console))


def _int_ask(prompt: str, default: int) -> int:
    """One integer answer; Rich re-asks until the input parses."""
    return int(IntPrompt.ask(Text(prompt), default=default, show_default=False, console=ui.console))


def _float_ask(prompt: str, default: float, *, default_label: str = "") -> float:
    """Float answer with a display label that need not be the numeric default.

    ``min-remaining`` defaults to the ``.env`` value, which the command reads
    when the flag is negative; showing ``-1`` would be honest but unreadable,
    so the label says where the number really comes from.
    """
    shown = default_label or f"{default:g}"
    raw = _ask(f"{prompt} [{shown}]", f"{default:g}")
    try:
        return float(raw)
    except ValueError:
        ui.error(f"not a number: {raw!r} (using {shown})")
        return default


# ---------------------------------------------------------------------------
# Panel
# ---------------------------------------------------------------------------
def _solver_chain() -> list[str]:
    """The provider chain ``build_solver`` would walk (first one wins)."""
    return _provider_names(os.environ.get("CAPTCHA_PROVIDER")) or ["cloak", "camoufox"]


def _set_result(state: _DashboardState, label: str, outcome: str) -> None:
    """Update the recent-results line and append to the session log."""
    state.last_result = f"{label}: {outcome}"
    stamp = datetime.now().strftime("%H:%M:%S")
    state.history.append((stamp, f"{label}: {outcome}"))
    del state.history[:-_LOG_TAIL]


# ---------------------------------------------------------------------------
# Panel building blocks
# ---------------------------------------------------------------------------
_BAR_FULL = "█"
_BAR_EMPTY = "░"
_BAR_WIDTH = 10
_SPARK = "▁▂▃▄▅▆▇█"
_LOG_TAIL = 5


def _bar(ratio: float, width: int = _BAR_WIDTH) -> str:
    """A block meter; clamped so a bad ratio can never overflow the layout."""
    ratio = min(1.0, max(0.0, ratio))
    filled = int(round(ratio * width))
    return _BAR_FULL * filled + _BAR_EMPTY * (width - filled)


def _spark(values: list[float]) -> str:
    """One glyph per value, scaled to the series maximum.

    All-zero (or empty) series render flat rather than dividing by zero; the
    point of the trend row is the SHAPE of the last week, not the axis.
    """
    if not values:
        return _SPARK[0]
    top = max(values) or 1.0
    return "".join(_SPARK[min(len(_SPARK) - 1, int(v / top * (len(_SPARK) - 1) + 0.5))] for v in values)


def _today_ledger(state: _DashboardState) -> dict[str, dict]:
    """Today's per-account results from the daily-loop ledger.

    The ledger is what the scheduler wrote during its last run, so it is the
    only honest source for "what happened today": ``Store.total_zaps`` is a
    per-run snapshot, not a daily accumulator.
    """
    state.refresh()
    day = datetime.now(timezone.utc).date().isoformat()
    return state.store.load_state().get("daily_loop", {}).get(day, {})


def _recent_days(state: _DashboardState, days: int = 7) -> list[tuple[str, float]]:
    """Zaps per day for the last ``days`` calendar days (oldest first).

    Days with no ledger entry count as 0 so the sparkline shows gaps honestly
    instead of compressing them away.
    """
    state.refresh()
    loop = state.store.load_state().get("daily_loop", {})
    today = datetime.now(timezone.utc).date()
    out: list[tuple[str, float]] = []
    for offset in range(days - 1, -1, -1):
        day = (today - timedelta(days=offset)).isoformat()
        zaps = sum(float(v.get("zaps", 0) or 0) for v in loop.get(day, {}).values())
        out.append((day, zaps))
    return out


def _kv_table() -> Table:
    """A label/value/extra grid used by the status blocks."""
    table = Table.grid(padding=(0, 1))
    table.add_column(justify="left", style="dim", no_wrap=True)
    table.add_column(justify="right", no_wrap=True)
    table.add_column(justify="left", no_wrap=True)
    return table


def _accounts_block(records: list[AccountRecord]) -> Table:
    total = len(records)
    active = sum(1 for r in records if r.status == "active")
    farmed = sum(1 for r in records if r.status == "farmed")
    failed = sum(1 for r in records if r.status == "failed")
    banned = sum(1 for r in records if r.status == "banned")
    ready = active + farmed
    share = (ready / total) if total else 0.0

    table = _kv_table()
    table.add_row("FLEET", "", "")
    table.add_row("total", str(total), "")
    table.add_row("ready", f"{ready}", Text(f"  {_bar(share)} {share * 100:3.0f}%", style="green"))
    if failed:
        table.add_row("failed", str(failed), Text("  needs attention", style="red"))
    if banned:
        table.add_row("banned", str(banned), Text("  will not earn", style="red"))
    if not failed and not banned:
        table.add_row("issues", "0", Text("  all clean", style="green"))
    return table


def _today_block(state: _DashboardState) -> Table:
    ledger = _today_ledger(state)
    table = _kv_table()
    day = datetime.now(timezone.utc).date().isoformat()
    table.add_row("TODAY", day, "")

    if not ledger:
        table.add_row("status", "idle", Text("  no run yet", style="yellow"))
        table.add_row("next", "", Text("  menu 1 = daily run", style="dim"))
        return table

    zaps = sum(float(v.get("zaps", 0) or 0) for v in ledger.values())
    turns = sum(int(v.get("turns", 0) or 0) for v in ledger.values())
    ok = sum(1 for v in ledger.values() if v.get("status") == "ok")
    capped = sum(1 for v in ledger.values() if v.get("status") == "capped")
    other = len(ledger) - ok - capped

    # The scheduler's own budget: every account that ran today may earn up to
    # the practical daily cap, so the meter reads "how much of today's
    # potential did the run actually collect".
    target = DAILY_ZAP_CAP * max(1, ok + capped)
    share = zaps / target if target else 0.0

    table.add_row("zaps", f"{zaps:,.2f}", Text(f"  {_bar(share)} {share * 100:3.0f}%", style="green"))
    table.add_row("turns", f"{turns:,}", "")
    counts = f"{ok} ok"
    if capped:
        counts += f" · {capped} capped"
    if other:
        counts += f" · {other} other"
    table.add_row("accounts", counts, "")
    if turns:
        table.add_row("per turn", f"{zaps / turns:,.2f}", Text("  zaps", style="dim"))
    return table


def _short_path(path: str) -> str:
    """Path relative to the cwd when possible, so the store fits on one line."""
    try:
        rel = os.path.relpath(path)
    except (ValueError, OSError):
        return path
    return rel if not rel.startswith("..") else path


def _provider_state(name: str) -> tuple[bool, str]:
    """``(ready, note)`` for one solver provider, without launching a browser.

    Delegates to the command layer's own check so the dashboard can never
    disagree with ``doctor``. A check failure degrades to "unknown" rather
    than killing the repaint.
    """
    try:
        from .commands.maintenance import _provider_ready

        return _provider_ready(name)
    except Exception as exc:  # noqa: BLE001 - a probe must not break the panel
        return False, f"probe failed: {exc}"


def _health_block(state: _DashboardState, records: list[AccountRecord]) -> Table:
    """Solver readiness, verifier, proxies and store — the system's vitals."""
    grid = Table.grid(padding=(0, 2))
    grid.add_column(justify="left", no_wrap=True)
    grid.add_column(justify="left", overflow="fold")

    chain = _solver_chain()
    ready = 0
    parts = Text()
    for index, name in enumerate(chain):
        ok, _note = _provider_state(name)
        ready += 1 if ok else 0
        if index:
            parts.append(" · ", style="dim")
        parts.append(name, style="magenta")
        parts.append(" ●", style="green" if ok else "red")

    grid.add_row(
        Text("HEALTH", style="dim"),
        Text(f"{ready}/{len(chain)} providers ready", style="green" if ready else "red"),
    )
    grid.add_row(Text("solver", style="dim"), parts)
    proxied = sum(1 for r in records if r.proxy)
    pool = len(state.settings.proxy.urls)
    grid.add_row(
        Text("verifier", style="dim"),
        Text(state.settings.verifier, style="cyan")
        + Text("    proxies ", style="dim")
        + Text(f"{pool} pool · {proxied} pinned" if pool else "direct", style="cyan"),
    )
    grid.add_row(
        Text("store", style="dim"),
        Text(_short_path(state.store.json_path), style="cyan"),
    )
    return grid


def _trend_block(state: _DashboardState) -> Table:
    """Last 7 days of zaps as a sparkline plus the series total."""
    series = _recent_days(state, 7)
    values = [zaps for _day, zaps in series]
    total = sum(values)
    best = max(values) if values else 0.0

    grid = Table.grid(padding=(0, 2))
    grid.add_column(justify="left", no_wrap=True)
    grid.add_column(justify="left", no_wrap=True)
    grid.add_column(justify="left", no_wrap=True)
    grid.add_row(
        Text("TREND 7d", style="dim"),
        Text(_spark(values), style="cyan"),
        Text(f"{total:,.0f} zaps total · best {best:,.0f}/day", style="dim"),
    )
    return grid


#: Menu column geometry, derived from the labels themselves so the layout
#: cannot drift when an entry is renamed.
_MENU_LEFT_WIDTH = max(len(label) for _s, entries in _MENU_SECTIONS
                       for _k, label, _rk, _rl in entries)
_MENU_RIGHT_WIDTH = max((len(label) for _s, entries in _MENU_SECTIONS
                         for _k, _l, key, label in entries if key), default=0)
#: Width a two-up row needs: indent+key+gaps (12) plus both label columns.
_MENU_TWO_UP_WIDTH = 12 + _MENU_LEFT_WIDTH + _MENU_RIGHT_WIDTH


def _menu_section(section: str, width: int) -> Text:
    """Section heading followed by a rule that fills the rest of the row.

    The rule is what turns the menu into a table of contents: it anchors each
    group and claims the dead space at the right edge of the panel, which is
    what made the old flat list look ragged.
    """
    head = Text()
    head.append(f" {section} ", style="bold #58a6ff")
    head.append("─" * max(0, width - len(section) - 2), style="grey37")
    return head


def _menu_row(left_key: str, left_label: str,
              right_key: str, right_label: str) -> Text:
    """One menu line: keys right-aligned, labels padded to a fixed column."""
    row = Text()
    row.append(f"  {left_key:>2}  ", style="bold cyan")
    row.append(f"{left_label:<{_MENU_LEFT_WIDTH}}", style="white")
    if right_key:
        row.append(f"  {right_key:>2}  ", style="bold cyan")
        row.append(right_label, style="white")
    return row


#: Tab order: ALL first, then one tab per category. Typing ``t`` cycles, and
#: ``t <name>`` jumps straight to a tab; numbers keep working on every tab so
#: muscle memory never breaks.
_TABS: tuple[str, ...] = ("ALL",) + tuple(section for section, _entries in _MENU_SECTIONS)


def _tab_bar(active: str) -> Text:
    """The tab strip: every tab, with the active one marked and highlighted."""
    bar = Text()
    bar.append("  ")
    for index, tab in enumerate(_TABS):
        if index:
            bar.append("  ", style="dim")
        if tab == active:
            bar.append(f" {tab} ", style="bold white on #1f6feb")
        else:
            bar.append(f" {tab} ", style="dim")
    bar.append("   t = switch tab", style="dim")
    return bar


def _switch_tab(state: _DashboardState, target: str) -> str:
    """Set the active tab; returns a short outcome for the log.

    Accepts a tab name (any case, or a unique prefix like ``acc``) or ``""``
    to cycle to the next tab. Unknown names are reported, never fatal.
    """
    want = target.strip().upper()
    if not want:
        index = (_TABS.index(state.tab) + 1) % len(_TABS) if state.tab in _TABS else 0
        state.tab = _TABS[index]
        return f"tab {state.tab}"
    matches = [tab for tab in _TABS if tab.startswith(want)]
    if len(matches) == 1:
        state.tab = matches[0]
        return f"tab {state.tab}"
    if not matches:
        return f"unknown tab {target!r} (have: {', '.join(_TABS)})"
    return f"ambiguous tab {target!r} (matches: {', '.join(matches)})"


def _menu_sections_for(tab: str) -> tuple[tuple[str, tuple[tuple[str, str, str, str], ...]], ...]:
    """The sections a tab shows; ``ALL`` shows every section."""
    if tab == "ALL":
        return _MENU_SECTIONS
    return tuple((section, entries) for section, entries in _MENU_SECTIONS if section == tab)


def _menu_table(width: int | None = None, tab: str = "ALL") -> Table:
    """The menu for one tab, two entries per row.

    Built as one ``Text`` per row rather than a four-column grid: a grid keeps
    a section heading inside its first column, which indented the headings and
    left the rows looking ragged. Laying rows out by hand keeps the columns
    exact and lets the section rules span the panel. On a terminal too narrow
    for two columns the menu falls back to one entry per row.
    """
    inner = _rule_width() if width is None else width
    two_up = inner >= _MENU_TWO_UP_WIDTH
    table = Table.grid(padding=(0, 0))
    table.add_column(no_wrap=True)
    table.add_row(_tab_bar(tab))
    for section, entries in _menu_sections_for(tab):
        table.add_row(_menu_section(section, inner))
        for left_key, left_label, right_key, right_label in entries:
            if two_up:
                table.add_row(_menu_row(left_key, left_label, right_key, right_label))
            else:
                table.add_row(_menu_row(left_key, left_label, "", ""))
                if right_key:
                    table.add_row(_menu_row(right_key, right_label, "", ""))
    return table


def _log_block(state: _DashboardState) -> Table:
    """Session activity log: the last few actions, newest last."""
    table = Table.grid(padding=(0, 1))
    table.add_column(justify="left", style="dim", no_wrap=True)   # time
    table.add_column(justify="left", overflow="fold")             # message
    if not state.history:
        table.add_row(Text("LOG", style="dim"), Text("no actions yet this session", style="dim"))
        return table
    for index, (stamp, message) in enumerate(state.history):
        label = Text("LOG", style="dim") if index == 0 else Text("")
        table.add_row(label, Text(f"{stamp}  ", style="dim") + Text(message, style="yellow"))
    return table


def _uptime(state: _DashboardState) -> str:
    """How long this dashboard session has been open, compactly."""
    seconds = int(max(0.0, time.monotonic() - state.started_at))
    minutes, secs = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h{minutes:02d}m"
    if minutes:
        return f"{minutes}m{secs:02d}s"
    return f"{secs}s"


def _panel(state: _DashboardState) -> Panel:
    """Build the dashboard panel (split out so tests can render it directly)."""
    records = state.refresh()
    failed = sum(1 for r in records if r.status == "failed")
    banned = sum(1 for r in records if r.status == "banned")

    band = Table.grid(padding=(0, 3))
    band.add_column(justify="left", vertical="top")
    band.add_column(justify="left", vertical="top")
    band.add_row(_accounts_block(records), _today_block(state))

    body = Table.grid(padding=(0, 0))
    body.add_column()
    body.add_row(band)
    body.add_row(_health_block(state, records))
    body.add_row(_trend_block(state))
    body.add_row(Text("─" * _rule_width(), style="grey37"))
    body.add_row(_menu_table(_rule_width(), state.tab))
    body.add_row(Text("─" * _rule_width(), style="grey37"))
    body.add_row(_log_block(state))

    title = Text("CONSO MONITOR", style="bold #58a6ff")
    if failed or banned:
        status, colour = "ATTENTION", "red"
    elif records:
        status, colour = "READY", "green"
    else:
        status, colour = "EMPTY", "yellow"
    subtitle = Text(" ● ", style=f"bold {colour}")
    subtitle.append(status, style=f"bold {colour}")
    subtitle.append(
        f"  {datetime.now().strftime('%H:%M:%S')} · up {_uptime(state)}"
        " · number = run, t = tab, S = settings, Q = quit ",
        style="dim",
    )
    return Panel(body, title=title, subtitle=subtitle, border_style="#30363d", padding=(0, 1))


def _render(state: _DashboardState) -> None:
    """Repaint the whole panel so the user always sees fresh store state."""
    ui.console.print(_panel(state))


def render_plain(state: _DashboardState, *, width: int = 110) -> str:
    """Render the panel to plain text.

    The panel is built from Rich tables (columns must line up), and a table has
    no ``.plain`` of its own — so this renders it through an off-screen console.
    Tests assert on this, and it doubles as the non-colour fallback path.
    """
    import io

    from rich.console import Console

    console = Console(file=io.StringIO(), width=width, highlight=False)
    console.print(_panel(state))
    return console.file.getvalue()


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
def _target(email: str) -> str:
    """Map the prompt's ``all`` sentinel to the commands' empty-string = all."""
    email = email.strip()
    return "" if email.lower() in ("", "all") else email


def _resolve_account(records: list[AccountRecord], pick: str) -> str | None:
    """Resolve a menu pick (1-based number or email) to an email, or ``None``."""
    pick = pick.strip()
    if pick.isdigit():
        index = int(pick)
        if 1 <= index <= len(records):
            return records[index - 1].email
        return None
    return next((r.email for r in records if r.email.lower() == pick.lower()), None)


def _referral(state: _DashboardState) -> str:
    """Referral code, defaulting to the one ``.env`` applies to new accounts."""
    default = state.settings.default_referral_code
    return _ask(f"referral [{default or 'none'}]", default)


# ---------------------------------------------------------------------------
# Actions (each returns a "recent" outcome, or None for plain success)
# ---------------------------------------------------------------------------
def _act_daily_run(state: _DashboardState) -> str | None:
    """loop --once: the cron cycle, run from the menu."""
    force = _confirm("force accounts already done today? [y/N]", default=False)
    workers = _int_ask("workers [1]", 1)
    turns = _int_ask("turns per account [10]", 10)
    min_remaining = _float_ask("min-remaining zaps", -1.0, default_label="from .env")
    shown = "from .env" if min_remaining < 0 else f"{min_remaining:g}"
    if not _confirm(
        f"Run the daily loop once now? (turns={turns}, workers={workers}, "
        f"force={'yes' if force else 'no'}, min-remaining={shown}) [Y/n]",
        default=True,
    ):
        return "cancelled"
    _call(loop_cmd, turns=turns, once=True, force=force, workers=workers,
          min_remaining=min_remaining)
    return None


def _act_farm(state: _DashboardState) -> str | None:
    """farm: missions + synthetic turns, all accounts or one email."""
    turns = _int_ask("turns [10]", 10)
    email = _target(_ask("email [all]", "all"))
    dry_run = _confirm("dry-run (no network)? [y/N]", default=False)
    earn = _confirm("claim missions too (--earn)? [Y/n]", default=True)
    if not _confirm(f"Farm {turns} turn(s) for {email or 'ALL accounts'}"
                    f"{' (missions included)' if earn else ''}"
                    f"{' [DRY RUN, no network]' if dry_run else ''}? [Y/n]", default=True):
        return "cancelled"
    _call(farm_cmd, turns=turns, email=email, earn=earn, dry_run=dry_run)
    return None


def _act_missions(state: _DashboardState) -> str | None:
    """earn: claim daily/bonus missions and codes."""
    email = _target(_ask("email [all]", "all"))
    referral = _referral(state)
    missions = _ask("missions (empty = all, or comma ids) []", "")
    access = _ask("access code (optional) []", "")
    if not _confirm(f"Claim missions/codes for {email or 'ALL accounts'} "
                    f"(referral={referral or 'none'}, access={access or 'none'})? [Y/n]",
                    default=True):
        return "cancelled"
    _call(earn_cmd, email=email, referral=referral, access=access, missions=missions)
    return None


def _act_earn_one(state: _DashboardState) -> str | None:
    """earn --email: claim missions/codes for one account picked from a list."""
    records = state.refresh()
    if not records:
        ui.error("earn: no accounts in the store")
        return "no accounts"
    for index, record in enumerate(records, 1):
        ui.console.print(f"  {index:>3}) {record.email}  ({record.status})")
    pick = _ask("account number or email [1]", "1")
    target = _resolve_account(records, pick)
    if target is None:
        ui.error(f"earn: no account matches {pick!r}")
        return f"unknown account: {pick}"
    referral = _referral(state)
    if not _confirm(f"Claim missions/codes for {target} "
                    f"(referral={referral or 'none'})? [Y/n]", default=True):
        return "cancelled"
    _call(earn_cmd, email=target, referral=referral)
    return None


def _act_status(state: _DashboardState) -> str | None:
    """status: daily-limit / earning state straight from the backend."""
    email = _target(_ask("email [all]", "all"))
    _call(status_cmd, email=email)
    return None


def _act_sessions(state: _DashboardState) -> str | None:
    """session: show/refresh stored sessions without re-authenticating."""
    email = _target(_ask("email [all]", "all"))
    force = _confirm("force a refresh now? [y/N]", default=False)
    _call(session_cmd, email=email, force=force)
    return None


def _act_recover(state: _DashboardState) -> str | None:
    """recover: refresh -> password -> email OTP, per account."""
    email = _target(_ask("email [all]", "all"))
    if not _confirm(f"Recover sessions for {email or 'ALL accounts'} "
                    f"(refresh -> password -> OTP)? [Y/n]", default=True):
        return "cancelled"
    _call(recover_cmd, email=email)
    return None


def _act_register(state: _DashboardState) -> str | None:
    """register: provision new accounts, optionally farming them right after."""
    count = _int_ask("count [1]", 1)
    referral = _referral(state)
    earn_now = _confirm("earn now? [y/N]", default=False)
    turns = _int_ask("turns per account [10]", 10) if earn_now else 10
    dry_run = _confirm("dry-run (identities only, no network)? [y/N]", default=False)
    extra = f", earn now with {turns} turns" if earn_now else ""
    if not _confirm(f"Register {count} account(s) "
                    f"(referral={referral or 'none'}{extra}"
                    f"{', DRY RUN' if dry_run else ''})? [Y/n]", default=True):
        return "cancelled"
    _call(register_cmd, count=count, referral=referral, earn=earn_now,
          turns=turns, dry_run=dry_run)
    return None


def _act_doctor(state: _DashboardState) -> str | None:
    """doctor: dependency / provider readiness report."""
    _call(doctor_cmd)
    return None


def _act_test(state: _DashboardState) -> str | None:
    """test: transport, backend reachability, economy sanity."""
    platform = _ask("platform [claude]", "claude")
    model = _ask("model [claude-opus-4-8]", "claude-opus-4-8")
    _call(test_cmd, platform=platform, model=model)
    return None


def _act_solve(state: _DashboardState) -> str | None:
    """solve: one Turnstile challenge with the configured solver."""
    sitekey = _ask(f"sitekey [{TURNSTILE_SITEKEY}]", TURNSTILE_SITEKEY)
    url = _ask(f"url [{TURNSTILE_PAGE_URL}]", TURNSTILE_PAGE_URL)
    _call(solve_cmd, sitekey=sitekey, url=url)
    return None


def _act_proxies(state: _DashboardState) -> str | None:
    """proxies: health-check the solver's proxy list."""
    env_file = os.environ.get("SOLVER_PROXY_FILE", "").strip()
    path = _ask(f"proxy list file [{env_file or 'SOLVER_PROXY_FILE'}]", "")
    limit = _int_ask("limit (0 = all) [0]", 0)
    _call(proxies_cmd, file=path, limit=limit)
    return None


def _act_verify(state: _DashboardState) -> str | None:
    """verify: health-check the configured proxy pool (no prompts needed)."""
    _call(verify_cmd)
    return None


def _act_notify(state: _DashboardState) -> str | None:
    """notify: send one Telegram test message (network action)."""
    if not _confirm("Send a Telegram test message now? [Y/n]", default=True):
        return "cancelled"
    _call(notify_cmd)
    return None


def _act_export(state: _DashboardState) -> str | None:
    """export: dump the account store."""
    fmt = (_ask("fmt (csv|json) [csv]", "csv") or "csv").lower()
    if fmt not in ("csv", "json"):
        ui.error(f"export: unknown format {fmt!r} (use csv or json)")
        return f"bad format: {fmt}"
    _call(export_cmd, fmt=fmt)
    return None


def _act_report(state: _DashboardState) -> str | None:
    """report: one-shot status table, optionally refreshed from the backend."""
    fmt = (_ask("fmt (table|json|csv) [table]", "table") or "table").lower()
    if fmt not in ("table", "json", "csv"):
        ui.error(f"report: unknown format {fmt!r} (use table, json or csv)")
        return f"bad format: {fmt}"
    refresh = _confirm("poll the backend first? [y/N]", default=False)
    _call(report_cmd, fmt=fmt, refresh=refresh)
    return None


def _act_pipeline(state: _DashboardState) -> str | None:
    """pipeline: register -> earn -> loop in one orchestrator run.

    The most destructive entry on the menu, so it asks for the flags that
    change behaviour and then names every one of them in the confirmation
    before anything touches the network.
    """
    register = _int_ask("register count (0 = skip) [0]", 0)
    earn_now = _confirm("earn for the new accounts? [y/N]", default=False)
    loop_after = _confirm("run the recurring daily loop after? [y/N]", default=False)
    turns = _int_ask("turns per account [10]", 10)
    workers = _int_ask("workers [1]", 1)
    interval_hours = _float_ask("interval hours", 24.0)
    cycles = _int_ask("loop cycles (0 = forever) [0]", 0)
    referral = _referral(state)
    daily_cap = _float_ask("daily cap zaps (0 = off)", 0.0)
    only_new = _confirm("earn only accounts registered this run? [y/N]", default=False)
    solver_concurrent = _int_ask("solver concurrency (0 = from .env) [0]", 0)
    plan = (f"register={register}, earn={'yes' if earn_now else 'no'}, "
            f"loop={'yes' if loop_after else 'no'}, turns={turns}, workers={workers}, "
            f"interval={interval_hours:g}h, cycles={cycles if cycles else 'forever'}, "
            f"referral={referral or 'none'}, daily-cap={daily_cap:g}, "
            f"only-new={'yes' if only_new else 'no'}, solver-concurrent={solver_concurrent}")
    if not _confirm(f"Run the full pipeline now? ({plan}) [Y/n]", default=True):
        return "cancelled"
    _call(pipeline_cmd, register=register, earn=earn_now, loop=loop_after,
          turns=turns, interval_hours=interval_hours, cycles=cycles,
          referral=referral, workers=workers, daily_cap=daily_cap,
          only_new=only_new, solver_concurrent=solver_concurrent)
    return None


def _act_summary(state: _DashboardState) -> str | None:
    """Local settings summary — no network, no command call."""
    records = state.refresh()
    settings = state.settings
    active = sum(1 for r in records if r.status == "active")
    ui.summary("SETTINGS", [
        ("store", state.store.json_path),
        ("accounts", f"{len(records)} ({active} active)"),
        ("verifier", settings.verifier),
        ("solver", ",".join(_solver_chain())),
        ("proxy", f"{len(settings.proxy.urls)} configured" if settings.proxy.urls else "direct"),
        ("referral", settings.default_referral_code or "none"),
        ("concurrency", f"{settings.concurrency} (max {settings.max_concurrency})"),
        ("pacing", f"{settings.min_delay_seconds:g}-{settings.max_delay_seconds:g}s per turn"),
        ("telegram", "configured" if is_configured() else "off"),
    ])
    return None


#: choice -> (label for the recent line, handler). ``q`` is handled by the loop
#: itself so it can return the exit code; ``s`` is the only letter shortcut.
_MENU: tuple[tuple[str, str, Callable[[_DashboardState], str | None]], ...] = (
    ("1", "daily run", _act_daily_run),
    ("2", "farm", _act_farm),
    ("3", "missions", _act_missions),
    ("4", "earn one", _act_earn_one),
    ("5", "status", _act_status),
    ("6", "sessions", _act_sessions),
    ("7", "recover", _act_recover),
    ("8", "register", _act_register),
    ("9", "doctor", _act_doctor),
    ("10", "test", _act_test),
    ("11", "solve", _act_solve),
    ("12", "proxies", _act_proxies),
    ("13", "export", _act_export),
    ("14", "report", _act_report),
    ("15", "verify", _act_verify),
    ("16", "notify", _act_notify),
    ("17", "pipeline", _act_pipeline),
    ("s", "summary", _act_summary),
)
_MENU_BY_CHOICE = {choice: (label, handler) for choice, label, handler in _MENU}


# ---------------------------------------------------------------------------
# Action guard + loop
# ---------------------------------------------------------------------------
def _restore_sigint(previous: Any) -> None:
    """Put back the SIGINT handler the shell expects after an action.

    WHY: ``loop``/``pipeline`` install a handler that only stops their runner.
    Leaving it installed would turn Ctrl+C at the menu into a no-op, so the
    pre-action handler is restored once the command returns. ``signal.signal``
    refuses to run off the main thread; the dashboard ignores that instead of
    failing the action.
    """
    try:
        if signal.getsignal(signal.SIGINT) is not previous:
            signal.signal(signal.SIGINT, previous)
    except (ValueError, TypeError):
        pass


def _guard(state: _DashboardState, label: str, work: Callable[[], str | None]) -> None:
    """Run one action body and absorb every failure mode.

    The body includes the prompts, so Ctrl+C in the middle of a question also
    lands here and returns to the menu. ``typer.Exit`` is an expected dead end
    (e.g. "no accounts"), ``ConsoAPIError`` is a backend failure; neither may
    take the dashboard down. ``stop_monitor`` runs defensively because a
    command that dies between its own start/stop would otherwise leave the
    live panel running over the menu.
    """
    previous = signal.getsignal(signal.SIGINT)
    try:
        outcome = work()
        _set_result(state, label, outcome or "done")
    except KeyboardInterrupt:
        _set_result(state, label, "interrupted (Ctrl+C)")
        ui.log("dashboard: interrupted — back to the menu", level="warn")
    except typer.Exit as exc:
        code = int(getattr(exc, "exit_code", 1) or 0)
        _set_result(state, label, f"exited with code {code}")
        if code:
            ui.error(f"{label}: command exited with code {code}")
    except SystemExit as exc:
        # Same contract as typer.Exit: a command calling sys.exit() is an
        # expected dead end, not a reason to take the dashboard down.
        code = int(exc.code or 0) if not isinstance(exc.code, str) else 1
        _set_result(state, label, f"exited with code {code}")
        if code:
            ui.error(f"{label}: command exited with code {code}")
    except ConsoAPIError as exc:
        _set_result(state, label, f"failed: {exc}")
        ui.error(f"{label}: {exc}")
    except Exception as exc:  # noqa: BLE001 - a bug here must not kill the loop
        _set_result(state, label, f"error: {exc!r}")
        ui.error(f"{label}: unexpected error: {exc!r}")
    finally:
        stop_monitor("DONE")
        _restore_sigint(previous)


def run_dashboard() -> int:
    """Interactive dashboard entry point; returns the process exit code.

    Runs until the user quits (``q``), presses Ctrl+C at the menu, or stdin
    reaches EOF. Always 0: a session ending is not an error.
    """
    settings = Settings.from_env()
    state = _DashboardState(settings=settings, store=Store(settings.data_dir))
    while True:
        try:
            _render(state)
        except KeyboardInterrupt:
            # Ctrl+C landing exactly on a repaint is still "quit", not a crash:
            # KeyboardInterrupt is a BaseException and would otherwise escape
            # as exit code 130.
            ui.log("dashboard: bye")
            return 0
        except Exception as exc:  # noqa: BLE001 - a bad panel must not end the session
            ui.error(f"dashboard: cannot render the panel: {exc!r}")
        try:
            choice = _choose("select").strip().lower()
        except (KeyboardInterrupt, EOFError):
            ui.log("dashboard: bye")
            return 0
        if choice in ("q", "quit", "exit"):
            ui.log("dashboard: bye")
            return 0
        if not choice:
            continue
        if choice == "t" or choice.startswith("t "):
            _set_result(state, "menu", _switch_tab(state, choice[1:]))
            continue
        entry = _MENU_BY_CHOICE.get(choice)
        if entry is None:
            _set_result(state, "menu", f"unknown choice: {choice!r}")
            ui.log(f"dashboard: unknown choice {choice!r}", level="warn")
            continue
        label, handler = entry
        _guard(state, label, lambda handler=handler: handler(state))
