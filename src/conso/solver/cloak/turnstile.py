"""
CloakBrowser-backed Turnstile solver (async core + blocking wrapper).

This is a direct port of ``turnstile/solve.py`` from waguriagentic/captcha-
solver, extended with a blocking ``CloakTurnstileSolver`` class that mirrors
the production Camoufox solver's lifecycle: persistent browser, dedicated
asyncio loop in a daemon thread, semaphore-bounded concurrency, context per
solve, cooldown, fail-fast retries, pinned per-account proxy.

Three async modes are preserved verbatim (minus the env-driven kwargs):

- :func:`solve_turnstile`         — route-intercept stub-page solve
- :func:`solve_and_verify`        — same + verify from the same session
- :func:`solve_turnstile_realpage`— real page, pre_actions, humanized click

Conso-specific defaults in the sync wrapper:

- ``real_page=True`` — Supabase rejects stub-page tokens for the Conso
  sitekey (``invalid-input-response``), so the real verify-human page is
  the accepted path.
- When ``page_url`` is the Conso verify page and has no ``redirect_uri``,
  the wrapper appends ``CONSO_REDIRECT_URIS[0]`` so the Next.js widget
  renders.

ALL cloakbrowser imports are LAZY (inside functions) so this module imports
cleanly on a plain Python with no browser deps installed.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
from concurrent.futures import Future
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Coroutine
from urllib.parse import quote

from ..base import (
    CONSO_REDIRECT_URIS,
    TURNSTILE_PAGE_URL,
    TURNSTILE_SITEKEY,
    SolverError,
    load_proxies,
)
from .browser import (
    build_browser_kwargs,
    fetch_from_page,
    route_glob,
    run_post_fetch,
    run_pre_actions,
)

log = logging.getLogger(__name__)

# Template is read lazily (first use) and cached, so an import of this
# module does not touch the filesystem.
_TEMPLATE_PATH = Path(__file__).parent / "templates" / "turnstile_stub.html"
_HTML_TEMPLATE: str | None = None


def _html_template() -> str:
    """Return the Turnstile stub HTML (cached read)."""
    global _HTML_TEMPLATE
    if _HTML_TEMPLATE is None:
        _HTML_TEMPLATE = _TEMPLATE_PATH.read_text(encoding="utf-8")
    return _HTML_TEMPLATE


# ---------------------------------------------------------------------------
# Async primitives (ported from turnstile/solve.py)
# ---------------------------------------------------------------------------


def _error_codes(body: str) -> list:
    """Pull Cloudflare siteverify error-codes from a verify response."""
    try:
        data = json.loads(body)
    except (ValueError, TypeError):
        return []
    return data.get("error-codes") or data.get("details") or []


async def _get_turnstile_response_route(page: Any, max_attempts: int = 20) -> str:
    """Retrieve a token from a route-intercepted stub page."""
    for _ in range(max_attempts):
        try:
            val = await page.input_value("[name=cf-turnstile-response]")
            if val == "":
                try:
                    await page.click("//div[@class='cf-turnstile']", timeout=3000)
                except Exception:  # noqa: BLE001
                    pass
                await asyncio.sleep(1)
            else:
                el = await page.query_selector("[name=cf-turnstile-response]")
                if el:
                    return await el.get_attribute("value")
                break
        except Exception:  # noqa: BLE001
            await asyncio.sleep(1)
    raise TimeoutError("Token not received via route-intercept")


async def _launch_browser(**kw: Any) -> Any:
    """Lazy import + launch of CloakBrowser."""
    try:
        import cloakbrowser  # type: ignore[import-untyped]
    except ImportError as exc:
        raise SolverError(
            "cloakbrowser is not installed (pip install cloakbrowser && "
            "python -m playwright install chromium)"
        ) from exc
    return await cloakbrowser.launch_async(**kw)


def _widget_div(sitekey: str, action: str | None, cdata: str | None) -> str:
    """Return the HTML snippet injected into the stub template.

    sitekey/action/cdata are HTML attribute values; they are placed inside
    double-quoted attributes so a bare value with normal characters is safe.
    Callers that need to accept hostile sitekeys should sanitize upstream.
    """
    attrs = f' data-sitekey="{sitekey}"'
    if action:
        attrs += f' data-action="{action}"'
    if cdata:
        attrs += f' data-cdata="{cdata}"'
    return f'<div class="cf-turnstile"{attrs}></div>'


async def solve_turnstile(
    sitekey: str,
    url: str,
    action: str | None = None,
    cdata: str | None = None,
    proxy: str | None = None,
    *,
    headless: bool = True,
    geoip: bool = False,
) -> dict:
    """Solve Turnstile via route-interception. Returns ``{token, expires_in, ...}``.

    Fastest mode: a local HTML stub is served at ``url``'s origin via
    ``page.route`` and the official api.js renders the widget inline. Some
    sites reject tokens minted on a stub context (Conso is one — use
    :func:`solve_turnstile_realpage` there).
    """
    t0 = time.monotonic()
    page_data = _html_template().replace(
        "<!-- cf turnstile -->", _widget_div(sitekey, action, cdata)
    )
    kw = build_browser_kwargs(headless=headless, geoip=geoip, proxy=proxy)
    async with await _launch_browser(**kw) as browser:
        page = await browser.new_page()
        try:
            await page.route(
                route_glob(url),
                lambda r: r.fulfill(body=page_data, status=200),
            )
            await page.goto(url, wait_until="domcontentloaded")
            token = await _get_turnstile_response_route(page)
            return {
                "token": token,
                "expires_in": 300,
                "elapsed": round(time.monotonic() - t0, 1),
                "method": "route",
            }
        finally:
            await page.close()


async def solve_and_verify(
    sitekey: str,
    verify_url: str,
    verify_payload: dict | None = None,
    *,
    action: str | None = None,
    cdata: str | None = None,
    page_url: str | None = None,
    proxy: str | None = None,
    headless: bool = True,
    geoip: bool = False,
) -> dict:
    """Solve via route-intercept, then verify from the same browser session."""
    t0 = time.monotonic()
    target = page_url or verify_url
    page_data = _html_template().replace(
        "<!-- cf turnstile -->", _widget_div(sitekey, action, cdata)
    )
    kw = build_browser_kwargs(headless=headless, geoip=geoip, proxy=proxy)
    async with await _launch_browser(**kw) as browser:
        page = await browser.new_page()
        try:
            await page.route(
                route_glob(target),
                lambda r: r.fulfill(body=page_data, status=200),
            )
            await page.goto(target, wait_until="domcontentloaded", timeout=30_000)
            token = await _get_turnstile_response_route(page)
            log.info("Route-intercept: token obtained in %.1fs", time.monotonic() - t0)

            payload = dict(verify_payload or {})
            payload["token"] = token
            result = await fetch_from_page(
                page, verify_url, "POST", json.dumps(payload)
            )
            codes = _error_codes(result["body"])
            # Never log response body — it may carry session tokens/JWTs.
            log.info(
                "Route-intercept verify: %d codes=%s", result["status"], codes
            )
            return {
                "token": token,
                "expires_in": 300,
                "verify_status": result["status"],
                "verify_body": result["body"],
                "verify_error_codes": codes,
                "method": "route",
                "elapsed": round(time.monotonic() - t0, 1),
            }
        finally:
            await page.close()


# Sitekey passed as the ``evaluate`` arg ``k`` — never interpolated into JS
# source (injection-safe). No ``data-theme``: a hard-coded theme is a fixed
# real-page fingerprint.
_WIDGET_INJECT_JS = (
    "(k) => {"
    "  const d = document.createElement('div');"
    "  d.className = 'cf-turnstile';"
    "  d.setAttribute('data-sitekey', k);"
    "  document.body.prepend(d);"
    "}"
)


async def _inject_turnstile_widget(page: Any, sitekey: str) -> None:
    await page.evaluate(_WIDGET_INJECT_JS, sitekey)


async def _human_click_iframe(page: Any, fr: Any) -> bool:
    """Click the Turnstile checkbox via humanized page-level mouse movement.

    CloakBrowser's humanizer hooks ``page.mouse.click`` (B-spline paths +
    overshoot) but NOT ``frame.click``, so clicking the cross-origin iframe
    via a frame selector sends a robotic instant click. Instead we resolve
    the iframe's page-absolute box and click at the checkbox offset
    (left edge + 30px, vertical centre) via the humanized ``page.mouse``.
    """
    try:
        el = await fr.frame_element()
        box = await el.bounding_box()
    except Exception:  # noqa: BLE001
        return False
    if not box or box["width"] < 20:
        return False
    x = box["x"] + 30
    y = box["y"] + box["height"] / 2
    await page.mouse.click(x, y)  # humanized (B-spline) — page-level
    return True


async def _click_turnstile_checkbox(page: Any, attempts: int = 25) -> bool:
    """Click the checkbox inside the cross-origin Cloudflare iframe."""
    for _ in range(attempts):
        for fr in page.frames:
            if "challenges.cloudflare.com" in (fr.url or ""):
                if await _human_click_iframe(page, fr):
                    return True
                for sel in ("input[type=checkbox]", "label", "body"):
                    try:
                        await fr.click(sel, timeout=2000)
                        return True
                    except Exception:  # noqa: BLE001
                        continue
        await asyncio.sleep(1)
    return False


async def solve_turnstile_realpage(
    url: str,
    sitekey: str | None = None,
    *,
    timeout_s: int = 60,
    pre_actions: list | None = None,
    post_fetch: list | None = None,
    proxy: str | None = None,
    headless: bool = True,
    geoip: bool = False,
) -> dict:
    """Navigate a real page, humanized-click Turnstile, return token + cookies.

    See upstream README for ``pre_actions`` / ``post_fetch`` selector syntax
    (CSS/XPath/text/role/regex). ``__TOKEN__`` in a ``post_fetch`` body is
    replaced with the solved token.
    """
    t0 = time.monotonic()
    kw = build_browser_kwargs(headless=headless, geoip=geoip, proxy=proxy)
    async with await _launch_browser(**kw) as browser:
        page = await browser.new_page()
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=45_000)

            if pre_actions:
                await run_pre_actions(page, pre_actions)
                await asyncio.sleep(2)

            if sitekey:
                await _inject_turnstile_widget(page, sitekey)
                await asyncio.sleep(3)

            clicked = await _click_turnstile_checkbox(page)
            log.info("Real-page checkbox clicked=%s", clicked)

            token = ""
            deadline = time.monotonic() + timeout_s
            while time.monotonic() < deadline:
                try:
                    token = await page.evaluate(
                        "() => { const e=document.querySelector("
                        "'[name=cf-turnstile-response]'); return e ? e.value : '' }"
                    )
                except Exception:  # noqa: BLE001
                    token = ""
                if token:
                    break
                await asyncio.sleep(1)

            cookies = await page.context.cookies()
            result: dict = {
                "token": token,
                "verify_success": bool(token),
                "cookies": cookies,
                "method": "real-page",
                "elapsed": round(time.monotonic() - t0, 1),
            }
            if post_fetch and token:
                result["post_fetch"] = await run_post_fetch(
                    page, post_fetch, token
                )
            return result
        finally:
            await page.close()


# ---------------------------------------------------------------------------
# Blocking sync wrapper — the public solver provider used by the chain
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Config:
    headless: bool
    geoip: bool
    timeout_seconds: int
    real_page: bool


# Process-wide serialisation of solves. Cloudflare issues one usable
# challenge flow per source IP; overlapping solves make every participant
# time out. See analysis/SOLVER_RELIABILITY.md for the measurement.
# SOLVER_SERIAL=0 disables this when every worker exits via a distinct IP.
_SOLVE_LOCK = threading.Lock()


class CloakTurnstileSolver:
    """In-process CloakBrowser Turnstile solver (blocking, thread-safe)."""

    def __init__(
        self,
        *,
        headless: bool | None = None,
        geoip: bool | None = None,
        timeout_seconds: int | None = None,
        proxy: str | None = None,
        max_concurrent: int | None = None,
        real_page: bool | None = None,
        attempts: int | None = None,
        attempt_timeout: float | None = None,
        backoff: float | None = None,
        solve_delay: float | None = None,
        serial: bool | None = None,
        proxy_file: str | None = None,
        proxy_mode: str | None = None,
    ) -> None:
        # Env resolution: inside the constructor, never at module scope.
        if headless is None:
            headless = os.environ.get(
                "CLOAK_HEADLESS", os.environ.get("SOLVER_HEADLESS", "1")
            ) != "0"
        if geoip is None:
            geoip = os.environ.get("CLOAK_GEOIP", "0") != "0"
        if timeout_seconds is None:
            timeout_seconds = int(os.environ.get("SOLVER_TIMEOUT", "90") or 90)
        if max_concurrent is None:
            # Cloak sessions are heavier than Camoufox contexts; default 2.
            max_concurrent = int(os.environ.get("CLOAK_MAX_CONCURRENT", "2") or 2)
        if real_page is None:
            # Conso: real_page=True is the accepted path (Supabase rejects
            # stub tokens as invalid-input-response).
            real_page = True
        if attempts is None:
            attempts = int(os.environ.get("SOLVER_ATTEMPTS", "3") or 3)
        if attempt_timeout is None:
            attempt_timeout = float(
                os.environ.get("SOLVER_ATTEMPT_TIMEOUT", "45") or 45
            )
        if backoff is None:
            backoff = float(os.environ.get("SOLVER_BACKOFF", "3") or 3)
        if solve_delay is None:
            solve_delay = float(os.environ.get("SOLVER_SOLVE_DELAY", "0") or 0)
        if serial is None:
            serial = os.environ.get("SOLVER_SERIAL", "1") != "0"
        if proxy_file is None:
            proxy_file = os.environ.get("SOLVER_PROXY_FILE", "")
        if proxy_mode is None:
            proxy_mode = os.environ.get("SOLVER_PROXY_MODE", "attempt").strip().lower()

        self._cfg = _Config(
            headless=bool(headless),
            geoip=bool(geoip),
            timeout_seconds=max(10, int(timeout_seconds)),
            real_page=bool(real_page),
        )
        self._default_proxy = proxy
        self._max_concurrent = max(1, int(max_concurrent))
        self._attempts = max(1, int(attempts))
        self._attempt_timeout = float(attempt_timeout)
        self._backoff = float(backoff)
        self._cooldown = float(solve_delay)
        self._serial = bool(serial)

        # Proxy pool for per-attempt rotation (``attempt`` mode) or
        # per-worker stickiness (``worker`` mode).
        self._proxies = load_proxies(proxy_file)
        self._proxy_mode = proxy_mode if proxy_mode in {"attempt", "worker"} else "attempt"
        self._proxy_idx = 0
        self._proxy_lock = threading.Lock()
        self._local = threading.local()

        # Pinned per-account proxy — consumed before any pool proxy.
        self._pinned_proxy = ""
        self._pin_lock = threading.Lock()

        # Loop + browser state.
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._loop_lock = threading.Lock()
        self._loop_ready = threading.Event()
        self._browser: Any = None
        self._browser_cm: Any = None  # async context manager (keeps browser alive)
        self._browser_lock: asyncio.Lock | None = None
        self._sem: asyncio.Semaphore | None = None
        self._closed = False
        self._last_solve = 0.0
        self._cool_lock = threading.Lock()

    # -- public helpers ----------------------------------------------------

    @property
    def ready(self) -> bool:
        return self._browser is not None and not self._closed

    @staticmethod
    def is_available() -> bool:
        try:
            import cloakbrowser  # type: ignore[import-untyped]  # noqa: F401
            return True
        except ImportError:
            return False

    def warm(self, *, timeout: float = 60.0) -> bool:
        """Start the browser now; return True once it's up."""
        if self.ready:
            return True
        try:
            self._submit(self._ensure_browser(), timeout=timeout)
            return self.ready
        except Exception:  # noqa: BLE001 - best-effort
            return False

    def warm_async(self) -> threading.Thread:
        """Warm on a background thread (non-blocking)."""
        t = threading.Thread(target=self.warm, name="conso-cloak-warm", daemon=True)
        t.start()
        return t

    def pin_account_proxy(self, proxy: str) -> None:
        """Make the next solve prefer ``proxy`` once (per-account sticky)."""
        with self._pin_lock:
            self._pinned_proxy = proxy or ""

    # -- loop + browser plumbing ------------------------------------------

    def _start_loop(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        with self._loop_lock:
            if self._thread and self._thread.is_alive():
                return
            self._loop_ready.clear()
            self._thread = threading.Thread(
                target=self._loop_main, name="conso-cloak-solver", daemon=True
            )
            self._thread.start()
            if not self._loop_ready.wait(timeout=15):
                raise SolverError("Cloak event loop failed to start")

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
            raise SolverError("Cloak solver is closed")
        self._start_loop()
        if self._loop is None:
            raise SolverError("Cloak event loop unavailable")
        future: Future[Any] = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result(timeout=timeout)

    async def _ensure_browser(self) -> Any:
        if self._browser is not None:
            return self._browser
        if self._browser_lock is None:
            self._browser_lock = asyncio.Lock()
        async with self._browser_lock:
            if self._browser is not None:
                return self._browser
            if self._sem is None:
                self._sem = asyncio.Semaphore(self._max_concurrent)
            kw = build_browser_kwargs(
                headless=self._cfg.headless,
                geoip=self._cfg.geoip,
                proxy=self._default_proxy,
            )
            # ``launch_async`` returns an async context manager; keep it on
            # the instance so ``close()`` can ``__aexit__`` it. We never
            # ``async with`` it here — we hold on to the entered browser.
            cm = await _launch_browser(**kw)
            try:
                self._browser = await cm.__aenter__()
            except Exception as exc:  # noqa: BLE001
                raise SolverError(f"failed to start CloakBrowser: {exc}") from exc
            self._browser_cm = cm
            return self._browser

    async def _close_async(self) -> None:
        cm = self._browser_cm
        self._browser_cm = None
        self._browser = None
        if cm is not None:
            try:
                await cm.__aexit__(None, None, None)
            except Exception:  # noqa: BLE001
                pass

    # -- async solve core (uses persistent browser + semaphore) ------------

    async def _solve_async(
        self,
        page_url: str,
        sitekey: str,
        *,
        timeout_s: int,
        proxy: str | None,
        real_page: bool,
    ) -> str:
        browser = await self._ensure_browser()
        if self._sem is None:
            self._sem = asyncio.Semaphore(self._max_concurrent)

        async with self._sem:
            # One context per solve, closed after. Proxy applied at the
            # context layer when provided (so the persistent browser can
            # launch without a default proxy and each solve can route
            # through a different exit IP).
            from .browser import parse_proxy  # local import to keep cold-path light

            ctx_proxy = parse_proxy(proxy) if proxy else None
            ctx = (
                await browser.new_context(proxy=ctx_proxy)
                if ctx_proxy
                else await browser.new_context()
            )
            try:
                page = await ctx.new_page()
                if real_page:
                    await page.goto(
                        page_url, wait_until="domcontentloaded", timeout=45_000
                    )
                    try:
                        await _click_turnstile_checkbox(page)
                    except Exception:  # noqa: BLE001
                        pass
                else:
                    page_data = _html_template().replace(
                        "<!-- cf turnstile -->", _widget_div(sitekey, None, None)
                    )
                    await page.route(
                        route_glob(page_url),
                        lambda r: r.fulfill(body=page_data, status=200),
                    )
                    await page.goto(
                        page_url, wait_until="domcontentloaded", timeout=25_000
                    )

                token = ""
                deadline = time.monotonic() + timeout_s
                while time.monotonic() < deadline:
                    try:
                        token = await page.evaluate(
                            "() => { const e=document.querySelector("
                            "'[name=cf-turnstile-response]'); return e ? e.value : '' }"
                        )
                    except Exception:  # noqa: BLE001
                        token = ""
                    if token and len(token) > 50:
                        return token
                    await asyncio.sleep(0.5)
                raise SolverError(
                    f"cloak Turnstile solve timed out after {timeout_s}s "
                    "(IP probably rate-flagged — raise SOLVER_SOLVE_DELAY, "
                    "use residential proxies, or fall through to capsolver/2captcha)"
                )
            finally:
                try:
                    await ctx.close()
                except Exception:  # noqa: BLE001
                    pass

    # -- proxy rotation ----------------------------------------------------

    def _consume_pinned(self) -> str:
        with self._pin_lock:
            proxy = self._pinned_proxy
            self._pinned_proxy = ""
            return proxy

    def _next_pool_proxy(self) -> str:
        """Return the next proxy from the pool, or ``""`` (direct)."""
        if not self._proxies:
            return ""
        if self._proxy_mode == "worker":
            cached = getattr(self._local, "proxy", None)
            if cached is not None:
                return cached
            with self._proxy_lock:
                proxy = self._proxies[self._proxy_idx % len(self._proxies)]
                self._proxy_idx += 1
            self._local.proxy = proxy
            return proxy
        with self._proxy_lock:
            proxy = self._proxies[self._proxy_idx % len(self._proxies)]
            self._proxy_idx += 1
            return proxy

    def _rotate_worker_proxy(self) -> str:
        """Advance a worker-mode caller to the next pool entry (used on retry)."""
        if not self._proxies:
            return ""
        with self._proxy_lock:
            proxy = self._proxies[self._proxy_idx % len(self._proxies)]
            self._proxy_idx += 1
        self._local.proxy = proxy
        return proxy

    # -- cooldown ----------------------------------------------------------

    def _apply_cooldown(self) -> None:
        if self._cooldown <= 0:
            return
        with self._cool_lock:
            wait = self._last_solve + self._cooldown - time.time()
            if wait > 0:
                time.sleep(wait)
            self._last_solve = time.time()

    # -- public solve ------------------------------------------------------

    def solve_turnstile(
        self,
        *,
        sitekey: str = TURNSTILE_SITEKEY,
        page_url: str = TURNSTILE_PAGE_URL,
        timeout: float = 120.0,
    ) -> str:
        if not sitekey or not page_url:
            raise SolverError("sitekey and page_url are required")

        # Conso-specific: ensure a whitelisted redirect_uri on the verify
        # page so the Next.js widget renders. (``with_redirect_uri`` could
        # be imported from .base but this inlined form keeps the hot path
        # tiny.)
        if (
            "redirect_uri=" not in page_url
            and "www.conso.xyz/verify-human" in page_url
        ):
            sep = "&" if "?" in page_url else "?"
            page_url = (
                f"{page_url}{sep}redirect_uri="
                f"{quote(CONSO_REDIRECT_URIS[0], safe='')}"
            )

        self._apply_cooldown()

        # Per-attempt budget: min(caller timeout, SOLVER_ATTEMPT_TIMEOUT).
        budget = int(max(10, min(timeout, self._attempt_timeout)))
        pinned = self._consume_pinned()
        last = ""

        def _one(proxy: str) -> str:
            return self._submit(
                self._solve_async(
                    page_url,
                    sitekey,
                    timeout_s=budget,
                    proxy=proxy or None,
                    real_page=self._cfg.real_page,
                ),
                timeout=budget + 45,
            )

        def _attempts_loop() -> str:
            nonlocal last
            for attempt in range(1, self._attempts + 1):
                if attempt == 1 and pinned:
                    proxy = pinned
                elif attempt == 1 or self._proxy_mode != "worker":
                    proxy = self._next_pool_proxy()
                else:
                    # Worker-mode retry: move to a genuinely different exit IP.
                    proxy = self._rotate_worker_proxy()
                try:
                    return _one(proxy)
                except Exception as exc:  # noqa: BLE001
                    last = str(exc)
                    if attempt >= self._attempts:
                        break
                    if "not installed" in last or "failed to start" in last:
                        break
                    time.sleep(self._backoff)
            raise SolverError(
                f"cloak turnstile solve failed after {self._attempts} attempt(s) "
                f"({budget}s each): {last}"
            )

        if self._serial:
            with _SOLVE_LOCK:
                return _attempts_loop()
        return _attempts_loop()

    # -- cf_clearance passthrough (available only when the cloak provider
    # is in the chain) ----------------------------------------------------

    def solve_cf_clearance(
        self,
        url: str,
        *,
        proxy: str | None = None,
        timeout_s: int = 60,
    ) -> dict:
        """Harvest a ``cf_clearance`` cookie by passing the full-page interstitial.

        Returns a dict shaped like the upstream endpoint:
        ``cf_clearance``, ``cookies``, ``user_agent``, ``headers``,
        ``proxy``, ``method``, ``elapsed``, ``warning`` (and ``success``).
        """
        # Lazy import to keep this module self-contained.
        from .cloudflare import solve_cf_clearance as _impl

        return self._submit(
            _impl(
                url,
                proxy=proxy or self._default_proxy,
                timeout_s=timeout_s,
                headless=self._cfg.headless,
                geoip=self._cfg.geoip,
            ),
            timeout=timeout_s + 60,
        )

    # -- teardown ----------------------------------------------------------

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

    def __enter__(self) -> "CloakTurnstileSolver":
        return self

    def __exit__(self, *args: object) -> None:
        self.close()


__all__ = [
    "CloakTurnstileSolver",
    "solve_and_verify",
    "solve_turnstile",
    "solve_turnstile_realpage",
]
