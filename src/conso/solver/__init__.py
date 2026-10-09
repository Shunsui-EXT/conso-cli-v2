"""
Pluggable captcha-solver package for the Conso CLI.

Public API (consumed by the rest of the CLI; keep this surface stable):

    from conso.solver import (
        build_solver,
        ensure_solver_ready,
        load_proxies,
        TURNSTILE_SITEKEY,
        TURNSTILE_PAGE_URL,
        CONSO_REDIRECT_URIS,
        CaptchaSolver,
        SolverError,
    )

Design (why this shape):

- A Conso Supabase auth call rejects stub-page Turnstile tokens
  (``invalid-input-response``), so the solver MUST drive the live
  ``verify-human`` page with one of the whitelisted ``redirect_uri``
  entries. ``CONSO_REDIRECT_URIS`` captures that whitelist.
- Providers form a comma-chain via the ``CAPTCHA_PROVIDER`` env (e.g.
  ``cloak,camoufox,capsolver``). Providers without the credentials they
  need silently return ``None`` and the chain skips them.
- Every browser-backed provider (``cloak``, ``camoufox``) imports its
  heavy dependency LAZILY — the package itself must import on a plain
  Python with no cloakbrowser/camoufox/playwright installed.
"""

from __future__ import annotations

from .base import (
    CONSO_REDIRECT_URIS,
    TURNSTILE_PAGE_URL,
    TURNSTILE_SITEKEY,
    CaptchaSolver,
    SolverError,
    load_proxies,
    with_redirect_uri,
)
from .chain import FallbackSolver, build_solver, ensure_solver_ready

__all__ = [
    "CONSO_REDIRECT_URIS",
    "TURNSTILE_PAGE_URL",
    "TURNSTILE_SITEKEY",
    "CaptchaSolver",
    "FallbackSolver",
    "SolverError",
    "build_solver",
    "ensure_solver_ready",
    "load_proxies",
    "with_redirect_uri",
]
