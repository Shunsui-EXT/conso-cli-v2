"""
Maintenance subcommands: test, doctor, solve, verify, proxies, notify.

Ported from the reference ``conso/cli.py``. ``doctor`` was rewritten for the
new solver chain: instead of reporting a single ``CAPTCHA_PROVIDER`` value it
walks the configured comma-chain and reports per-provider readiness, including
which providers are browser-free.
"""

from __future__ import annotations

import importlib.util
import os
import time
from concurrent.futures import ThreadPoolExecutor

import typer

from .. import ui
from ...core import economy
from ...core.config import Settings
from ...net.transport import Transport
from ...ops.notify import is_configured
from ...ops.notify import notify as tg_notify
from ...ops.notify import summary as tg_summary
from ...solver import (
    TURNSTILE_PAGE_URL,
    TURNSTILE_SITEKEY,
    build_solver,
    load_proxies,
)
from ...solver.chain import _provider_names

_log = ui.log_auto

#: Providers that never need a browser: they talk HTTP APIs or take a token
#: from the operator, so the core install stays browser-free.
_BROWSER_FREE = {"capsolver", "2captcha", "manual", "none"}

#: Browser libraries that must stay optional. `doctor` reports whether each is
#: importable and then greps the core packages for hard imports of them.
_BROWSER_LIBS = ("playwright", "cloakbrowser", "camoufox", "selenium", "pyppeteer", "nodriver")


def register(app: typer.Typer) -> None:
    """Attach this module's commands to ``app``."""
    app.command()(test)
    app.command()(doctor)
    app.command()(solve)
    app.command()(verify)
    app.command()(proxies)
    app.command()(backup)
    app.command(name="notify")(notify)


def test(
    platform: str = typer.Option("claude", help="Platform to simulate."),
    model: str = typer.Option("claude-opus-4-8", help="Model to simulate."),
) -> None:
    """Pre-flight: transport, backend reachability, economy sanity."""
    settings = Settings.from_env()
    _log("pre-flight start")

    # 1) economy sanity on a synthetic turn
    account = economy.account_turn(
        platform=platform,
        model=model,
        prompt_text=(
            "Write a detailed Python function that parses a JSON config file and "
            "validates every key against a schema, returning a typed dataclass.\n"
            "Output format: markdown table and code block."
        ),
        response_text="Here is a complete implementation with tests. " * 12,
    )
    _log(
        f"economy: in={account.inputTokens} out={account.outputTokens} "
        f"q={account.promptQuality} zaps={account.zaps} usd={round(account.spend_usd, 6)}"
    )

    # 2) backend reachability + captcha posture
    transport = Transport(settings)
    try:
        resp = transport.request(
            "GET",
            f"{settings.supabase_url}/auth/v1/settings",
            headers={"apikey": settings.supabase_key},
        )
        data = resp.json()
        external = data.get("external", {})
        _log(
            f"supabase: http={resp.status_code} email={external.get('email')} "
            f"google={external.get('google')} signup_disabled={data.get('disable_signup')} "
            f"autoconfirm={data.get('mailer_autoconfirm')}"
        )
    except Exception as exc:  # noqa: BLE001
        _log(f"supabase unreachable: {exc}")

    # 2b) captcha gate (expected: captcha_failed)
    try:
        resp = transport.request(
            "POST",
            f"{settings.supabase_url}/auth/v1/token?grant_type=password",
            headers={"apikey": settings.supabase_key, "Content-Type": "application/json"},
            json={"email": "probe@example.invalid", "password": "x"},
        )
        gate = (resp.json() or {}).get("error_code", "none")
        _log(f"auth gate: http={resp.status_code} error_code={gate} (captcha required: {gate == 'captcha_failed'})")
    except Exception as exc:  # noqa: BLE001
        _log(f"auth gate probe failed: {exc}")

    _log(f"turnstile: sitekey={TURNSTILE_SITEKEY} page={TURNSTILE_PAGE_URL}")

    # 3) proxy health
    if settings.proxy.urls:
        results = transport.pool.health_check(settings.impersonate)
        for url, ok in results.items():
            _log(f"proxy {'OK ' if ok else 'DEAD'} {url}")
    else:
        _log("proxy: none configured (direct)")

    transport.close()
    _log("pre-flight done")


def _provider_ready(name: str) -> tuple[bool, str]:
    """Readiness of one solver-chain provider: ``(ready, human-readable note)``.

    Browser providers are checked WITHOUT launching a browser: the dependency
    must import and the solver's own ``is_available()`` must agree. Browser
    deps are optional and lazy — a missing one downgrades that provider only.
    """
    if name == "cloak":
        if importlib.util.find_spec("cloakbrowser") is None:
            return False, "cloakbrowser not importable (browser dep is optional/lazy)"
        try:
            from ...solver.cloak.turnstile import CloakTurnstileSolver

            ok = CloakTurnstileSolver.is_available()
        except Exception as exc:  # noqa: BLE001
            return False, f"cloakbrowser present but solver unusable: {exc}"
        return ok, "in-process browser solver ready" if ok else "cloakbrowser present but reports unavailable"

    if name == "camoufox":
        if importlib.util.find_spec("camoufox") is None:
            return False, "camoufox not importable (browser dep is optional/lazy)"
        try:
            from ...solver.camoufox import CamoufoxTurnstileSolver

            ok = CamoufoxTurnstileSolver.is_available()
        except Exception as exc:  # noqa: BLE001
            return False, f"camoufox present but solver unusable: {exc}"
        return ok, "in-process browser solver ready" if ok else "camoufox present but reports unavailable"

    if name == "capsolver":
        key = os.environ.get("CAPSOLVER_API_KEY", "").strip()
        return bool(key), "API key present (browser-free)" if key else "CAPSOLVER_API_KEY not set"

    if name == "2captcha":
        key = os.environ.get("TWOCAPTCHA_API_KEY", "").strip()
        return bool(key), "API key present (browser-free)" if key else "TWOCAPTCHA_API_KEY not set"

    if name == "manual":
        return True, "operator pastes the token (browser-free)"

    if name == "none":
        return True, "NoopSolver: needs CAPTCHA_TOKEN (browser-free)"

    return False, "unknown provider (chain skips it)"


def doctor() -> None:
    """Report whether the pipeline is browser-free and what each layer uses."""
    settings = Settings.from_env()

    _log("doctor: dependency check")
    for lib in _BROWSER_LIBS:
        present = importlib.util.find_spec(lib) is not None
        _log(f"  {lib:14} {'present' if present else 'absent'}")

    # Core packages must not hard-import any browser lib: those imports are
    # only legal inside the solver providers, and even there they are lazy.
    import pathlib

    core = pathlib.Path(__file__).resolve().parents[2]
    hits: list[str] = []
    for package in ("core", "net", "store", "auth", "ops"):
        for py in sorted((core / package).glob("*.py")):
            text = py.read_text(encoding="utf-8", errors="ignore")
            for lib in _BROWSER_LIBS:
                if f"import {lib}" in text or f"from {lib}" in text:
                    hits.append(f"{py.name}:{lib}")
    _log(f"  core browser imports: {hits or 'NONE (browser-free)'}")

    # --- solver chain ------------------------------------------------------
    chain = _provider_names(os.environ.get("CAPTCHA_PROVIDER"))
    defaulted = not chain
    if defaulted:
        chain = ["cloak", "camoufox"]
    _log(f"doctor: solver chain = {', '.join(chain)}"
         f"{' (default)' if defaulted else ''}")
    ready_count = 0
    for name in chain:
        ready, note = _provider_ready(name)
        ready_count += int(ready)
        tag = "browser-free" if name in _BROWSER_FREE else "needs a browser"
        _log(f"  {name:10} {'ready    ' if ready else 'NOT ready'} [{tag}] {note}")
    if not ready_count:
        _log("  [yellow]no solver provider is ready[/yellow] — install cloakbrowser/camoufox, "
             "or set CAPSOLVER_API_KEY / TWOCAPTCHA_API_KEY")
    _log(f"doctor: browser-free providers: {', '.join(sorted(_BROWSER_FREE))} "
         "(no browser needed)")
    _log("doctor: browser deps are lazy/optional — the core install never imports them")

    _log(f"doctor: transport=curl_cffi impersonate={settings.impersonate} "
         f"proxy={'yes' if settings.proxy.urls else 'no'}")


def solve(
    sitekey: str = typer.Option(TURNSTILE_SITEKEY, help="Turnstile sitekey."),
    url: str = typer.Option(TURNSTILE_PAGE_URL, help="Page hosting the widget."),
) -> None:
    """Solve one Turnstile challenge via the configured solver (sanity check)."""
    settings = Settings.from_env()
    transport = Transport(settings)
    solver = build_solver(transport)
    _log(f"solve: provider={type(solver).__name__} sitekey={sitekey}")
    try:
        token = solver.solve_turnstile(sitekey=sitekey, page_url=url)
        _log(f"solved: token_len={len(token)} token={token[:40]}...")
    except Exception as exc:  # noqa: BLE001
        _log(f"solve failed: {exc}")
    finally:
        transport.close()


def verify() -> None:
    """Health-check the proxy pool."""
    settings = Settings.from_env()
    if not settings.proxy.urls:
        _log("verify: no proxies configured")
        return
    transport = Transport(settings)
    for url, ok in transport.pool.health_check(settings.impersonate).items():
        _log(f"{'OK ' if ok else 'DEAD'} {url}")
    transport.close()


def proxies(
    file: str = typer.Option("", help="Proxy list file (default: SOLVER_PROXY_FILE)."),
    limit: int = typer.Option(0, help="Test only the first N (0 = all)."),
) -> None:
    """Health-check a proxy list (for the in-process solver)."""
    settings = Settings.from_env()
    path = file or os.environ.get("SOLVER_PROXY_FILE", "")
    pool = load_proxies(path)
    if not pool:
        _log(f"proxies: no list at {path!r}")
        raise typer.Exit(code=1)
    if limit:
        pool = pool[:limit]
    transport = Transport(settings)
    _log(f"proxies: testing {len(pool)} from {path}")

    def check(proxy: str) -> tuple[str, bool, str]:
        try:
            # Explicit per-request proxy: these proxies come from a file, not
            # from the pool the transport rotates, so they must be passed
            # straight to the session instead of through `acquire()`.
            resp = transport._session.get(
                "https://api.ipify.org?format=json",
                proxies={"http": proxy, "https": proxy},
                impersonate=settings.impersonate, timeout=12,
            )
            ok = resp.status_code == 200
            return proxy, ok, (resp.text[:40] if ok else f"http {resp.status_code}")
        except Exception as exc:  # noqa: BLE001
            return proxy, False, str(exc)[:60]

    live = 0
    with ThreadPoolExecutor(max_workers=min(20, len(pool))) as pool_exec:
        for proxy, ok, note in pool_exec.map(check, pool):
            host = proxy.split("@")[-1]
            if ok:
                live += 1
                _log(f"  LIVE {host} {note}")
    _log(f"proxies: {live}/{len(pool)} live")
    transport.close()


def notify() -> None:
    """Send a test Telegram message (verifies TELEGRAM_BOT_TOKEN/CHAT_ID)."""
    if not is_configured():
        ui.error("Telegram is not configured: set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env")
        raise typer.Exit(1)
    text = tg_summary("CONSO TEST", [("status", "ok"), ("time", time.strftime("%Y-%m-%d %H:%M:%S"))])
    ui.log(f"notify: sending\n{text}")
    tg_notify("CONSO TEST", [("status", "ok"), ("time", time.strftime("%Y-%m-%d %H:%M:%S"))],
              logger=_log)
    ui.log("notify: sent (check your Telegram)", level="ok")


def backup(
    keep: int = typer.Option(20, help="How many timestamped backups to keep."),
) -> None:
    """Copy the account store aside, so a lost data/ dir never costs access.

    The store holds the rotated refresh tokens: losing it means falling back to
    the OTP path for every account, and the password is not recoverable at all.
    Copies land in ``backups/`` (gitignored — they hold the same credentials).
    """
    import shutil
    from pathlib import Path

    settings = Settings.from_env()
    data_dir = Path(settings.data_dir)
    if not data_dir.is_dir():
        ui.error(f"backup: no {data_dir}/ directory yet — nothing to copy")
        raise typer.Exit(1)

    sources = [p for p in sorted(data_dir.iterdir()) if p.is_file()]
    if not sources:
        ui.error(f"backup: {data_dir}/ is empty — nothing to copy")
        raise typer.Exit(1)

    out_dir = Path("backups")
    out_dir.mkdir(exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    copied = 0
    for src in sources:
        shutil.copy2(src, out_dir / f"{src.stem}-{stamp}{src.suffix}")
        copied += 1

    # Prune oldest per family so the dir cannot grow without bound. The family
    # key keeps the extension: `accounts.json` and `accounts.csv` are different
    # files, and grouping them by stem alone would prune one of each pair away.
    # `keep` counts copies of the SAME file, which is what an operator expects.
    pruned = 0
    families = {f"{p.stem}-*{p.suffix}" for p in sources}
    for pattern in families:
        family = sorted(out_dir.glob(pattern), key=lambda p: p.stat().st_mtime)
        for old in family[:-keep] if keep > 0 else []:
            old.unlink()
            pruned += 1

    ui.log(f"backup: {copied} file(s) -> {out_dir}/ ({stamp})"
           + (f", pruned {pruned} old" if pruned else ""), level="ok")
