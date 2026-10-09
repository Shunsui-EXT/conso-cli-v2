"""
Camoufox-backed Turnstile solver (ported near-verbatim from the proven
production implementation in the prior CONSO deployment).

Why Camoufox AND Cloak?
------------------------
Both are internal (in-process) solvers that drive a real browser; they are
listed in the chain's default (``cloak,camoufox``) so a transient failure
in one still produces a token through the other. Camoufox has 58 accounts'
worth of production evidence; the cloak port is new.

Measured behaviour that we deliberately preserve
------------------------------------------------
- Persistent browser, started lazily and reused across solves (first-solve
  latency = launch time; subsequent solves pay just the challenge wait).
- Dedicated asyncio event loop in a daemon thread, driven from blocking
  callers via ``asyncio.run_coroutine_threadsafe`` — this is how a thread
  pool of registration workers can share one browser.
- ``asyncio.Semaphore`` for parallel solves (not a lock) — each solve
  takes its own ``browser.new_context()`` + ``new_page()``.
- Context per solve, closed afterwards — isolation, no cookie leakage.
- Optional per-solve proxy via Playwright's proxy dict.
- Cooldown between solves (``SOLVER_SOLVE_DELAY``) — Cloudflare rate-flags
  an IP that solves too quickly; 30-45s is the measured safe range.
- Real-page solve on the Conso verify-human URL: Supabase rejects stub
  tokens as ``invalid-input-response``. The explicit ``turnstile.render()``
  from the api.js onload callback is the path that reliably mints a token
  accepted by Supabase.
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from concurrent.futures import Future
from dataclasses import dataclass
from typing import Any, Coroutine
from urllib.parse import quote, urlparse

from .base import (
    CONSO_REDIRECT_URIS,
    TURNSTILE_PAGE_URL,
    TURNSTILE_SITEKEY,
    SolverError,
)


# Stub page that explicitly calls ``turnstile.render()`` from the api.js
# onload callback. The real page's own Next.js <Script> render is unreliable
# in Camoufox (widget renders before api.js is ready, CSP blocks the inline
# render on some loads). An explicit onload callback renders reliably.
_STUB_HTML = """<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">
<script src="https://challenges.cloudflare.com/turnstile/v0/api.js?onload=__consoCb" async defer></script>
<script>function __consoCb(){window.turnstile.render("#cf-slot", __CONSO_OPTS__);}</script>
</head><body><div id="cf-slot"></div><p id="ip-display"></p></body></html>"""


@dataclass(frozen=True)
class _Config:
    headless: bool = True
    timeout_seconds: int = 90
    proxy: str | None = None


class CamoufoxTurnstileSolver:
    """Persistent in-process Camoufox Turnstile solver.

    Lifecycle: build; call ``warm()`` to launch the browser now (or let the
    first ``solve_turnstile()`` warm it on demand); call ``close()`` on
    shutdown to join the event-loop thread.

    Camoufox is imported LAZILY — importing this module does not require
    camoufox to be installed. The constructor stores config only; the first
    actual async entry (``warm`` or ``solve_turnstile``) imports camoufox
    and raises ``SolverError`` on ``ImportError``.
    """

    def __init__(
        self,
        *,
        headless: bool | None = None,
        timeout_seconds: int | None = None,
        proxy: str | None = None,
        max_concurrent: int | None = None,
        real_page: bool | None = None,
    ) -> None:
        if headless is None:
            headless = os.environ.get("SOLVER_HEADLESS", "1") != "0"
        if timeout_seconds is None:
            timeout_seconds = int(os.environ.get("SOLVER_TIMEOUT", "90") or 90)
        if max_concurrent is None:
            max_concurrent = int(os.environ.get("SOLVER_MAX_CONCURRENT", "2") or 2)
        if real_page is None:
            # Conso-specific default: route-intercept with the explicit-render
            # stub (``_STUB_HTML``) is the ONLY path with 58-account production
            # evidence of accepted tokens. ``real_page=True`` remains available
            # for callers that need the real verify-human DOM.
            real_page = False

        self._cfg = _Config(
            headless=headless,
            timeout_seconds=max(10, int(timeout_seconds)),
            proxy=proxy,
        )
        self._max_concurrent = max(1, int(max_concurrent))
        self._real_page = bool(real_page)

        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._loop_lock = threading.Lock()
        self._loop_ready = threading.Event()

        self._camoufox: Any = None
        self._browser: Any = None
        self._browser_lock: asyncio.Lock | None = None
        self._sem: asyncio.Semaphore | None = None
        self._closed = False

        # Retry + cooldown config (same levers as the production deployment).
        self._attempts = int(os.environ.get("SOLVER_ATTEMPTS", "3") or 3)
        self._attempt_timeout = float(
            os.environ.get("SOLVER_ATTEMPT_TIMEOUT", "45") or 45
        )
        self._backoff = float(os.environ.get("SOLVER_BACKOFF", "3") or 3)
        self._cooldown = float(os.environ.get("SOLVER_SOLVE_DELAY", "0") or 0)
        self._last_solve = 0.0
        self._solve_at_lock = threading.Lock()

        # Pinned per-account proxy (consumed once, then the per-attempt pool
        # resumes).
        self._pinned_proxy = ""
        self._pin_lock = threading.Lock()

    # -- public helpers ----------------------------------------------------

    @property
    def ready(self) -> bool:
        return self._browser is not None and not self._closed

    @staticmethod
    def is_available() -> bool:
        try:
            import camoufox  # noqa: F401
            return True
        except ImportError:
            return False

    def warm(self, *, timeout: float = 60.0) -> bool:
        """Start the browser ahead of the first solve (blocks until ready)."""
        if self.ready:
            return True
        try:
            self._submit(self._ensure_browser(), timeout=timeout)
            return self.ready
        except Exception:  # noqa: BLE001 - warm is best-effort
            return False

    def warm_async(self) -> threading.Thread:
        """Warm the browser on a background thread (non-blocking)."""
        t = threading.Thread(
            target=self.warm, name="conso-camoufox-warm", daemon=True
        )
        t.start()
        return t

    def pin_account_proxy(self, proxy: str) -> None:
        """Make the next solve prefer ``proxy`` (one-shot)."""
        with self._pin_lock:
            self._pinned_proxy = proxy or ""

    # -- loop / browser plumbing ------------------------------------------

    def _start_loop(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        with self._loop_lock:
            if self._thread and self._thread.is_alive():
                return
            self._loop_ready.clear()
            self._thread = threading.Thread(
                target=self._loop_main, name="conso-camoufox-solver", daemon=True
            )
            self._thread.start()
            if not self._loop_ready.wait(timeout=15):
                raise SolverError("Camoufox event loop failed to start")

    def _loop_main(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        self._loop_ready.set()
        try:
            loop.run_forever()
        finally:
            pending = asyncio.all_tasks(loop)
            for task in pending:
                task.cancel()
            if pending:
                loop.run_until_complete(
                    asyncio.gather(*pending, return_exceptions=True)
                )
            loop.close()

    def _submit(self, coro: Coroutine[Any, Any, Any], timeout: float | None = None) -> Any:
        if self._closed:
            raise SolverError("Camoufox solver is closed")
        self._start_loop()
        if self._loop is None:
            raise SolverError("Camoufox event loop unavailable")
        future: Future[Any] = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result(timeout=timeout)

    async def _ensure_browser(self) -> None:
        if self._browser is not None:
            return
        if self._browser_lock is None:
            self._browser_lock = asyncio.Lock()
        async with self._browser_lock:
            if self._browser is not None:
                return
            if self._sem is None:
                self._sem = asyncio.Semaphore(self._max_concurrent)
            try:
                # Python 3.14 + multiprocessing resource_tracker guard — the
                # production GROK-CLI port patches this at import time.
                try:
                    import camoufox.addons  # type: ignore[import-untyped]

                    camoufox.addons.Lock = threading.Lock  # type: ignore[assignment]
                except Exception:  # noqa: BLE001
                    pass
                from camoufox import DefaultAddons  # type: ignore[import-untyped]
                from camoufox.async_api import AsyncCamoufox  # type: ignore[import-untyped]
            except ImportError as exc:
                raise SolverError(
                    "camoufox is not installed; "
                    "pip install 'camoufox[geoip]>=0.4.0' && python -m camoufox fetch"
                ) from exc
            self._camoufox = AsyncCamoufox(
                headless=self._cfg.headless,
                exclude_addons=[DefaultAddons.UBO],
                args=["--no-sandbox", "--disable-setuid-sandbox"],
            )
            try:
                self._browser = await self._camoufox.start()
            except Exception as exc:  # noqa: BLE001
                await self._close_async()
                raise SolverError(
                    f"failed to start embedded Camoufox: {exc}"
                ) from exc

    @staticmethod
    def _proxy_config(proxy: str | None) -> dict[str, str] | None:
        if not proxy:
            return None
        parsed = urlparse(proxy)
        if not parsed.scheme or not parsed.hostname or not parsed.port:
            raise SolverError(f"invalid solver proxy: {proxy}")
        result = {"server": f"{parsed.scheme}://{parsed.hostname}:{parsed.port}"}
        if parsed.username:
            result["username"] = parsed.username
        if parsed.password:
            result["password"] = parsed.password
        return result

    # -- async solve core --------------------------------------------------

    async def _solve_async(
        self,
        url: str,
        sitekey: str,
        *,
        action: str | None,
        cdata: str | None,
        timeout_seconds: int,
        proxy: str | None,
        real_page: bool,
    ) -> str:
        await self._ensure_browser()
        if self._sem is None:
            self._sem = asyncio.Semaphore(self._max_concurrent)

        async with self._sem:
            target = url if url.endswith("/") else url + "/"
            opts: dict[str, Any] = {"sitekey": sitekey}
            if action:
                opts["action"] = action
            if cdata:
                opts["cdata"] = cdata
            page_data = _STUB_HTML.replace("__CONSO_OPTS__", json.dumps(opts))
            deadline = time.monotonic() + timeout_seconds

            proxy_cfg = self._proxy_config(proxy or self._cfg.proxy)
            ctx = (
                await self._browser.new_context(proxy=proxy_cfg)
                if proxy_cfg
                else await self._browser.new_context()
            )
            try:
                page = await ctx.new_page()
                if real_page:
                    await page.goto(
                        target, wait_until="domcontentloaded", timeout=30_000
                    )
                    # Inject the explicit render callback. Camoufox's own CSP
                    # bypass is reliable here; the page's inline <Script> is
                    # not, so we drive turnstile.render() ourselves.
                    await page.evaluate(
                        """(opts) => new Promise((resolve) => {
                            const go = () => { window.turnstile.render("#cf-slot", opts); resolve(); };
                            if (window.turnstile) { go(); return; }
                            const s = document.createElement('script');
                            s.src = "https://challenges.cloudflare.com/turnstile/v0/api.js?onload=__consoCb2";
                            window.__consoCb2 = go;
                            document.head.appendChild(s);
                        })""",
                        opts,
                    )
                    try:
                        await page.evaluate(
                            """() => { if (!document.querySelector('#cf-slot')) {
                                const d = document.createElement('div'); d.id = 'cf-slot';
                                document.body.prepend(d); } }"""
                        )
                    except Exception:  # noqa: BLE001
                        pass
                else:
                    await page.route(
                        target,
                        lambda route: route.fulfill(body=page_data, status=200),
                    )
                    await page.goto(
                        target, wait_until="domcontentloaded", timeout=25_000
                    )

                while time.monotonic() < deadline:
                    try:
                        token = await page.evaluate(
                            "() => { const e=document.querySelector("
                            "'[name=cf-turnstile-response]'); return e?e.value:'' }"
                        )
                        if token and len(token) > 50:
                            return token
                    except Exception:  # noqa: BLE001
                        pass
                    await asyncio.sleep(0.3)

                raise SolverError(
                    f"Camoufox Turnstile solve timed out after {timeout_seconds}s "
                    "(solve IP probably rate-flagged — wait, raise "
                    "SOLVER_SOLVE_DELAY, use residential proxies, or switch to "
                    "CAPTCHA_PROVIDER=capsolver/2captcha)"
                )
            finally:
                try:
                    await ctx.close()
                except Exception:  # noqa: BLE001
                    pass

    # -- blocking public solve --------------------------------------------

    def _consume_pinned(self) -> str:
        with self._pin_lock:
            proxy = self._pinned_proxy
            self._pinned_proxy = ""
            return proxy

    def _apply_cooldown(self) -> None:
        if self._cooldown <= 0:
            return
        with self._solve_at_lock:
            wait = self._last_solve + self._cooldown - time.time()
            if wait > 0:
                time.sleep(wait)
            self._last_solve = time.time()

    def solve_turnstile(
        self,
        *,
        sitekey: str = TURNSTILE_SITEKEY,
        page_url: str = TURNSTILE_PAGE_URL,
        timeout: float = 120.0,
    ) -> str:
        if not sitekey or not page_url:
            raise SolverError("sitekey and page_url are required")

        # Verify-human only renders with a whitelisted redirect_uri.
        if (
            "redirect_uri=" not in page_url
            and "www.conso.xyz/verify-human" in page_url
        ):
            sep = "&" if "?" in page_url else "?"
            page_url = f"{page_url}{sep}redirect_uri={quote(CONSO_REDIRECT_URIS[0], safe='')}"

        self._apply_cooldown()
        proxy = self._consume_pinned()

        # Per-attempt budget: min(caller timeout, SOLVER_ATTEMPT_TIMEOUT).
        # A rate-flagged IP never returns a token, so burn a short slice per
        # attempt instead of the whole caller timeout on attempt #1.
        budget = int(max(10, min(timeout, self._attempt_timeout)))
        last = ""
        for attempt in range(1, self._attempts + 1):
            try:
                return self._submit(
                    self._solve_async(
                        page_url,
                        sitekey,
                        action=None,
                        cdata=None,
                        timeout_seconds=budget,
                        proxy=proxy or None,
                        real_page=self._real_page,
                    ),
                    timeout=budget + 45,
                )
            except Exception as exc:  # noqa: BLE001
                last = str(exc)
                if attempt >= self._attempts:
                    break
                # Hard install failures do not benefit from retry.
                if "not installed" in last or "failed to start" in last:
                    break
                time.sleep(self._backoff)
        raise SolverError(
            f"camoufox turnstile solve failed after {self._attempts} attempt(s) "
            f"({budget}s each): {last}"
        )

    # -- teardown ----------------------------------------------------------

    async def _close_async(self) -> None:
        if self._browser is not None:
            try:
                await self._browser.close()
            except Exception:  # noqa: BLE001
                pass
        self._browser = None
        self._camoufox = None

    def close(self) -> None:
        if self._closed:
            return
        if self._loop and self._loop.is_running():
            try:
                self._submit(self._close_async(), timeout=20)
            except Exception:  # noqa: BLE001
                pass
            self._closed = True
            self._loop.call_soon_threadsafe(self._loop.stop)
            if self._thread:
                self._thread.join(timeout=5)
        else:
            self._closed = True

    def __enter__(self) -> "CamoufoxTurnstileSolver":
        return self

    def __exit__(self, *args: object) -> None:
        self.close()


__all__ = ["CamoufoxTurnstileSolver"]
