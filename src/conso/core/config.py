"""Runtime configuration loaded from environment / .env."""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

from . import constants as C

load_dotenv()


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _env_int(name: str, default: int) -> int:
    raw = _env(name)
    return int(raw) if raw else default


def _env_float(name: str, default: float) -> float:
    raw = _env(name)
    return float(raw) if raw else default


@dataclass
class ProxyConfig:
    """Proxy pool settings."""

    urls: list[str] = field(default_factory=list)
    health_check_url: str = "https://api.ipify.org?format=json"
    max_failures: int = 3
    #: when True, each registered account is pinned to one proxy (sticky) so
    #: signup/create_consouser run from a distinct IP.
    per_account: bool = True

    @property
    def enabled(self) -> bool:
        return bool(self.urls)


@dataclass
class Settings:
    # Backend
    supabase_url: str = C.SUPABASE_URL
    supabase_key: str = C.SUPABASE_PUBLISHABLE_KEY
    conso_web: str = C.CONSO_WEB

    # Concurrency
    concurrency: int = 4
    max_concurrency: int = 16
    request_timeout: float = 30.0
    max_retries: int = 3

    # Fingerprint / transport
    impersonate: str = "chrome"
    user_agent: str = ""

    # Identity generation
    email_domain: str = ""
    password_length: int = 16
    display_name_prefix: str = "user"

    # Referral applied to every newly registered account.
    default_referral_code: str = ""

    # Skip farming an account whose remaining daily budget is below this many
    # zaps. At 20.83/21 the server still accepts turns but sizes each one down
    # to ~0.02, so finishing the last fraction costs a dozen requests and adds
    # nothing. 0 disables the skip.
    min_remaining_zaps: float = 1.0

    # Anti-abuse pacing. A reference implementation (analysis/REFERENCE_STUDY.md)
    # uses 60-180s between turns on one account; shorter gaps are faster but the
    # backend scores scripted cadence, so raise these for long-running farms.
    min_delay_seconds: float = 20.0
    max_delay_seconds: float = 60.0

    # Registration pacing, separate from the turn pacing above. Turns reuse one
    # account's session, so they need long gaps; signups each use a fresh
    # identity and (with PROXY_PER_ACCOUNT) a distinct IP, so they only need
    # enough jitter to avoid a fixed cadence. Keeping the turn delay here would
    # serialise the worker pool and cap register throughput at ~1/min.
    register_min_delay_seconds: float = 2.0
    register_max_delay_seconds: float = 6.0

    # Email verifier selection (consumed by conso.ops.verifiers / build_verifier).
    # Kept on Settings so every subsystem reads it the same way and the CLI's
    # `doctor` output can report what's active without re-reading os.environ.
    verifier: str = "temptf"
    temptf_provider: str = "gmail"

    # Paths
    data_dir: str = "data"
    logs_dir: str = "logs"

    # Proxy
    proxy: ProxyConfig = field(default_factory=ProxyConfig)

    @classmethod
    def from_env(cls) -> "Settings":
        proxy_urls = [u for u in _env("PROXY_URLS").split(",") if u.strip()]
        proxy_file = _env("PROXY_FILE")
        if proxy_file and not proxy_urls:
            proxy_urls = _load_proxy_file(proxy_file)
        return cls(
            supabase_url=_env("SUPABASE_URL", C.SUPABASE_URL),
            supabase_key=_env("SUPABASE_KEY", C.SUPABASE_PUBLISHABLE_KEY),
            conso_web=_env("CONSO_WEB", C.CONSO_WEB),
            concurrency=_env_int("CONCURRENCY", 4),
            max_concurrency=_env_int("MAX_CONCURRENCY", 16),
            request_timeout=_env_float("REQUEST_TIMEOUT", 30.0),
            max_retries=_env_int("MAX_RETRIES", 3),
            impersonate=_env("IMPERSONATE", "chrome"),
            user_agent=_env("USER_AGENT"),
            email_domain=_env("EMAIL_DOMAIN"),
            password_length=_env_int("PASSWORD_LENGTH", 16),
            display_name_prefix=_env("DISPLAY_NAME_PREFIX", "user"),
            default_referral_code=_env("DEFAULT_REFERRAL_CODE"),
            min_remaining_zaps=_env_float("MIN_REMAINING_ZAPS", 1.0),
            min_delay_seconds=_env_float("MIN_DELAY_SECONDS", 20.0),
            max_delay_seconds=_env_float("MAX_DELAY_SECONDS", 60.0),
            register_min_delay_seconds=_env_float("REGISTER_MIN_DELAY_SECONDS", 2.0),
            register_max_delay_seconds=_env_float("REGISTER_MAX_DELAY_SECONDS", 6.0),
            verifier=_env("VERIFIER", "temptf"),
            temptf_provider=_env("TEMPTF_PROVIDER", "gmail"),
            data_dir=_env("DATA_DIR", "data"),
            logs_dir=_env("LOGS_DIR", "logs"),
            proxy=ProxyConfig(
                urls=proxy_urls,
                per_account=_env("PROXY_PER_ACCOUNT", "1") == "1",
            ),
        )


def _load_proxy_file(path: str) -> list[str]:
    """Load proxies from a file (one per line; strips CRLF and blanks)."""
    if not path or not os.path.exists(path):
        return []
    out: list[str] = []
    with open(path, "r", encoding="utf-8", errors="ignore") as fh:
        for line in fh:
            entry = line.strip()
            if entry and not entry.startswith("#"):
                out.append(entry)
    return out
