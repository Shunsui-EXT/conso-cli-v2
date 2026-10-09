"""
Browser helpers for the CloakBrowser-backed solvers.

Direct port of ``common/browser.py`` from waguriagentic/captcha-solver, with
one meaningful change: ``browser_kwargs`` is NOT env-driven. The sidecar
project resolved headless/geoip from ``BROWSER_HEADLESS`` and
``{PREFIX}_GEOIP``; this port takes them as explicit arguments so the
in-process solver's config lives in one place (``CloakTurnstileSolver``).

Injection-safety is preserved verbatim: every caller value reaches the
browser as an ``evaluate()`` argument, never interpolated into JS source.

Public surface
--------------
    resolve_selector(page, selector, timeout) -> Playwright locator
    run_pre_actions(page, actions)             -> run click/fill/select/press/wait
    build_browser_kwargs(*, headless, geoip, proxy, humanize) -> dict
    route_glob(url) -> str                     -> page.route pattern
    fetch_from_page(page, url, method, body)   -> {status, body}
    run_post_fetch(page, post_fetch, token)    -> [{url,status,body}, ...]
    parse_proxy(proxy_url) -> Playwright-style proxy dict | None
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any
from urllib.parse import urlparse, urlsplit

log = logging.getLogger(__name__)


# Parameterized fetch: caller values (url, method, body) arrive as
# evaluate() args, NEVER interpolated into JS source — safe against
# quotes/injection.
_FETCH_JS = (
    "async ({u, m, b}) => { try {"
    " const r = await fetch(u, {method: m,"
    " headers: {'Content-Type': 'application/json'},"
    " body: b, credentials: 'include'});"
    " return {status: r.status, body: await r.text()};"
    " } catch(e) { return {status: 0, body: String(e)}; } }"
)


# ---------------------------------------------------------------------------
# Selector + action helpers
# ---------------------------------------------------------------------------


async def resolve_selector(page: Any, selector: str, timeout: int = 10000) -> Any:
    """Resolve a selector string to a Playwright locator.

    Supports:
      - CSS (default): ``input[type=email]``
      - XPath:         ``//button[@type='submit']``
      - Text:          ``text=Continue with Email``
      - Regex:         ``regex=Continue.*Email``
      - Role:          ``role=button[name='Submit']``
    """
    if selector.startswith("//") or selector.startswith("(//"):
        return page.locator(f"xpath={selector}")
    if selector.startswith("text="):
        return page.get_by_text(selector[5:], exact=False)
    if selector.startswith("regex="):
        return page.locator(f"text=/{selector[6:]}/")
    if selector.startswith("role="):
        m = re.match(r"role=(\w+)(?:\[name=['\"](.+?)['\"]\])?", selector)
        if m:
            role, name = m.group(1), m.group(2)
            kwargs = {"name": name} if name else {}
            return page.get_by_role(role, **kwargs)
    return page.locator(selector)


async def run_pre_actions(page: Any, actions: list) -> None:
    """Execute a list of pre-actions before solving.

    Each action: ``{"type": "click|fill|select|press|wait", "selector": "...",
    "value": "...", "timeout": N}``.
    """
    for i, action in enumerate(actions):
        atype = action.get("type", "")
        selector = action.get("selector", "")
        value = action.get("value", "")
        timeout = action.get("timeout", 10000)
        log.info("Pre-action %d: %s %s", i + 1, atype, (selector or "")[:60])
        if atype == "click":
            loc = await resolve_selector(page, selector, timeout)
            await loc.click(timeout=timeout)
        elif atype == "fill":
            loc = await resolve_selector(page, selector, timeout)
            await loc.fill(value, timeout=timeout)
        elif atype == "select":
            loc = await resolve_selector(page, selector, timeout)
            await loc.select_option(value, timeout=timeout)
        elif atype == "press":
            await page.keyboard.press(value)
        elif atype == "wait":
            await asyncio.sleep(float(value or 1))
        else:
            log.warning("Unknown pre-action type: %s", atype)
        await asyncio.sleep(0.5)


# ---------------------------------------------------------------------------
# Browser kwargs + proxy parsing
# ---------------------------------------------------------------------------


def build_browser_kwargs(
    *,
    headless: bool = True,
    geoip: bool = False,
    proxy: str | None = None,
    humanize: bool = True,
) -> dict:
    """Build the kwargs dict for ``cloakbrowser.launch_async(**kw)``.

    Replaces the upstream ``browser_kwargs(prefix, proxy)`` which read env
    vars. In this port the solver's constructor owns config, so this
    helper takes explicit values.

    ``proxy`` is a URL (``http://user:pass@host:port``); when set it is
    forwarded as-is because CloakBrowser accepts a URL string.
    """
    kw: dict[str, Any] = {"humanize": humanize, "headless": headless}
    if proxy:
        kw["proxy"] = proxy
    if geoip:
        kw["geoip"] = True
    return kw


def parse_proxy(proxy_url: str | None) -> dict[str, str] | None:
    """Parse ``http://user:pass@host:port`` into a Playwright proxy dict.

    Returns ``None`` when ``proxy_url`` is empty. Used when the caller wants
    a Playwright proxy dict instead of CloakBrowser's URL form (e.g. for
    per-context overrides).
    """
    if not proxy_url:
        return None
    parsed = urlparse(proxy_url)
    if not parsed.scheme or not parsed.hostname or not parsed.port:
        raise ValueError(f"invalid proxy URL: {proxy_url}")
    out: dict[str, str] = {
        "server": f"{parsed.scheme}://{parsed.hostname}:{parsed.port}"
    }
    if parsed.username:
        out["username"] = parsed.username
    if parsed.password:
        out["password"] = parsed.password
    return out


# ---------------------------------------------------------------------------
# Route glob + session fetch
# ---------------------------------------------------------------------------


def route_glob(url: str) -> str:
    """Return the ``page.route()`` pattern for ``url``.

    ``goto("https://ex.com")`` actually requests ``https://ex.com/`` so a bare
    URL used as a route pattern silently misses and the solve hangs. Appending
    ``/**`` matches bare, trailing-slash, and deeper-path forms. URLs that
    already carry a path pass through unchanged.
    """
    parts = urlsplit(url)
    if parts.path in ("", "/"):
        return f"{parts.scheme}://{parts.netloc}/**"
    return url


async def fetch_from_page(page: Any, url: str, method: str, body: str) -> dict:
    """One parameterized fetch from the page's session. Returns ``{status, body}``."""
    return await page.evaluate(_FETCH_JS, {"u": url, "m": method.upper(), "b": body})


async def run_post_fetch(page: Any, post_fetch: list, token: str) -> list:
    """Fire post-solve API calls from the SAME browser session.

    Each entry: ``{"url": ..., "method": "POST", "body": {...}}``. ``__TOKEN__``
    in the JSON body is replaced with the solved token. All values flow
    through ``evaluate`` args (injection-safe).
    """
    results: list = []
    for pf in post_fetch:
        pf_url = pf["url"]
        pf_method = pf.get("method", "POST")
        body_str = json.dumps(pf.get("body", {})).replace("__TOKEN__", token)
        try:
            fr = await fetch_from_page(page, pf_url, pf_method, body_str)
            log.info("post_fetch %s: %d", pf_url, fr["status"])
            results.append({"url": pf_url, **fr})
        except Exception as exc:  # noqa: BLE001 - record failures per entry
            log.warning("post_fetch %s failed: %s", pf_url, exc)
            results.append({"url": pf_url, "status": 0, "body": str(exc)})
    return results


__all__ = [
    "build_browser_kwargs",
    "fetch_from_page",
    "parse_proxy",
    "resolve_selector",
    "route_glob",
    "run_post_fetch",
    "run_pre_actions",
]
