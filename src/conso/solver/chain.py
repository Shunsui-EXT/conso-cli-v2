"""
Ordered fallback across captcha providers + the env-driven factory.

``build_solver`` reads ``CAPTCHA_PROVIDER`` (comma-chain, e.g.
``cloak,camoufox,capsolver``) and returns either a single provider or a
``FallbackSolver`` wrapping the chain. Providers that need credentials and
don't have them (``capsolver`` without ``CAPSOLVER_API_KEY``) are silently
skipped, so a dev box with no API keys still boots.

Default when ``CAPTCHA_PROVIDER`` is unset: ``cloak,camoufox``. Both are
internal and both lazy-import their browser so a missing dep yields a
provider that raises only on first solve, not on import.
"""

from __future__ import annotations

import os
from typing import Any

from .base import (
    TURNSTILE_PAGE_URL,
    TURNSTILE_SITEKEY,
    CaptchaSolver,
    SolverError,
)
from .managed import (
    CapSolverSolver,
    CapsolverApiSolver,
    ManualSolver,
    NoopSolver,
    TwoCaptchaSolver,
)


# ---------------------------------------------------------------------------
# FallbackSolver
# ---------------------------------------------------------------------------


class FallbackSolver:
    """Try providers in order; first success wins.

    Mirrors the previous CONSO behaviour: a rate-flagged local IP degrades to
    a paid API instead of failing the whole registration. ``last_errors``
    exposes per-provider failure strings for logging/diagnostics.

    ``warm`` returns True on the first provider that reports it is ready
    (so warming the browser providers doesn't force an in-process launch of
    both when only one is needed).

    ``solve_cf_clearance`` is passed through to any provider that supports
    it (currently only the cloak provider); if none do, the call raises.
    """

    def __init__(self, solvers: list[CaptchaSolver]) -> None:
        self.solvers = [s for s in solvers if s is not None]
        self.last_errors: list[str] = []

    # Keep lint quiet on the heterogeneous solver list.
    def _attr(self, solver: Any, name: str) -> Any:
        return getattr(solver, name, None)

    def warm(self, *, timeout: float = 60.0) -> bool:
        for solver in self.solvers:
            warm = self._attr(solver, "warm")
            if callable(warm):
                try:
                    if warm(timeout=timeout):
                        return True
                except Exception:  # noqa: BLE001 - warm is best-effort
                    pass
        return False

    def warm_async(self) -> Any:
        """Fire ``warm_async`` on every provider that supports it.

        Returns a list of started threads (empty when no provider can warm);
        callers typically ignore it — warming is best-effort.
        """
        threads = []
        for solver in self.solvers:
            warm_async = self._attr(solver, "warm_async")
            if callable(warm_async):
                try:
                    t = warm_async()
                    if t is not None:
                        threads.append(t)
                except Exception:  # noqa: BLE001
                    pass
        return threads

    def pin_account_proxy(self, proxy: str) -> None:
        """Forward a sticky per-account proxy to every provider that accepts it.

        The in-process providers (``cloak``, ``camoufox``) use it to make the
        Turnstile solve share an exit IP with the signup HTTP calls. Managed
        APIs don't care and don't define the method, so we skip them.
        """
        for solver in self.solvers:
            pin = self._attr(solver, "pin_account_proxy")
            if callable(pin):
                try:
                    pin(proxy)
                except Exception:  # noqa: BLE001
                    pass

    def solve_turnstile(
        self,
        *,
        sitekey: str = TURNSTILE_SITEKEY,
        page_url: str = TURNSTILE_PAGE_URL,
        timeout: float = 120.0,
    ) -> str:
        self.last_errors = []
        if not self.solvers:
            raise SolverError("no captcha providers configured")
        for solver in self.solvers:
            try:
                return solver.solve_turnstile(
                    sitekey=sitekey, page_url=page_url, timeout=timeout
                )
            except Exception as exc:  # noqa: BLE001 - try next provider
                self.last_errors.append(f"{type(solver).__name__}: {exc}")
        raise SolverError(
            "all captcha providers failed -> " + " | ".join(self.last_errors)
        )

    def solve_cf_clearance(
        self,
        url: str,
        *,
        proxy: str | None = None,
        timeout_s: int = 60,
    ) -> dict:
        """Forward cf_clearance harvesting to the first provider that supports it."""
        for solver in self.solvers:
            fn = self._attr(solver, "solve_cf_clearance")
            if callable(fn):
                return fn(url, proxy=proxy, timeout_s=timeout_s)
        raise SolverError(
            "no provider in the chain supports solve_cf_clearance "
            "(enable the `cloak` provider)"
        )


# ---------------------------------------------------------------------------
# Env-driven factory
# ---------------------------------------------------------------------------
#
# NEVER evaluate env at import time: `_build_one` and `build_solver` do all
# their os.environ reads lazily so test runners can monkeypatch the env
# between calls.


def _provider_names(raw: str | None) -> list[str]:
    """Normalise a ``CAPTCHA_PROVIDER`` string into a list of provider names.

    - commas or pipes delimit
    - whitespace / case is normalised
    - the alias ``internal`` is folded to ``cloak`` (the user's explicit
      request: the new in-process port of waguriagentic/captcha-solver is
      THE internal provider going forward).
    """
    text = (raw or "").strip().lower()
    if not text:
        return []
    names = [p.strip() for p in text.replace("|", ",").split(",") if p.strip()]
    out: list[str] = []
    for name in names:
        if name == "internal":
            name = "cloak"
        if name not in out:
            out.append(name)
    return out


def _build_one(name: str, transport: Any) -> CaptchaSolver | None:
    """Instantiate a single provider; return ``None`` if config is missing."""
    if name == "cloak":
        # Lazy import so the solver package imports fine without cloakbrowser.
        from .cloak.turnstile import CloakTurnstileSolver

        return CloakTurnstileSolver()

    if name == "camoufox":
        from .camoufox import CamoufoxTurnstileSolver

        return CamoufoxTurnstileSolver()

    if name == "capsolver":
        key = os.environ.get("CAPSOLVER_API_KEY", "").strip()
        if not key or transport is None:
            return None
        if os.environ.get("CAPSOLVER_MODE", "api").strip().lower() == "sdk":
            return CapSolverSolver(key, transport)
        return CapsolverApiSolver(
            key, transport, proxy=os.environ.get("CAPSOLVER_PROXY", "")
        )

    if name == "2captcha":
        key = os.environ.get("TWOCAPTCHA_API_KEY", "").strip()
        if not key or transport is None:
            return None
        return TwoCaptchaSolver(key, transport)

    if name == "manual":
        return ManualSolver()

    if name == "none":
        return NoopSolver(os.environ.get("CAPTCHA_TOKEN", ""))

    # Unknown name: skip rather than crash — the chain may still work with
    # the remaining providers.
    return None


def build_solver(transport: Any | None = None) -> CaptchaSolver:
    """Env-driven solver factory.

    - ``CAPTCHA_PROVIDER`` comma-chain; default ``cloak,camoufox``.
    - Providers that need credentials and don't have them are skipped.
    - A single resulting provider is returned directly; two or more are
      wrapped in a ``FallbackSolver``.
    - If the chain is empty, falls back to ``NoopSolver`` with
      ``CAPTCHA_TOKEN`` — raises on first solve if no token is set.
    """
    names = _provider_names(os.environ.get("CAPTCHA_PROVIDER")) or ["cloak", "camoufox"]
    solvers: list[CaptchaSolver] = []
    for name in names:
        solver = _build_one(name, transport)
        if solver is not None:
            solvers.append(solver)
    if not solvers:
        return NoopSolver(os.environ.get("CAPTCHA_TOKEN", ""))
    if len(solvers) == 1:
        return solvers[0]
    return FallbackSolver(solvers)


# ---------------------------------------------------------------------------
# Warm-up
# ---------------------------------------------------------------------------


def ensure_solver_ready(*, logger: Any = None) -> bool:
    """Warm the in-process providers listed in ``CAPTCHA_PROVIDER``.

    This never raises. Managed APIs and the Noop/Manual providers have
    nothing to start, so for a chain of only those we return True
    immediately. For ``cloak`` / ``camoufox`` we try to launch the browser
    now so the first registration doesn't pay the launch latency.
    """

    def _log(msg: str) -> None:
        if logger:
            try:
                logger(msg)
            except Exception:  # noqa: BLE001
                pass

    names = _provider_names(os.environ.get("CAPTCHA_PROVIDER")) or ["cloak", "camoufox"]
    needs_browser = {"cloak", "camoufox"} & set(names)
    if not needs_browser:
        return True

    any_warm = False
    for name in names:
        try:
            if name == "cloak":
                from .cloak.turnstile import CloakTurnstileSolver

                solver = CloakTurnstileSolver()
                _log("solver: warming CloakBrowser ...")
                if solver.warm(timeout=90):
                    _log("solver: CloakBrowser ready")
                    any_warm = True
                else:
                    _log("solver: CloakBrowser warm failed (will retry on solve)")
            elif name == "camoufox":
                from .camoufox import CamoufoxTurnstileSolver

                solver = CamoufoxTurnstileSolver()
                _log("solver: warming Camoufox ...")
                if solver.warm(timeout=90):
                    _log("solver: Camoufox ready")
                    any_warm = True
                else:
                    _log("solver: Camoufox warm failed (will retry on solve)")
        except Exception as exc:  # noqa: BLE001 - warm is best-effort
            _log(f"solver: {name} warm error: {exc}")

    # Even if neither warm succeeded, don't fail the whole run — the solve
    # path will try again and raise its own error with specific detail.
    return any_warm or False
