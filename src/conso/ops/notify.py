"""Optional Telegram delivery for run summaries.

Configure in `.env`:

    TELEGRAM_BOT_TOKEN=123456:ABC-...
    TELEGRAM_CHAT_ID=-1001234567890

When both are set, `register`, `pipeline` and `loop` send a formatted summary
after the run. Nothing is sent when either is missing, and a delivery failure
is logged and swallowed — a notification problem must never fail a run.

Rendering:
  - **HTML** by default (Telegram parses `<b>`, `<code>`, `<pre>`), which gives
    aligned columns and monospace figures without emoji.
  - falls back to plain text when the API rejects the markup, so a bad tag can
    never lose the report.

This module is transport-only: it never reads the account store, so no tokens
or emails end up in the message.
"""

from __future__ import annotations

import os
import urllib.parse
import urllib.request
from typing import Any, Iterable

def _load_env() -> None:
    """Pull .env into os.environ when python-dotenv is available.

    The CLI already loads settings, but this keeps the module usable from a
    standalone script (or a cron one-liner) without the caller remembering to
    bootstrap the environment first.
    """
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv(override=False)


_load_env()

API = "https://api.telegram.org"
TIMEOUT = 15
# Telegram rejects messages longer than this.
MAX_CHARS = 4000

BAR_WIDTH = 20


class NotifierError(RuntimeError):
    """Raised when a message cannot be delivered."""


def is_configured() -> bool:
    return bool(os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
                and os.environ.get("TELEGRAM_CHAT_ID", "").strip())


def send(text: str, *, parse_mode: str = "") -> bool:
    """POST one message. Returns True on success, False if not configured.

    Raises NotifierError on a real transport/API failure so callers can decide
    whether to care (the CLI swallows it).
    """
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        return False

    payload: dict[str, Any] = {"chat_id": chat_id, "text": text[:MAX_CHARS]}
    if parse_mode:
        payload["parse_mode"] = parse_mode

    data = urllib.parse.urlencode(payload).encode("utf-8")
    req = urllib.request.Request(
        f"{API}/bot{token}/sendMessage", data=data, method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return resp.status == 200
    except Exception as exc:  # noqa: BLE001 - caller decides
        raise NotifierError(f"telegram send failed: {exc}") from exc


# -- rendering -------------------------------------------------------------
def _bar(ratio: float, width: int = BAR_WIDTH) -> str:
    """Block progress bar.

    Uses box-drawing glyphs rather than emoji: they render identically in every
    Telegram client and never get replaced by a colour glyph.
    """
    ratio = 0.0 if ratio < 0 else (1.0 if ratio > 1 else ratio)
    filled = int(round(ratio * width))
    return "[" + "█" * filled + "░" * (width - filled) + "]"


# Typographic icons (no emoji). Matched on the row label.
_OK_ICONS = ("active", "ok", "zaps", "earned", "registered", "accounts", "solved")
_WARN_ICONS = ("skipped", "capped", "pending", "retry", "warn")
_BAD_ICONS = ("failed", "banned", "error", "dead")
_ICON_OK, _ICON_WARN, _ICON_BAD, _ICON_NEUTRAL = "●", "◐", "✕", "○"
_ICON_ZAPS, _ICON_ACCOUNT = "◆", "◉"


def _as_number(value: Any) -> float:
    """Numeric value of a row; handles "4/4" style strings (uses the left side)."""
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if "/" in text:
        text = text.split("/", 1)[0]
    try:
        return float(text)
    except (TypeError, ValueError):
        return 0.0


def _icon_for(label: str, value: Any) -> str:
    """Pick a glyph for a row: shape encodes state, not decoration.

    A zero is never alarming and never an achievement, so it always renders as
    the neutral ring regardless of the label — otherwise "failed 0" would show
    the same red cross as "failed 9".
    """
    if _as_number(value) == 0:
        return _ICON_NEUTRAL
    low = str(label).lower()
    if any(k in low for k in _BAD_ICONS):
        return _ICON_BAD
    if any(k in low for k in _WARN_ICONS):
        return _ICON_WARN
    if "zap" in low:
        return _ICON_ZAPS
    if "account" in low:
        return _ICON_ACCOUNT
    if any(k in low for k in _OK_ICONS):
        return _ICON_OK
    return _ICON_NEUTRAL


def _split_row(row: Any) -> tuple[str, str, Any]:
    """Accept (label, value) or (icon, label, value)."""
    if len(row) == 3:
        icon, label, value = row
        return str(icon), str(label), value
    label, value = row
    return _icon_for(label, value), str(label), value


def _esc(value: str) -> str:
    """Escape HTML for Telegram's parser."""
    return (str(value).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def _fmt_num(value: Any) -> str:
    if isinstance(value, float):
        return f"{value:,.2f}"
    if isinstance(value, int):
        return f"{value:,}"
    return str(value)


def render_html(title: str, rows: Iterable[tuple[str, Any]], *, footer: str = "",
                progress: tuple[str, float] | None = None) -> str:
    """Build an HTML summary.

    `rows` is (label, value). A `progress` pair of (label, 0..1 ratio) renders
    an ASCII bar under the header.
    """
    pairs = [_split_row(r) for r in rows]
    body = [f"▶ <b>{_esc(title)}</b>", "━" * 22]
    if progress:
        label, ratio = progress
        body.append("")
        body.append(f"<code>{_bar(ratio)}</code> {ratio * 100:5.1f}%  <i>{_esc(label)}</i>")

    if pairs:
        width = max(len(_esc(lbl)) for _, lbl, _ in pairs)
        body.append("")
        for icon, label, value in pairs:
            body.append(
                f"{icon} {_esc(label).ljust(width)}  <code>{_esc(_fmt_num(value))}</code>"
            )
    if footer:
        body += ["", "━" * 22, f"<i>{_esc(footer)}</i>"]
    return "\n".join(body)


def render_text(title: str, rows: Iterable[tuple[str, Any]], *, footer: str = "",
                progress: tuple[str, float] | None = None) -> str:
    """Plain-text fallback (used when HTML is rejected)."""
    pairs = [_split_row(r) for r in rows]
    body = [f"▶ {title}", "━" * 22]
    if progress:
        label, ratio = progress
        body += ["", f"{_bar(ratio)} {ratio * 100:5.1f}%  {label}"]
    if pairs:
        width = max(len(lbl) for _, lbl, _ in pairs)
        body.append("")
        for icon, label, value in pairs:
            body.append(f"{icon} {label.ljust(width)}  {_fmt_num(value)}")
    if footer:
        body += ["", "━" * 22, footer]
    return "\n".join(body)


def summary(title: str, rows: Iterable[tuple[str, Any]], *, footer: str = "",
            progress: tuple[str, float] | None = None) -> str:
    """Render the default (HTML) summary."""
    return render_html(title, rows, footer=footer, progress=progress)


def notify(title: str, rows: Iterable[tuple[str, Any]], *, footer: str = "",
           progress: tuple[str, float] | None = None, logger=None) -> bool:
    """Send a summary if Telegram is configured. Never raises.

    Tries HTML first and retries as plain text if Telegram rejects the markup,
    so a bad tag degrades the look but never loses the report.
    """
    if not is_configured():
        return False
    html = render_html(title, rows, footer=footer, progress=progress)
    try:
        if send(html, parse_mode="HTML"):
            if logger:
                logger("notify: telegram summary sent")
            return True
    except NotifierError as exc:
        if logger:
            logger(f"notify: html send failed ({exc}), retrying as plain text")
    try:
        if send(render_text(title, rows, footer=footer, progress=progress)):
            if logger:
                logger("notify: telegram summary sent (plain text)")
            return True
    except NotifierError as exc:
        if logger:
            logger(f"notify: {exc}")
    return False
