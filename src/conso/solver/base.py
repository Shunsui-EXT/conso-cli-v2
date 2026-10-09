"""
Shared types, constants, and tiny helpers for the Conso solver package.

Everything here is dependency-free Python: no browser imports, no HTTP
clients. Providers in sibling modules import from here and bring their
own heavy deps lazily so this module stays safe to import anywhere.
"""

from __future__ import annotations

import os
from typing import Protocol, runtime_checkable
from urllib.parse import quote

# --- Conso-specific constants ------------------------------------------------
#
# The Supabase project behind www.conso.xyz enforces Cloudflare Turnstile on
# signup / password login / OTP. The sitekey is baked into the verify-human
# page; recon from the vendored waguriagentic/captcha-solver and the previous
# CONSO deployment both agree on this value.
TURNSTILE_SITEKEY = "0x4AAAAAAEzmjKoKI6TA61_6"
TURNSTILE_PAGE_URL = "https://www.conso.xyz/verify-human"

# Verify-human only renders the Turnstile widget when `redirect_uri` is one of
# these whitelist entries. Without a whitelisted redirect the Next.js widget
# never mounts, no token is produced, and solves hang until timeout. These
# values are kept in sync with conso.xyz's own list.
CONSO_REDIRECT_URIS: list[str] = [
    "https://bjibbmkefnaamkenamdppfengeepadpi.chromiumapp.org/",
    "https://mfjolkgcoehffnccojgdegniohfejfml.chromiumapp.org/",
    "consomobileapp://verify-human",
]


class SolverError(RuntimeError):
    """Base error raised by solver providers.

    Managed-API providers may raise plain ``RuntimeError`` / ``TimeoutError``;
    the chain treats any ``Exception`` as "try the next provider", so this
    class is mainly a hook for callers that want to disambiguate.
    """


@runtime_checkable
class CaptchaSolver(Protocol):
    """Minimal protocol every provider in the chain satisfies.

    ``solve_turnstile`` is BLOCKING by contract — the chain is driven from
    thread-based worker pools, so async-only providers wrap their coroutine
    through a dedicated loop thread (see ``cloak/turnstile.py``).
    """

    def solve_turnstile(
        self,
        *,
        sitekey: str = TURNSTILE_SITEKEY,
        page_url: str = TURNSTILE_PAGE_URL,
        timeout: float = 120.0,
    ) -> str: ...


# --- Helpers -----------------------------------------------------------------


def load_proxies(path: str) -> list[str]:
    """Load a proxy list (one URL per line); strip CRLF, blanks, and ``#`` lines.

    ``path`` is a filesystem path from ``SOLVER_PROXY_FILE``. Missing or empty
    files return ``[]`` so the caller can simply iterate.
    """
    if not path or not os.path.exists(path):
        return []
    out: list[str] = []
    with open(path, "r", encoding="utf-8", errors="ignore") as fh:
        for line in fh:
            entry = line.strip()
            if entry and not entry.startswith("#"):
                out.append(entry)
    return out


def with_redirect_uri(
    page_url: str,
    *,
    redirect_uri: str = CONSO_REDIRECT_URIS[0],
) -> str:
    """Append a whitelisted ``redirect_uri`` to a Conso verify-human URL.

    Idempotent: if the URL already carries ``redirect_uri=`` the string is
    returned unchanged. Only the Conso verify-human page needs this; a
    foreign page_url is passed through so the function is safe to call
    unconditionally from a solver.
    """
    if not page_url or "redirect_uri=" in page_url:
        return page_url
    if "www.conso.xyz/verify-human" not in page_url:
        return page_url
    sep = "&" if "?" in page_url else "?"
    return f"{page_url}{sep}redirect_uri={quote(redirect_uri, safe='')}"
