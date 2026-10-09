"""
Terminal UI helpers for the headless CLI.

Keeps script output readable: a compact startup banner, coloured single-line
logs, a live progress line, and a summary panel at the end. All rendering goes
through Rich; when stdout is not a TTY it degrades to plain text automatically.

Two helpers exist for the command layer specifically:

- :func:`log_auto` — one-line log whose level is inferred from the message
  (this is what the commands call; it replaces the old ``cli._log``).
- :func:`mirror` — wraps a logger so every line ALSO lands in the live
  monitor's log tail (replaces the old ``cli._mirror``).
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from types import TracebackType
from typing import Any, Callable

from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

console = Console(highlight=False)
_err = Console(stderr=True, highlight=False)

#: Serialises whole log lines so concurrent workers never interleave output.
print_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Startup banner
# ---------------------------------------------------------------------------
def banner(mode: str, *, solver: str = "", verifier: str = "",
           proxy: str = "", workers: int = 0, extra: dict[str, str] | None = None) -> None:
    """Print a compact branded banner before a run starts."""
    title = Text("CONSO FARM", style="bold #58a6ff")
    title.append(f"  ·  {mode}", style="bold white")
    meta = Text()
    for label, value in [("solver", solver), ("verifier", verifier), ("proxy", proxy),
                         ("workers", str(workers) if workers else "")]:
        if value:
            meta.append(f"{label} ", style="dim")
            meta.append(f"{value}  ", style="cyan")
    for label, value in (extra or {}).items():
        if value:
            meta.append(f"{label} ", style="dim")
            meta.append(f"{value}  ", style="magenta")
    console.print(Panel(Text.assemble(title, "\n", meta), border_style="#30363d", padding=(0, 1)))


# ---------------------------------------------------------------------------
# Coloured single-line logs
# ---------------------------------------------------------------------------
_LEVEL_COLOR = {
    "ok": "green", "done": "green", "active": "green", "claimed": "green",
    "warn": "yellow", "note": "yellow", "skip": "dim", "skipped": "dim",
    "err": "red", "fail": "red", "failed": "red", "banned": "red",
    "info": "cyan",
}


def log(message: str, *, level: str = "") -> None:
    """One-line coloured log: ``HH:MM:SS  message``."""
    stamp = time.strftime("%H:%M:%S")
    color = _LEVEL_COLOR.get(level, "")
    if color:
        console.print(f"[dim]{stamp}[/dim]  [{color}]{message}[/{color}]")
    else:
        console.print(f"[dim]{stamp}[/dim]  {message}")


def classify(message: str) -> str:
    """Best-effort level from a pipeline log line."""
    low = message.lower()
    for key in ("account_banned", "rejected", "failed", " error", "err:", "timeout"):
        if key in low:
            return "err"
    if "[active]" in low or "earned" in low or "credited" in low or "done" in low:
        return "ok"
    if "referral" in low or "consoname" in low or "captcha solved" in low:
        return "info"
    if "note:" in low or "skip" in low:
        return "warn"
    return ""


def log_auto(message: str) -> None:
    """Log a line, inferring its level from the message.

    Takes the module-level print lock so a thread pool's output stays one line
    per call. This is the logger the command modules hand to the ops layer.
    """
    with print_lock:
        log(message, level=classify(message))


def mirror(logger: Callable[[str], None]) -> Callable[[str], None]:
    """Wrap a logger so lines also land in the live monitor's log tail."""
    def _fn(message: str) -> None:
        logger(message)
        from .monitor import get_monitor

        mon = get_monitor()
        if mon is not None:
            mon.log(message, level=classify(message))
    return _fn


# ---------------------------------------------------------------------------
# Live progress line (single row, updated in place)
# ---------------------------------------------------------------------------
@dataclass
class Progress:
    """Thread-safe single-line progress counter rendered with Live."""

    label: str = "working"
    total: int = 0
    done: int = 0
    failed: int = 0
    started: float = field(default_factory=time.monotonic)
    _live: Live | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def __enter__(self) -> "Progress":
        if console.is_terminal:
            self._live = Live(self._render(), console=console, refresh_per_second=8,
                              transient=True)
            self._live.__enter__()
        return self

    def __exit__(self, exc_type: type[BaseException] | None,
                 exc_val: BaseException | None, exc_tb: TracebackType | None) -> None:
        if self._live is not None:
            self._live.update(self._render(final=True))
            time.sleep(0.05)
            self._live.__exit__(exc_type, exc_val, exc_tb)

    def _render(self, *, final: bool = False) -> Text:
        with self._lock:
            done, failed, total = self.done, self.failed, self.total
        elapsed = time.monotonic() - self.started
        rate = (done / elapsed * 60.0) if elapsed > 0 and done else 0.0
        t = Text()
        t.append("● ", style="bold green" if not final else "bold cyan")
        t.append(f"{self.label}  ", style="bold")
        t.append(f"{done}", style="green")
        if total:
            t.append(f"/{total}", style="dim")
        t.append(f"  failed {failed}  ", style="red" if failed else "dim")
        t.append(f"{elapsed:.0f}s  ", style="white")
        t.append(f"{rate:.1f}/min", style="cyan")
        return t

    def inc(self, *, ok: bool = True) -> None:
        with self._lock:
            if ok:
                self.done += 1
            else:
                self.failed += 1
        if self._live is not None:
            self._live.update(self._render())


# ---------------------------------------------------------------------------
# Summary panel
# ---------------------------------------------------------------------------
def summary(title: str, rows: list[tuple[str, Any]], *, color: str = "#58a6ff") -> None:
    """Print a key/value summary panel at the end of a run."""
    body = Text()
    for i, (k, v) in enumerate(rows):
        if i:
            body.append("\n")
        body.append(f"{k:<14}", style="dim")
        body.append(str(v), style="bold white")
    console.print(Panel(body, title=Text(title, style=f"bold {color}"),
                        border_style=color, padding=(0, 1)))


def accounts_table(rows: list[dict], *, title: str = "ACCOUNTS") -> None:
    """Compact coloured table for `report`."""
    table = Table(title=title, border_style="#30363d", header_style="bold #58a6ff")
    for col in ("STATUS", "ACCOUNT", "TOTAL", "TODAY", "STREAK", "BOOST"):
        table.add_column(col, justify="right" if col not in ("STATUS", "ACCOUNT") else "left")
    for r in rows:
        status = r["status"]
        scolor = {"active": "green", "banned": "red", "failed": "red"}.get(status, "yellow")
        table.add_row(
            Text(status, style=scolor), r["email"],
            f"{r['total_zaps']:.1f}", f"{r['today']:.1f}",
            str(r["streak"]), f"{r['boost']:.2f}",
        )
    console.print(table)


def error(message: str) -> None:
    _err.print(f"[bold red]error:[/bold red] {message}")
