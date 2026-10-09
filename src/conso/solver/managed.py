"""
Managed / out-of-process captcha providers.

These share one trait: no local browser. The chain uses them as paid-API
fallbacks behind the in-process providers (``cloak``, ``camoufox``).

Transports:
    All HTTP providers accept a ``transport`` parameter — a lightweight
    object exposing ``request(method, url, *, json=..., data=..., headers=...,
    retries=..., timeout=..., no_proxy=...)`` returning an object with
    ``.status_code`` / ``.text`` / ``.json()``.  The project's ``Transport``
    class in ``conso.net.transport`` is the production implementation; tests
    swap in a fake. ``transport`` is kept ``Any`` here so this module does
    not import the project's transport (which belongs to another session).
"""

from __future__ import annotations

import time
from typing import Any

from .base import (
    TURNSTILE_PAGE_URL,
    TURNSTILE_SITEKEY,
    CaptchaSolver,
    SolverError,
)


# ---------------------------------------------------------------------------
# NoopSolver
# ---------------------------------------------------------------------------


class NoopSolver:
    """Returns a configured static token.

    Used when the caller supplies a token manually (``CAPTCHA_TOKEN`` env)
    or when the chain is empty and we need a last-resort "do nothing"
    provider. Raises if no token is configured — that way the chain's
    error message surfaces the real problem instead of silently returning
    an empty string.
    """

    def __init__(self, token: str = "") -> None:
        self.token = token

    def solve_turnstile(
        self,
        *,
        sitekey: str = TURNSTILE_SITEKEY,
        page_url: str = TURNSTILE_PAGE_URL,
        timeout: float = 120.0,
    ) -> str:
        if not self.token:
            raise SolverError(
                "NoopSolver has no token configured (set CAPTCHA_TOKEN or add "
                "a working provider to CAPTCHA_PROVIDER)."
            )
        return self.token


# ---------------------------------------------------------------------------
# CapSolver (two flavours: SDK-style createTask/getTaskResult, pure HTTP API)
# ---------------------------------------------------------------------------


class CapSolverSolver:
    """CapSolver anti-turnstile via createTask / getTaskResult, proxyless.

    Kept because the vendored CONSO deployment named its "SDK style" variant
    this way. Picked by ``CAPSOLVER_MODE=sdk``.
    """

    BASE = "https://api.capsolver.com"

    def __init__(self, api_key: str, transport: Any) -> None:
        self.api_key = api_key
        self.transport = transport

    def solve_turnstile(
        self,
        *,
        sitekey: str = TURNSTILE_SITEKEY,
        page_url: str = TURNSTILE_PAGE_URL,
        timeout: float = 120.0,
    ) -> str:
        task = {
            "clientKey": self.api_key,
            "task": {
                "type": "AntiTurnstileTaskProxyLess",
                "websiteURL": page_url,
                "websiteKey": sitekey,
            },
        }
        created = self.transport.request(
            "POST", f"{self.BASE}/createTask", json=task
        ).json()
        if created.get("errorId"):
            raise SolverError(f"capsolver createTask: {created.get('errorDescription')}")
        task_id = created["taskId"]

        deadline = time.time() + timeout
        while time.time() < deadline:
            time.sleep(3)
            result = self.transport.request(
                "POST",
                f"{self.BASE}/getTaskResult",
                json={"clientKey": self.api_key, "taskId": task_id},
            ).json()
            if result.get("errorId"):
                raise SolverError(
                    f"capsolver getTaskResult: {result.get('errorDescription')}"
                )
            if result.get("status") == "ready":
                token = (result.get("solution") or {}).get("token")
                if not token:
                    raise SolverError("capsolver returned no token")
                return token
        raise TimeoutError("capsolver turnstile solve timed out")


class CapsolverApiSolver:
    """CapSolver managed API (pure HTTP) — picked by default (``CAPSOLVER_MODE=api``).

    Difference from ``CapSolverSolver``: uses retries=1 on the transport calls
    (matches the measured production config) and supports an optional
    ``proxy`` that promotes the task to ``AntiTurnstileTask`` with proxy auth.
    """

    BASE = "https://api.capsolver.com"

    def __init__(
        self,
        api_key: str,
        transport: Any,
        *,
        sitekey: str = TURNSTILE_SITEKEY,
        page_url: str = TURNSTILE_PAGE_URL,
        proxy: str = "",
    ) -> None:
        self.api_key = api_key
        self.transport = transport
        self.sitekey = sitekey
        self.page_url = page_url
        self.proxy = proxy

    def solve_turnstile(
        self,
        *,
        sitekey: str = TURNSTILE_SITEKEY,
        page_url: str = TURNSTILE_PAGE_URL,
        timeout: float = 120.0,
    ) -> str:
        task: dict[str, Any] = {
            "type": "AntiTurnstileTaskProxyLess",
            "websiteURL": page_url,
            "websiteKey": sitekey,
        }
        if self.proxy:
            task["type"] = "AntiTurnstileTask"
            task["proxy"] = self.proxy
        created = self.transport.request(
            "POST",
            f"{self.BASE}/createTask",
            json={"clientKey": self.api_key, "task": task},
            retries=1,
        ).json()
        if created.get("errorId"):
            raise SolverError(f"capsolver createTask: {created.get('errorDescription')}")
        task_id = created.get("taskId")
        if not task_id:
            raise SolverError(f"capsolver createTask: no taskId ({created})")

        deadline = time.time() + timeout
        while time.time() < deadline:
            time.sleep(3)
            result = self.transport.request(
                "POST",
                f"{self.BASE}/getTaskResult",
                json={"clientKey": self.api_key, "taskId": task_id},
                retries=1,
            ).json()
            if result.get("errorId"):
                raise SolverError(
                    f"capsolver getTaskResult: {result.get('errorDescription')}"
                )
            if result.get("status") == "ready":
                token = (result.get("solution") or {}).get("token")
                if not token:
                    raise SolverError("capsolver returned no token")
                return token
        raise TimeoutError("capsolver turnstile solve timed out")


# ---------------------------------------------------------------------------
# 2Captcha
# ---------------------------------------------------------------------------


class TwoCaptchaSolver:
    """2Captcha turnstile via in.php / res.php polling."""

    BASE = "https://2captcha.com"

    def __init__(self, api_key: str, transport: Any) -> None:
        self.api_key = api_key
        self.transport = transport

    def solve_turnstile(
        self,
        *,
        sitekey: str = TURNSTILE_SITEKEY,
        page_url: str = TURNSTILE_PAGE_URL,
        timeout: float = 120.0,
    ) -> str:
        params = {
            "key": self.api_key,
            "method": "turnstile",
            "sitekey": sitekey,
            "pageurl": page_url,
            "json": 1,
        }
        created = self.transport.request(
            "POST", f"{self.BASE}/in.php", data=params
        ).json()
        if created.get("status") != 1:
            raise SolverError(f"2captcha in.php: {created.get('request')}")
        task_id = created["request"]

        deadline = time.time() + timeout
        while time.time() < deadline:
            time.sleep(5)
            result = self.transport.request(
                "GET",
                f"{self.BASE}/res.php",
                data={"key": self.api_key, "action": "get", "id": task_id, "json": 1},
            ).json()
            if result.get("status") == 1:
                return result["request"]
            if result.get("request") != "CAPCHA_NOT_READY":
                raise SolverError(f"2captcha res.php: {result.get('request')}")
        raise TimeoutError("2captcha turnstile solve timed out")


# ---------------------------------------------------------------------------
# Manual (operator pastes a token)
# ---------------------------------------------------------------------------


class ManualSolver:
    """Prompt the operator to open the URL and paste a token."""

    def solve_turnstile(
        self,
        *,
        sitekey: str = TURNSTILE_SITEKEY,
        page_url: str = TURNSTILE_PAGE_URL,
        timeout: float = 120.0,
    ) -> str:
        print(
            f"Open {page_url} , solve the Turnstile (sitekey {sitekey}), "
            f"paste the token:"
        )
        return input("captcha_token> ").strip()


__all__: list[str] = [
    "CapSolverSolver",
    "CapsolverApiSolver",
    "ManualSolver",
    "NoopSolver",
    "TwoCaptchaSolver",
    "CaptchaSolver",
]
