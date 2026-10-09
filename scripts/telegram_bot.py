#!/usr/bin/env python3
"""Telegram command bot for monitoring a Conso deployment.

Run it where you want to watch the pipeline (locally or on the VPS):

    python scripts/telegram_bot.py

It long-polls the Bot API and answers commands. Only the chat id in
TELEGRAM_CHAT_ID is served; every other sender gets a refusal, so an unknown
person finding the bot learns nothing.

Commands:
    /status    quick counts from the local store (no network)
    /report    live numbers polled from the Conso backend
    /solver    captcha provider, browser readiness, proxy pool size
    /cron      cron entry and the tail of logs/daily.log
    /help      list of commands

The command list is registered with setMyCommands so Telegram shows it as a
menu next to the input field.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import httpx  # noqa: E402

from conso.ops.notify import is_configured, render_html  # noqa: E402

API = "https://api.telegram.org"
POLL_TIMEOUT = 25
COMMANDS = [
    ("status", "quick counts from the local store"),
    ("report", "live numbers from the Conso backend"),
    ("solver", "captcha provider, browser, proxy pool"),
    ("cron", "cron entry and daily log tail"),
    ("help", "list of commands"),
]


def _token() -> str:
    return os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()


def _owner() -> str:
    return os.environ.get("TELEGRAM_CHAT_ID", "").strip()


def api(method: str, **params) -> dict:
    """One Bot API call. Raises on transport failure; caller handles."""
    with httpx.Client(timeout=POLL_TIMEOUT + 15) as client:
        resp = client.post(f"{API}/bot{_token()}/{method}", data=params)
        resp.raise_for_status()
        return resp.json()


def send(chat_id: str, text: str) -> None:
    try:
        api("sendMessage", chat_id=chat_id, text=text[:4000], parse_mode="HTML")
    except Exception:
        # HTML can be rejected if a tag is malformed; plain text still lands.
        api("sendMessage", chat_id=chat_id, text=text[:4000])


def register_menu() -> None:
    """Publish the command list so Telegram renders it as a menu."""
    import json

    try:
        api("setMyCommands", commands=json.dumps(
            [{"command": c, "description": d} for c, d in COMMANDS]))
    except Exception as exc:  # noqa: BLE001
        print(f"menu registration failed: {exc}")


# -- command handlers -----------------------------------------------------
def cmd_status() -> str:
    from conso.core.config import Settings
    from conso.store.storage import Store

    settings = Settings.from_env()
    accounts = Store(settings.data_dir).all()
    active = [a for a in accounts if a.status == "active"]
    banned = sum(1 for a in accounts if getattr(a, "banned", False))
    return render_html(
        "STATUS  ·  conso",
        [("accounts", len(accounts)), ("active", len(active)), ("banned", banned),
         ("inactive", len(accounts) - len(active) - banned)],
        progress=("active share", len(active) / max(1, len(accounts))),
        footer=f"host: {os.uname().nodename} · local store",
    )


def cmd_report() -> str:
    from conso.auth.session import SessionManager
    from conso.core.config import Settings
    from conso.net.client import ConsoClient
    from conso.ops.limits import get_daily_status
    from conso.store.storage import Store

    settings = Settings.from_env()
    store = Store(settings.data_dir)
    manager = SessionManager(settings, store)

    def one(record):
        try:
            result = manager.ensure_session(record)
            client = ConsoClient(settings)
            client.session = result.session
            status = get_daily_status(client)
            client.close()
            return (float(status.total_zaps) if status else 0.0,
                    float(status.effective_daily) if status else 0.0,
                    bool(status.is_banned) if status else False)
        except Exception:  # noqa: BLE001
            return (0.0, 0.0, False)

    records = [a for a in store.all() if a.status == "active"]
    with ThreadPoolExecutor(max_workers=4) as pool:
        rows = list(pool.map(one, records))

    total = sum(r[0] for r in rows)
    daily = sum(r[1] for r in rows)
    banned = sum(1 for r in rows if r[2])
    return render_html(
        "REPORT  ·  conso",
        [("total zaps", total), ("today", daily), ("accounts", len(records)),
         ("banned", banned)],
        progress=("daily budget", min(1.0, daily / (21.0 * max(1, len(records))))),
        footer=f"host: {os.uname().nodename} · live",
    )


def cmd_solver() -> str:
    """Report the configured chain and which browser backends are installed.

    Both browser imports are lazy inside the solver package, so a missing
    extra reports as "missing" instead of raising here.
    """
    from conso.core.config import Settings

    settings = Settings.from_env()
    provider = os.environ.get("CAPTCHA_PROVIDER", "(default: cloak,camoufox)")

    cloak = "missing"
    camoufox = "missing"
    try:
        from conso.solver.cloak.turnstile import CloakTurnstileSolver

        cloak = "ready" if CloakTurnstileSolver.is_available() else "missing"
    except Exception:  # noqa: BLE001
        cloak = "error"
    try:
        from conso.solver.camoufox import CamoufoxTurnstileSolver

        camoufox = "ready" if CamoufoxTurnstileSolver.is_available() else "missing"
    except Exception:  # noqa: BLE001
        camoufox = "error"

    return render_html(
        "SOLVER  ·  conso",
        [("provider", provider), ("cloak", cloak), ("camoufox", camoufox),
         ("proxies", len(settings.proxy.urls)),
         ("per account", "yes" if settings.proxy.per_account else "no"),
         ("serial", os.environ.get("SOLVER_SERIAL", "1"))],
        footer=f"host: {os.uname().nodename}",
    )


def cmd_cron() -> str:
    try:
        cron = subprocess.run(["crontab", "-l"], capture_output=True, text=True,
                              timeout=10).stdout
    except Exception as exc:  # noqa: BLE001
        cron = f"(crontab unavailable: {exc})"
    entry = next((l for l in cron.splitlines() if "conso-daily" in l), "(not installed)")

    root = Path(__file__).resolve().parent.parent
    log = root / "logs" / "daily.log"
    tail = ""
    if log.exists():
        try:
            tail = "\n".join(log.read_text(errors="ignore").splitlines()[-4:])
        except Exception:  # noqa: BLE001
            tail = ""
    body = render_html(
        "CRON  ·  conso",
        [("schedule", entry.split("cd ")[0].strip() or "(none)")],
        footer=f"host: {os.uname().nodename}",
    )
    return body + (f"\n<pre>{tail}</pre>" if tail else "")


HELP = render_html(
    "COMMANDS  ·  conso",
    [("/" + c, d) for c, d in COMMANDS],
    footer="only this chat is served",
)


def handle(text: str, chat_id: str) -> str:
    command = text.strip().split()[0].split("@")[0].lower() if text.strip() else ""
    if chat_id != _owner():
        return "not authorised"
    if command in ("/status", "/start"):
        return cmd_status()
    if command == "/report":
        return cmd_report()
    if command == "/solver":
        return cmd_solver()
    if command == "/cron":
        return cmd_cron()
    return HELP


def main() -> int:
    try:
        from dotenv import load_dotenv

        load_dotenv(override=False)
    except ImportError:
        pass

    if not is_configured():
        print("set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID first")
        return 1

    register_menu()
    print(f"bot polling (owner={_owner()}) — Ctrl-C to stop")

    offset = 0
    while True:
        try:
            result = api("getUpdates", offset=offset + 1, timeout=POLL_TIMEOUT)
        except Exception as exc:  # noqa: BLE001
            print(f"poll error: {exc}; retrying in 5s")
            time.sleep(5)
            continue
        for update in result.get("result", []):
            offset = max(offset, int(update.get("update_id", 0)))
            message = update.get("message") or {}
            chat_id = str((message.get("chat") or {}).get("id", ""))
            text = message.get("text") or ""
            if not chat_id:
                continue
            try:
                send(chat_id, handle(text, chat_id))
            except Exception as exc:  # noqa: BLE001
                print(f"reply failed: {exc}")
        sys.stdout.flush()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nstopped")
