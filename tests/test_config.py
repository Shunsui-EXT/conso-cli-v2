"""Settings loader tests — environment parsing and proxy wiring."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from conso.core import config  # noqa: E402


# ---------------------------------------------------------------------------
# `.env` is auto-loaded on `conso.core.config` import, so every env-sensitive
# test clears the whole environment first with monkeypatch to see pristine
# defaults. Each spec is checked by constructing a fresh Settings via
# `Settings.from_env()` after the patch.
# ---------------------------------------------------------------------------


def _clear_env(monkeypatch):
    for name in [
        "SUPABASE_URL", "SUPABASE_KEY", "CONSO_WEB",
        "CONCURRENCY", "MAX_CONCURRENCY", "REQUEST_TIMEOUT", "MAX_RETRIES",
        "IMPERSONATE", "USER_AGENT",
        "EMAIL_DOMAIN", "PASSWORD_LENGTH", "DISPLAY_NAME_PREFIX",
        "DEFAULT_REFERRAL_CODE", "MIN_REMAINING_ZAPS",
        "MIN_DELAY_SECONDS", "MAX_DELAY_SECONDS",
        "REGISTER_MIN_DELAY_SECONDS", "REGISTER_MAX_DELAY_SECONDS",
        "VERIFIER", "TEMPTF_PROVIDER",
        "DATA_DIR", "LOGS_DIR",
        "PROXY_URLS", "PROXY_FILE", "PROXY_PER_ACCOUNT",
    ]:
        monkeypatch.delenv(name, raising=False)


def test_defaults_when_env_empty(monkeypatch):
    _clear_env(monkeypatch)
    s = config.Settings.from_env()
    # Supabase defaults come from constants (contract with the extension).
    assert s.supabase_url == "https://jzxlayjrsdbyzykuiqns.supabase.co"
    assert s.supabase_key.startswith("sb_publishable_")
    assert s.conso_web == "https://www.conso.xyz"
    # Concurrency / pacing / fingerprint defaults match .env.example.
    assert s.concurrency == 4
    assert s.max_concurrency == 16
    assert s.request_timeout == 30.0
    assert s.max_retries == 3
    assert s.impersonate == "chrome"
    assert s.user_agent == ""
    # Identity defaults.
    assert s.email_domain == ""
    assert s.password_length == 16
    assert s.display_name_prefix == "user"
    assert s.default_referral_code == ""
    assert s.min_remaining_zaps == 1.0
    # Turn/register pacing.
    assert s.min_delay_seconds == 20.0
    assert s.max_delay_seconds == 60.0
    assert s.register_min_delay_seconds == 2.0
    assert s.register_max_delay_seconds == 6.0
    # Verifier defaults.
    assert s.verifier == "temptf"
    assert s.temptf_provider == "gmail"
    # Paths.
    assert s.data_dir == "data"
    assert s.logs_dir == "logs"
    # Proxy pool starts empty + sticky-per-account on by default.
    assert s.proxy.urls == []
    assert s.proxy.enabled is False
    assert s.proxy.per_account is True


def test_proxy_urls_env_parsing(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv(
        "PROXY_URLS",
        "http://u:p@a.example:1080, http://u:p@b.example:1080 ,,"
        "http://u:p@c.example:1080",
    )
    s = config.Settings.from_env()
    # Blanks between commas are dropped; order and credentials are preserved.
    assert s.proxy.urls == [
        "http://u:p@a.example:1080",
        " http://u:p@b.example:1080 ",
        "http://u:p@c.example:1080",
    ]
    assert s.proxy.enabled is True


def test_proxy_file_loading_strips_blanks_and_crlf(tmp_path, monkeypatch):
    _clear_env(monkeypatch)
    pfile = tmp_path / "proxies.txt"
    # Mix of CRLF line endings, blanks and a comment to prove _load_proxy_file
    # drops everything but valid lines.
    pfile.write_bytes(
        b"http://u:p@a.example:1080\r\n"
        b"\r\n"
        b"# commented-out\r\n"
        b"   \r\n"
        b"http://u:p@b.example:1080\r\n"
    )
    monkeypatch.setenv("PROXY_FILE", str(pfile))
    s = config.Settings.from_env()
    assert s.proxy.urls == [
        "http://u:p@a.example:1080",
        "http://u:p@b.example:1080",
    ]
    assert s.proxy.enabled is True


def test_proxy_urls_wins_over_proxy_file(tmp_path, monkeypatch):
    _clear_env(monkeypatch)
    pfile = tmp_path / "proxies.txt"
    pfile.write_text("http://ignored.example:1080\n")
    monkeypatch.setenv("PROXY_URLS", "http://inline.example:1080")
    monkeypatch.setenv("PROXY_FILE", str(pfile))
    s = config.Settings.from_env()
    # Inline list is used verbatim; the file is NOT merged in.
    assert s.proxy.urls == ["http://inline.example:1080"]


def test_proxy_config_enabled_toggle():
    assert config.ProxyConfig().enabled is False
    assert config.ProxyConfig(urls=["http://p"]).enabled is True


def test_proxy_per_account_zero_disables_sticky(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("PROXY_PER_ACCOUNT", "0")
    s = config.Settings.from_env()
    assert s.proxy.per_account is False
