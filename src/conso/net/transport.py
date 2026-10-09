"""
Transport layer.

curl_cffi is used for TLS/JA3/HTTP2 fingerprint impersonation so requests to
the Conso backend carry a real Chrome TLS fingerprint rather than Python's
default (which is trivially flagged). A health-checked proxy pool with
automatic quarantine is layered on top.
"""

from __future__ import annotations

import itertools
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from curl_cffi import requests as cffi_requests

from ..core.config import Settings


@dataclass
class ProxyState:
    url: str
    failures: int = 0
    quarantined: bool = False
    last_checked: float = 0.0


@dataclass
class ProxyPool:
    """Round-robin proxy pool with failure-based quarantine."""

    urls: list[str]
    max_failures: int = 3
    health_check_url: str = "https://api.ipify.org?format=json"

    _states: list[ProxyState] = field(default_factory=list, init=False)
    _cycle: Any = field(default=None, init=False, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    def __post_init__(self) -> None:
        self._states = [ProxyState(url=u) for u in self.urls]
        self._cycle = itertools.cycle(self._states) if self._states else None

    @property
    def enabled(self) -> bool:
        return bool(self._states)

    def acquire(self) -> str | None:
        if not self._states:
            return None
        with self._lock:
            live = [s for s in self._states if not s.quarantined]
            if not live:
                # All quarantined: reset and retry from the top.
                for s in self._states:
                    s.quarantined = False
                    s.failures = 0
                live = list(self._states)
            state = live[time.monotonic_ns() % len(live)]
            return state.url

    def report(self, url: str | None, ok: bool) -> None:
        if not url:
            return
        with self._lock:
            for state in self._states:
                if state.url == url:
                    if ok:
                        state.failures = 0
                    else:
                        state.failures += 1
                        if state.failures >= self.max_failures:
                            state.quarantined = True
                    state.last_checked = time.time()
                    break

    def health_check(self, impersonate: str = "chrome") -> dict[str, bool]:
        results: dict[str, bool] = {}
        for state in self._states:
            try:
                resp = cffi_requests.get(
                    self.health_check_url,
                    proxies={"http": state.url, "https": state.url},
                    impersonate=impersonate,
                    timeout=15,
                )
                ok = resp.status_code == 200
            except Exception:
                ok = False
            self.report(state.url, ok)
            results[state.url] = ok
        return results


class Transport:
    """Fingerprinted HTTP client with retry + proxy rotation."""

    RETRY_STATUS = {429, 500, 502, 503, 504}

    def __init__(self, settings: Settings, *, pin_proxy: str | None = None) -> None:
        self.settings = settings
        self.pool = ProxyPool(
            urls=settings.proxy.urls,
            max_failures=settings.proxy.max_failures,
            health_check_url=settings.proxy.health_check_url,
        )
        # When set, every request uses this one proxy (sticky per account).
        self.pin_proxy = pin_proxy
        self._session = cffi_requests.Session(impersonate=settings.impersonate)
        if settings.user_agent:
            self._session.headers["User-Agent"] = settings.user_agent

    def _proxies(self) -> dict[str, str] | None:
        url = self.pin_proxy or self.pool.acquire()
        if not url:
            return None
        return {"http": url, "https": url}

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        json: Any = None,
        data: Any = None,
        retries: int | None = None,
        timeout: float | None = None,
        no_proxy: bool = False,
    ) -> cffi_requests.Response:
        attempts = (retries if retries is not None else self.settings.max_retries) + 1
        effective_timeout = timeout if timeout is not None else self.settings.request_timeout
        last_exc: Exception | None = None
        for attempt in range(attempts):
            proxies = None if no_proxy else self._proxies()
            try:
                resp = self._session.request(
                    method,
                    url,
                    headers=headers,
                    json=json,
                    data=data,
                    proxies=proxies,
                    timeout=effective_timeout,
                )
                if resp.status_code in self.RETRY_STATUS and attempt < attempts - 1:
                    self.pool.report(proxies["https"] if proxies else None, False)
                    time.sleep(min(2 ** attempt, 8))
                    continue
                self.pool.report(proxies["https"] if proxies else None, resp.status_code < 500)
                return resp
            except Exception as exc:  # network-level failure -> rotate + retry
                last_exc = exc
                self.pool.report(proxies["https"] if proxies else None, False)
                if attempt < attempts - 1:
                    time.sleep(min(2 ** attempt, 8))
                    continue
                raise
        raise last_exc if last_exc else RuntimeError("request failed")

    def close(self) -> None:
        self._session.close()
