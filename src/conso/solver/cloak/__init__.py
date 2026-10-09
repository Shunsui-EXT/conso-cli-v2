"""
In-process CloakBrowser-backed captcha solver.

This is the port requested by the user: the open code from
``waguriagentic/captcha-solver`` (turnstile/, cloudflare/, common/) is lifted
into this subpackage as an in-process library — no FastAPI, no :8877
sidecar, no SSRF guard. ``CloakTurnstileSolver`` + ``CloakCloudflareSolver``
drive CloakBrowser directly through a dedicated asyncio-loop thread.

See ``NOTICE.md`` for the attribution + licensing status.
"""

from __future__ import annotations

from .cloudflare import CloakCloudflareSolver, solve_cf_clearance
from .turnstile import (
    CloakTurnstileSolver,
    solve_and_verify,
    solve_turnstile,
    solve_turnstile_realpage,
)

__all__ = [
    "CloakCloudflareSolver",
    "CloakTurnstileSolver",
    "solve_and_verify",
    "solve_cf_clearance",
    "solve_turnstile",
    "solve_turnstile_realpage",
]
