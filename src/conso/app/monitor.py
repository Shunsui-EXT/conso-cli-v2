"""Single-screen live monitor for the headless CLI.

A minimal, readable replacement for the old Textual app: one Rich `Live`
render that refreshes a single panel in place. There are no screens, no
keybindings and no event loop to drive — the caller keeps running its normal
pipeline and only feeds events in.

Layout (top to bottom):

    +--------------------------------------------------+
    |  ASCII "CONSO" header  + tagline   [meta line]   |
    |  status dot + phase + elapsed + rate             |
    |  progress bar  3/8  37%   eta 04:12              |
    |  STATS   zaps 12.4  rate 2.1/m  ok 2  fail 1     |
    |  log tail (last 6 lines, coloured)               |
    +--------------------------------------------------+

Usage:

    with Monitor("register"):
        ...                      # events arrive via emit()

The ops layer no longer knows this module exists: it publishes progress on the
process-wide event bus (``conso.ops.events``) and :func:`start_monitor`
subscribes the monitor to it. The subscription is torn down by
:func:`stop_monitor`.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Callable

from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.text import Text

# Compact ASCII banner. Sized to survive an 80-column terminal.
LOGO = (
    " ██████╗ ██████╗ ███╗   ██╗███████╗ ██████╗",
    "██╔════╝██╔═══██╗████╗  ██║██╔════╝██╔═══██╗",
    "██║     ██║   ██║██╔██╗ ██║███████╗██║   ██║",
    "██║     ██║   ██║██║╚██╗██║╚════██║██║   ██║",
    "╚██████╗╚██████╔╝██║ ╚████║███████║╚██████╔╝",
    " ╚═════╝ ╚═════╝ ╚═╝  ╚═══╝╚══════╝ ╚═════╝",
)
TAGLINE = "conso automation pipeline"

# Status -> (dot, style)
STATUS_STYLE = {
    "IDLE": ("*", "dim"),
    "RUNNING": ("*", "green"),
    "DONE": ("*", "green"),
    "STOPPED": ("*", "yellow"),
    "FAILED": ("*", "red"),
}

_DOT = {"ok": "+", "fail": "-", "warn": "!", "info": "."}
_LEVEL_STYLE = {"ok": "green", "fail": "red", "warn": "yellow", "info": "cyan"}
_TAIL = 6


@dataclass
class MonitorState:
    """Everything the panel renders. Mutated only under the monitor lock."""

    mode: str = "run"
    status: str = "RUNNING"
    phase: str = "starting"
    target: int = 0
    done: int = 0
    ok: int = 0
    failed: int = 0
    zaps: float = 0.0
    started_at: float = field(default_factory=time.time)
    logs: list[tuple[str, str]] = field(default_factory=list)  # (level, message)

    @property
    def elapsed(self) -> float:
        return max(0.0, time.time() - self.started_at)

    @property
    def rate_per_min(self) -> float:
        minutes = self.elapsed / 60.0
        return (self.zaps / minutes) if minutes > 0.05 else 0.0

    @property
    def eta_seconds(self) -> float:
        """ETA from completed-item throughput, not zaps."""
        if self.done <= 0 or self.target <= 0:
            return 0.0
        remaining = max(0, self.target - self.done)
        per_item = self.elapsed / self.done
        return remaining * per_item


def _compact(seconds: float) -> str:
    seconds = int(max(0, seconds))
    minutes, secs = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h{minutes:02d}m"
    return f"{minutes:02d}:{secs:02d}"


class Monitor:
    """Live single-panel monitor.

    Safe to use from worker threads: every mutation and render takes the lock.
    If stdout is not a terminal, rendering degrades to nothing and only the
    plain log lines are printed by the caller.
    """

    def __init__(self, mode: str = "run", *, console: Console | None = None,
                 target: int = 0) -> None:
        self.state = MonitorState(mode=mode, target=target)
        self._console = console or Console(highlight=False)
        self._lock = threading.RLock()
        self._live: Live | None = None

    # -- context manager ---------------------------------------------------
    def __enter__(self) -> "Monitor":
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()

    def start(self) -> None:
        with self._lock:
            if self._live is not None:
                return
            self.state.started_at = time.time()
            if not self._console.is_terminal:
                return  # no TTY -> caller's plain logs are enough
            self._live = Live(
                self._render(), console=self._console, refresh_per_second=6,
                transient=False, screen=False, vertical_overflow="visible",
            )
            self._live.start()

    def stop(self, status: str = "DONE") -> None:
        with self._lock:
            self.state.status = status
            if self._live is not None:
                self._live.update(self._render())
                self._live.stop()
                self._live = None

    # -- mutation ----------------------------------------------------------
    def emit(self, event: str, **data: object) -> None:
        """Fold one pipeline event into the panel state."""
        event = (event or "").upper()
        with self._lock:
            st = self.state
            if event == "BATCH_STARTED":
                st.status = "RUNNING"
                st.phase = "registering"
                target = data.get("target")
                if isinstance(target, int) and target > 0:
                    st.target = target
            elif event == "BATCH_COMPLETED":
                st.status = str(data.get("status") or "COMPLETED").upper() or "DONE"
                st.phase = "done"
            elif event == "ACCOUNT_QUEUED":
                target = data.get("target")
                if isinstance(target, int) and target > 0:
                    st.target = target
            elif event == "ACCOUNT_STAGE":
                st.phase = str(data.get("stage") or st.phase)
            elif event == "ACCOUNT_COMPLETED":
                st.done += 1
                st.ok += 1
                zaps = data.get("zaps")
                if isinstance(zaps, (int, float)):
                    st.zaps += float(zaps)
                if st.status == "IDLE":
                    st.status = "RUNNING"
            elif event == "ACCOUNT_FAILED":
                st.done += 1
                st.failed += 1
                self._push("fail", f"{data.get('email', '')} {data.get('reason', '')}")
            elif event == "TURN_CREDITED":
                zaps = data.get("zaps")
                if isinstance(zaps, (int, float)):
                    st.zaps += float(zaps)
            elif event == "LOG":
                self._push("info", str(data.get("message") or ""))
            if self._live is not None:
                self._live.update(self._render())

    def log(self, message: str, level: str = "info") -> None:
        """Append one line to the log tail."""
        with self._lock:
            self._push(level, message)
            if self._live is not None:
                self._live.update(self._render())

    def _push(self, level: str, message: str) -> None:
        message = (message or "").strip()
        if not message:
            return
        self.state.logs.append((level, message))
        if len(self.state.logs) > _TAIL:
            del self.state.logs[:-_TAIL]

    # -- rendering ---------------------------------------------------------
    def _render(self) -> Panel:
        st = self.state
        text = Text()

        # Logo + tagline + mode on the far right of the first logo rows.
        for i, line in enumerate(LOGO):
            text.append(line + "  ", style="bold #58a6ff")
            if i == 1:
                text.append(TAGLINE, style="dim")
            if i == 2:
                text.append(f"mode: {st.mode}", style="cyan")
            if i == 3:
                dot, style = STATUS_STYLE.get(st.status, ("*", "white"))
                text.append(f"{dot} {st.status}", style=f"bold {style}")
            text.append("\n")

        # Status line: phase + elapsed + rate.
        text.append("  phase ", style="dim")
        text.append(f"{st.phase}", style="white")
        text.append("   elapsed ", style="dim")
        text.append(f"{_compact(st.elapsed)}", style="cyan")
        text.append("   zaps ", style="dim")
        text.append(f"{st.zaps:.1f}", style="green")
        text.append("   rate ", style="dim")
        text.append(f"{st.rate_per_min:.1f}/min", style="green")
        text.append("\n")

        # Progress bar.
        pct = (st.done / st.target) if st.target > 0 else 0.0
        width = 28
        filled = int(round(pct * width))
        text.append("  [", style="dim")
        text.append("#" * filled, style="green")
        text.append("." * (width - filled), style="dim")
        text.append("] ", style="dim")
        text.append(f"{st.done}/{st.target or 0}", style="white")
        text.append(f"  {pct * 100:.0f}%", style="cyan")
        if st.target > 0 and st.done > 0 and st.status == "RUNNING":
            text.append(f"  eta {_compact(st.eta_seconds)}", style="yellow")
        text.append("\n")

        # Stats row.
        for label, value, style in (
            ("ok", str(st.ok), "green"),
            ("fail", str(st.failed), "red" if st.failed else "dim"),
            ("accounts", str(st.target or st.done), "cyan"),
        ):
            text.append(f"  {label} ", style="dim")
            text.append(f"{value}", style=style)
        text.append("\n")

        # Log tail.
        if st.logs:
            text.append("\n")
            for level, message in st.logs[-_TAIL:]:
                text.append(f"  {_DOT.get(level, '.')} ", style=_LEVEL_STYLE.get(level, "white"))
                text.append(message[:96], style="white")
                text.append("\n")

        return Panel(text, border_style="#30363d", padding=(0, 1))


# ---------------------------------------------------------------------------
# Process-wide singleton so nested modules can share one monitor
# ---------------------------------------------------------------------------
_current: Monitor | None = None
_current_lock = threading.Lock()
_unsubscribe: Callable[[], None] | None = None


def start_monitor(mode: str = "run", *, target: int = 0) -> Monitor:
    """Start (or reuse) the process-wide monitor.

    The monitor subscribes itself to the ops event bus, so the pipeline can
    report progress without importing this module. Repeated calls reuse the
    running monitor and its single subscription.
    """
    global _current, _unsubscribe
    with _current_lock:
        if _current is None:
            from ..ops.events import subscribe

            mon = Monitor(mode, target=target)
            mon.start()
            _unsubscribe = subscribe(lambda name, data: mon.emit(name, **data))
            _current = mon
        return _current


def get_monitor() -> Monitor | None:
    return _current


def stop_monitor(status: str = "DONE") -> None:
    global _current, _unsubscribe
    with _current_lock:
        if _unsubscribe is not None:
            try:
                _unsubscribe()
            except Exception:  # noqa: BLE001 - teardown must never raise
                pass
            _unsubscribe = None
        if _current is not None:
            _current.stop(status=status)
            _current = None
