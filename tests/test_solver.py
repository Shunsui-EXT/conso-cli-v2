"""
Offline tests for the Conso solver package.

NO browser launches, NO network calls — these must run on a plain Python
with no cloakbrowser / camoufox / playwright installed.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Match the sibling tests style: inject the project's ``src`` on sys.path so
# ``conso.solver`` imports without a package install.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from conso.solver import (  # noqa: E402
    CONSO_REDIRECT_URIS,
    TURNSTILE_PAGE_URL,
    TURNSTILE_SITEKEY,
    FallbackSolver,
    SolverError,
    build_solver,
    ensure_solver_ready,
    load_proxies,
    with_redirect_uri,
)
from conso.solver.base import CaptchaSolver  # noqa: E402
from conso.solver.chain import _provider_names  # noqa: E402
from conso.solver.managed import (  # noqa: E402
    CapsolverApiSolver,
    CapSolverSolver,
    ManualSolver,
    NoopSolver,
    TwoCaptchaSolver,
)


# ---------------------------------------------------------------------------
# Public constants
# ---------------------------------------------------------------------------


def test_sitekey_and_page_url_are_the_conso_values() -> None:
    """The sitekey / page_url are documented product data; pin them."""
    assert TURNSTILE_SITEKEY == "0x4AAAAAAEzmjKoKI6TA61_6"
    assert TURNSTILE_PAGE_URL == "https://www.conso.xyz/verify-human"


def test_conso_redirect_uris_whitelist_has_three_entries() -> None:
    """Verify-human only renders with a whitelisted redirect_uri."""
    assert CONSO_REDIRECT_URIS[0].startswith("https://bjibbmkefnaamkenamdppfengeepadpi")
    assert "chromiumapp.org" in CONSO_REDIRECT_URIS[1]
    assert CONSO_REDIRECT_URIS[-1] == "consomobileapp://verify-human"


# ---------------------------------------------------------------------------
# Fakes + fallback behaviour
# ---------------------------------------------------------------------------


class _FailSolver:
    def __init__(self, msg: str) -> None:
        self.msg = msg

    def solve_turnstile(self, *, sitekey=TURNSTILE_SITEKEY,
                        page_url=TURNSTILE_PAGE_URL, timeout=120.0) -> str:
        raise RuntimeError(self.msg)


class _TokenSolver:
    def __init__(self, token: str) -> None:
        self.token = token
        self.calls = 0

    def solve_turnstile(self, *, sitekey=TURNSTILE_SITEKEY,
                        page_url=TURNSTILE_PAGE_URL, timeout=120.0) -> str:
        self.calls += 1
        return self.token


class _WarmSolver(_TokenSolver):
    def __init__(self, token: str) -> None:
        super().__init__(token)
        self.warmed = False

    def warm(self, *, timeout: float = 60.0) -> bool:
        self.warmed = True
        return True


def test_fallback_returns_first_success() -> None:
    good = _TokenSolver("TOK")
    chain = FallbackSolver([_FailSolver("boom"), good])
    assert chain.solve_turnstile() == "TOK"
    assert good.calls == 1
    # ``last_errors`` records the failures that preceded the hit.
    assert chain.last_errors == ["_FailSolver: boom"]


def test_fallback_raises_when_all_fail() -> None:
    chain = FallbackSolver([_FailSolver("a"), _FailSolver("b")])
    with pytest.raises(SolverError) as exc:
        chain.solve_turnstile()
    assert "a" in str(exc.value) and "b" in str(exc.value)


def test_fallback_empty_raises_solver_error() -> None:
    chain = FallbackSolver([])
    with pytest.raises(SolverError):
        chain.solve_turnstile()


def test_fallback_warm_short_circuits_on_first_success() -> None:
    warm_a = _WarmSolver("A")
    warm_b = _WarmSolver("B")
    chain = FallbackSolver([warm_a, warm_b])
    assert chain.warm() is True
    assert warm_a.warmed is True
    assert warm_b.warmed is False  # short-circuited


def test_fallback_pin_proxy_forwards_to_providers_that_accept_it() -> None:
    class Pinnable:
        def __init__(self) -> None:
            self.pinned = ""

        def pin_account_proxy(self, proxy: str) -> None:
            self.pinned = proxy

        def solve_turnstile(self, *, sitekey=TURNSTILE_SITEKEY,
                            page_url=TURNSTILE_PAGE_URL, timeout=120.0) -> str:
            return ""

    p = Pinnable()
    chain = FallbackSolver([p, _TokenSolver("x")])
    chain.pin_account_proxy("http://u:p@1.2.3.4:9000")
    assert p.pinned == "http://u:p@1.2.3.4:9000"


def test_fallback_cf_clearance_raises_when_no_provider_supports_it() -> None:
    chain = FallbackSolver([_TokenSolver("x")])
    with pytest.raises(SolverError):
        chain.solve_cf_clearance("https://example.com")


def test_fallback_cf_clearance_forwards_to_first_capable_provider() -> None:
    class CfSolver:
        def __init__(self) -> None:
            self.last_url = ""

        def solve_turnstile(self, *, sitekey=TURNSTILE_SITEKEY,
                            page_url=TURNSTILE_PAGE_URL, timeout=120.0) -> str:
            return ""

        def solve_cf_clearance(self, url, *, proxy=None, timeout_s=60):
            self.last_url = url
            return {"cf_clearance": {"value": "C"}, "success": True}

    cf = CfSolver()
    chain = FallbackSolver([_TokenSolver("x"), cf])
    out = chain.solve_cf_clearance("https://ex.com")
    assert out["success"] is True
    assert cf.last_url == "https://ex.com"


# ---------------------------------------------------------------------------
# Env chain parsing (``CAPTCHA_PROVIDER``)
# ---------------------------------------------------------------------------


def test_provider_names_default_and_folding() -> None:
    # ``internal`` is an alias for ``cloak`` (user's explicit decision).
    assert _provider_names("internal,capsolver") == ["cloak", "capsolver"]
    # Pipes also work as a delimiter.
    assert _provider_names("cloak|camoufox") == ["cloak", "camoufox"]
    # Case + whitespace normalised, duplicates de-duped.
    assert _provider_names(" CLOAK, cloak ,2captcha") == ["cloak", "2captcha"]
    # Empty / None -> empty list (caller supplies the default).
    assert _provider_names(None) == []
    assert _provider_names("") == []


def test_build_solver_default_is_cloak_then_camoufox(monkeypatch) -> None:
    """When ``CAPTCHA_PROVIDER`` is unset we chain cloak -> camoufox.

    Neither browser dep is installed, but the solver classes construct
    without touching the browser (lazy imports) — the FallbackSolver should
    contain exactly those two provider instances in order.
    """
    monkeypatch.delenv("CAPTCHA_PROVIDER", raising=False)
    monkeypatch.delenv("CAPTCHA_TOKEN", raising=False)

    solver = build_solver(transport=None)
    assert isinstance(solver, FallbackSolver)
    names = [type(s).__name__ for s in solver.solvers]
    assert names == ["CloakTurnstileSolver", "CamoufoxTurnstileSolver"]


def test_build_solver_skips_managed_providers_without_keys(monkeypatch) -> None:
    """CAPSolver/2captcha without a key must be silently dropped."""
    monkeypatch.setenv("CAPTCHA_PROVIDER", "capsolver,2captcha,none")
    monkeypatch.delenv("CAPSOLVER_API_KEY", raising=False)
    monkeypatch.delenv("TWOCAPTCHA_API_KEY", raising=False)
    monkeypatch.setenv("CAPTCHA_TOKEN", "static-tok")

    solver = build_solver(transport=object())  # transport present but unused
    # Only ``none`` -> NoopSolver survives (single provider, no chain wrap).
    assert isinstance(solver, NoopSolver)
    assert solver.token == "static-tok"


def test_build_solver_unknown_name_is_dropped(monkeypatch) -> None:
    """An unknown provider is skipped without crashing the chain."""
    monkeypatch.setenv("CAPTCHA_PROVIDER", "doesnotexist,none")
    monkeypatch.setenv("CAPTCHA_TOKEN", "T")
    solver = build_solver(transport=None)
    assert isinstance(solver, NoopSolver)


def test_build_solver_falls_back_to_noop_when_chain_empty(monkeypatch) -> None:
    """CAPTCHA_PROVIDER with only invalid names collapses to NoopSolver."""
    monkeypatch.setenv("CAPTCHA_PROVIDER", "bogus,alsobogus")
    monkeypatch.setenv("CAPTCHA_TOKEN", "FALLBACK")
    solver = build_solver(transport=None)
    assert isinstance(solver, NoopSolver)
    assert solver.solve_turnstile() == "FALLBACK"


def test_ensure_solver_ready_is_true_for_managed_only_chain(monkeypatch) -> None:
    """No browser providers -> nothing to warm; never raises."""
    monkeypatch.setenv("CAPTCHA_PROVIDER", "capsolver,2captcha,manual,none")
    monkeypatch.delenv("CAPSOLVER_API_KEY", raising=False)
    monkeypatch.delenv("TWOCAPTCHA_API_KEY", raising=False)
    assert ensure_solver_ready() is True


def test_ensure_solver_ready_never_raises(monkeypatch) -> None:
    """Browser warm failures are logged and swallowed."""
    monkeypatch.setenv("CAPTCHA_PROVIDER", "cloak")
    messages: list[str] = []
    # The warm attempt will fail quietly (cloakbrowser not installed on CI);
    # the function must still return a bool and never raise.
    out = ensure_solver_ready(logger=messages.append)
    assert isinstance(out, bool)


# ---------------------------------------------------------------------------
# load_proxies
# ---------------------------------------------------------------------------


def test_load_proxies_missing_file_returns_empty(tmp_path) -> None:
    assert load_proxies(str(tmp_path / "nope.txt")) == []
    assert load_proxies("") == []


def test_load_proxies_strips_blanks_crlf_and_comments(tmp_path) -> None:
    path = tmp_path / "p.txt"
    # Mix of CRLF, LF, blanks, comments, and leading whitespace.
    path.write_bytes(
        b"\r\n"
        b"http://a:b@1.2.3.4:9000\r\n"
        b"# comment line\n"
        b"\n"
        b"  http://c:d@5.6.7.8:1000  \n"
        b"\r\n"
    )
    out = load_proxies(str(path))
    assert out == [
        "http://a:b@1.2.3.4:9000",
        "http://c:d@5.6.7.8:1000",
    ]


# ---------------------------------------------------------------------------
# with_redirect_uri helper
# ---------------------------------------------------------------------------


def test_with_redirect_uri_appends_default_on_conso_verify_page() -> None:
    url = with_redirect_uri(TURNSTILE_PAGE_URL)
    assert "redirect_uri=" in url
    # URL-encoded first whitelist entry.
    assert "chromiumapp.org" in url


def test_with_redirect_uri_is_idempotent() -> None:
    url = with_redirect_uri(TURNSTILE_PAGE_URL)
    assert with_redirect_uri(url) == url


def test_with_redirect_uri_leaves_foreign_urls_alone() -> None:
    assert with_redirect_uri("https://example.com/x") == "https://example.com/x"


def test_with_redirect_uri_handles_existing_query_string() -> None:
    url = with_redirect_uri(TURNSTILE_PAGE_URL + "?foo=1")
    assert "foo=1" in url and "redirect_uri=" in url
    assert url.count("?") == 1
    assert "&redirect_uri=" in url


# ---------------------------------------------------------------------------
# Template file
# ---------------------------------------------------------------------------


def test_template_file_present_and_has_markers() -> None:
    """The route-intercept stub must exist and carry both markers."""
    tpl = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "conso"
        / "solver"
        / "cloak"
        / "templates"
        / "turnstile_stub.html"
    )
    body = tpl.read_text(encoding="utf-8")
    assert "<!-- cf turnstile -->" in body
    assert "challenges.cloudflare.com/turnstile/v0/api.js" in body


# ---------------------------------------------------------------------------
# Managed providers: construct from env with dummy keys (no API calls)
# ---------------------------------------------------------------------------


def test_capsolver_api_builds_from_env(monkeypatch) -> None:
    monkeypatch.setenv("CAPTCHA_PROVIDER", "capsolver")
    monkeypatch.setenv("CAPSOLVER_API_KEY", "KEY123")
    monkeypatch.setenv("CAPSOLVER_MODE", "api")
    monkeypatch.setenv("CAPSOLVER_PROXY", "http://u:p@1.2.3.4:9000")

    solver = build_solver(transport=object())
    assert isinstance(solver, CapsolverApiSolver)
    assert solver.api_key == "KEY123"
    assert solver.proxy == "http://u:p@1.2.3.4:9000"


def test_capsolver_sdk_builds_from_env(monkeypatch) -> None:
    monkeypatch.setenv("CAPTCHA_PROVIDER", "capsolver")
    monkeypatch.setenv("CAPSOLVER_API_KEY", "KEY123")
    monkeypatch.setenv("CAPSOLVER_MODE", "sdk")

    solver = build_solver(transport=object())
    assert isinstance(solver, CapSolverSolver)
    assert solver.api_key == "KEY123"


def test_twocaptcha_builds_from_env(monkeypatch) -> None:
    monkeypatch.setenv("CAPTCHA_PROVIDER", "2captcha")
    monkeypatch.setenv("TWOCAPTCHA_API_KEY", "KEY-2C")

    solver = build_solver(transport=object())
    assert isinstance(solver, TwoCaptchaSolver)
    assert solver.api_key == "KEY-2C"


def test_manual_builds_from_env(monkeypatch) -> None:
    monkeypatch.setenv("CAPTCHA_PROVIDER", "manual")
    solver = build_solver(transport=None)
    assert isinstance(solver, ManualSolver)


# ---------------------------------------------------------------------------
# NoopSolver + SolverError semantics
# ---------------------------------------------------------------------------


def test_noop_returns_configured_token() -> None:
    assert NoopSolver("abc").solve_turnstile() == "abc"


def test_noop_without_token_raises_solver_error() -> None:
    with pytest.raises(SolverError):
        NoopSolver("").solve_turnstile()


def test_captchasolver_protocol_accepts_noop_at_runtime() -> None:
    # ``CaptchaSolver`` is a ``runtime_checkable`` Protocol; verify instance
    # of NoopSolver satisfies it (duck-typing on ``solve_turnstile``).
    assert isinstance(NoopSolver("x"), CaptchaSolver)


def test_solver_error_inherits_runtime_error() -> None:
    assert issubclass(SolverError, RuntimeError)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
