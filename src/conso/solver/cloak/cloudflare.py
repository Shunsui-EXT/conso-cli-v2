"""
Harvest a Cloudflare ``cf_clearance`` cookie by passing the interstitial.

Direct port of ``cloudflare/solve.py`` from waguriagentic/captcha-solver,
converted into an in-process library module. Two paths covered by one
code path:

- Managed Challenge: a Turnstile checkbox inside the
  ``challenges.cloudflare.com`` iframe. Reuses the humanized page-level
  click helper from ``turnstile.py``.
- JS Challenge: "Checking your browser…" that auto-resolves.

Either way we poll ``page.context.cookies()`` until ``cf_clearance``
appears AND the interstitial markers are gone, bounded by ``timeout_s``.

Replay contract
---------------
``cf_clearance`` is bound to IP + JA3/TLS + User-Agent + the specific
challenge. The returned dict carries ``cookies`` + ``user_agent`` +
``proxy`` + a ``warning`` string so callers can reproduce the replay
rules — they MUST use the same proxy IP, the same UA, and a TLS stack
with matching JA3 (``curl_cffi`` impersonation or another CloakBrowser).
Plain ``requests`` / ``httpx`` / ``curl`` get re-challenged even with
the right IP+UA.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from concurrent.futures import Future
from typing import Any, Coroutine

from ..base import SolverError
from .browser import build_browser_kwargs, run_post_fetch, run_pre_actions

log = logging.getLogger(__name__)

_solve_lock = asyncio.Lock()


# One JS probe covers both Managed and JS Challenge interstitials. The
# challenge-form + ``_cf_chl_opt`` blob appear on both variants; the
# turnstile iframe is Managed-only.
_CF_MARKERS_JS = r"""() => {
  const html = document.documentElement.outerHTML;
  const t = (document.title || '').toLowerCase();
  return (
    !!document.querySelector('#challenge-form, form#challenge-form') ||
    !!document.querySelector('#cf-wrapper, .cf-browser-verification, #challenge-running, #trk_jschal_js') ||
    !!document.querySelector('iframe[src*="challenges.cloudflare.com"]') ||
    /window\._cf_chl_opt|__cf_chl_/.test(html) ||
    /just a moment|attention required|checking your browser|verifying you are human/.test(t)
  );
}"""


def _clearance(cookies: list) -> dict | None:
    """Return the ``cf_clearance`` cookie record or ``None``."""
    return next((c for c in cookies if c.get("name") == "cf_clearance"), None)


async def _is_interstitial(page: Any) -> bool:
    try:
        return bool(await page.evaluate(_CF_MARKERS_JS))
    except Exception:  # noqa: BLE001 - mid-navigation; cookie poll decides
        return False


async def _launch_browser(**kw: Any) -> Any:
    try:
        import cloakbrowser  # type: ignore[import-untyped]
    except ImportError as exc:
        raise SolverError(
            "cloakbrowser is not installed (pip install cloakbrowser && "
            "python -m playwright install chromium)"
        ) from exc
    return await cloakbrowser.launch_async(**kw)


async def solve_cf_clearance(
    url: str,
    *,
    proxy: str | None = None,
    timeout_s: int = 60,
    pre_actions: list | None = None,
    post_fetch: list | None = None,
    headless: bool = True,
    geoip: bool = False,
) -> dict:
    """Navigate ``url``, pass the Cloudflare interstitial, return cf_clearance.

    Returns a dict shaped like the upstream endpoint — see module docstring
    for the replay contract.
    """
    t0 = time.monotonic()
    kw = build_browser_kwargs(headless=headless, geoip=geoip, proxy=proxy)

    # Lazy import of the humanized click helper so this module does not
    # pull cloakbrowser transitively at import time.
    from .turnstile import _click_turnstile_checkbox

    async with _solve_lock:
        async with await _launch_browser(**kw) as browser:
            page = await browser.new_page()
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=45_000)
                if pre_actions:
                    await run_pre_actions(page, pre_actions)

                # Managed path: best-effort humanized checkbox click.
                # JS Challenge: no-op (cookie appears from the JS alone).
                try:
                    await _click_turnstile_checkbox(page, attempts=8)
                except Exception:  # noqa: BLE001 - swallow nav/race errors
                    pass

                clg: dict | None = None
                cookies: list = []
                deadline = time.monotonic() + timeout_s
                while time.monotonic() < deadline:
                    cookies = await page.context.cookies()
                    clg = _clearance(cookies)
                    if clg and not await _is_interstitial(page):
                        break
                    if clg:  # cookie set; let the final navigation settle
                        await asyncio.sleep(1)
                        cookies = await page.context.cookies()
                        clg = _clearance(cookies)
                        break
                    await asyncio.sleep(1)

                ua = await page.evaluate("() => navigator.userAgent")
                lang = await page.evaluate("() => navigator.language")
                result: dict = {
                    "cf_clearance": clg,
                    "success": bool(clg),
                    "cookies": cookies,
                    "user_agent": ua,
                    "headers": {"User-Agent": ua, "Accept-Language": lang},
                    "proxy": proxy or None,
                    "method": "interstitial",
                    "elapsed": round(time.monotonic() - t0, 1),
                    "warning": (
                        "cf_clearance is bound to IP + JA3/TLS + User-Agent. "
                        "Replay ONLY from the same proxy IP, with this exact "
                        "User-Agent, over a TLS stack producing the same JA3."
                    ),
                }
                if not clg:
                    result["error"] = "cf_clearance not set (challenge unsolved)"
                if post_fetch and clg:
                    result["post_fetch"] = await run_post_fetch(
                        page, post_fetch, clg["value"]
                    )
                return result
            finally:
                await page.close()


# ---------------------------------------------------------------------------
# Blocking sync wrapper
# ---------------------------------------------------------------------------
#
# This mirrors the pattern of ``CloakTurnstileSolver`` without sharing a
# browser — cf_clearance harvests fire a fresh browser each call because
# the cookie is bound to that session's TLS fingerprint and we want a
# clean one. For bulk harvesting a single long-lived browser can be shared
# by passing the solver into a loop; keep this simple for now.


class CloakCloudflareSolver:
    """In-process cf_clearance harvester (one browser per harvest)."""

    def __init__(
        self,
        *,
        headless: bool | None = None,
        geoip: bool | None = None,
        timeout_seconds: int | None = None,
        proxy: str | None = None,
    ) -> None:
        if headless is None:
            headless = os.environ.get(
                "CLOAK_HEADLESS", os.environ.get("SOLVER_HEADLESS", "1")
            ) != "0"
        if geoip is None:
            geoip = os.environ.get("CLOAK_GEOIP", "0") != "0"
        if timeout_seconds is None:
            timeout_seconds = int(os.environ.get("SOLVER_TIMEOUT", "60") or 60)

        self.headless = bool(headless)
        self.geoip = bool(geoip)
        self.timeout_seconds = max(10, int(timeout_seconds))
        self.proxy = proxy

        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._loop_lock = threading.Lock()
        self._loop_ready = threading.Event()
        self._closed = False

    # -- loop plumbing -----------------------------------------------------

    def _start_loop(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        with self._loop_lock:
            if self._thread and self._thread.is_alive():
                return
            self._loop_ready.clear()
            self._thread = threading.Thread(
                target=self._loop_main,
                name="conso-cloak-cf-clearance",
                daemon=True,
            )
            self._thread.start()
            if not self._loop_ready.wait(timeout=15):
                raise SolverError("cf_clearance event loop failed to start")

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
            raise SolverError("cf_clearance solver is closed")
        self._start_loop()
        if self._loop is None:
            raise SolverError("cf_clearance event loop unavailable")
        future: Future[Any] = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result(timeout=timeout)

    # -- public solve ------------------------------------------------------

    def solve_cf_clearance(
        self,
        url: str,
        *,
        proxy: str | None = None,
        timeout_s: int | None = None,
        pre_actions: list | None = None,
        post_fetch: list | None = None,
    ) -> dict:
        timeout = int(timeout_s or self.timeout_seconds)
        return self._submit(
            solve_cf_clearance(
                url,
                proxy=proxy or self.proxy,
                timeout_s=timeout,
                pre_actions=pre_actions,
                post_fetch=post_fetch,
                headless=self.headless,
                geoip=self.geoip,
            ),
            timeout=timeout + 60,
        )

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._loop and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._loop.stop)
            if self._thread:
                self._thread.join(timeout=5)

    def __enter__(self) -> "CloakCloudflareSolver":
        return self

    def __exit__(self, *args: object) -> None:
        self.close()


__all__ = ["CloakCloudflareSolver", "solve_cf_clearance"]
