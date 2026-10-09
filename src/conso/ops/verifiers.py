"""
Pluggable verification adapters.

If the Supabase project has `mailer_autoconfirm` disabled, a new account must
confirm its email (or phone). The registration pipeline is decoupled from the
verification mechanism through the `EmailVerifier` protocol, so any of these
can be swapped in without touching the pipeline:

  - NoopVerifier       : account is auto-confirmed, nothing to do.
  - ImapVerifier       : poll an IMAP mailbox (catch-all) for the confirm link.
  - TempMailVerifier   : poll a TempMail-style HTTP API.
  - WebhookVerifier    : wait for a link pushed by an external relay.
"""

from __future__ import annotations

import email
import imaplib
import re
import time
from typing import Protocol

from ..net.transport import Transport

LINK_RE = re.compile(r"https?://[^\s\"'<>]+", re.IGNORECASE)
OTP_RE = re.compile(r"\b(\d{6})\b")


class EmailVerifier(Protocol):
    def wait_for_link(self, address: str, *, timeout: float = 120.0) -> str | None: ...


class NoopVerifier:
    """For auto-confirm projects: nothing to fetch."""

    def wait_for_link(self, address: str, *, timeout: float = 120.0) -> str | None:
        return None


class ImapVerifier:
    """Poll an IMAP mailbox for a confirmation link addressed to `address`."""

    def __init__(
        self,
        host: str,
        user: str,
        password: str,
        *,
        port: int = 993,
        folder: str = "INBOX",
        sender_filter: str = "",
    ) -> None:
        self.host = host
        self.user = user
        self.password = password
        self.port = port
        self.folder = folder
        self.sender_filter = sender_filter.lower()

    def wait_for_link(self, address: str, *, timeout: float = 120.0) -> str | None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            link = self._poll(address)
            if link:
                return link
            time.sleep(3)
        return None

    def _poll(self, address: str) -> str | None:
        try:
            with imaplib.IMAP4_SSL(self.host, self.port) as conn:
                conn.login(self.user, self.password)
                conn.select(self.folder)
                typ, data = conn.search(None, "TO", f'"{address}"')
                if typ != "OK" or not data or not data[0]:
                    return None
                for num in reversed(data[0].split()[-10:]):
                    typ, msg_data = conn.fetch(num, "(RFC822)")
                    if typ != "OK" or not msg_data:
                        continue
                    raw = msg_data[0][1]
                    message = email.message_from_bytes(raw)
                    sender = str(message.get("From", "")).lower()
                    if self.sender_filter and self.sender_filter not in sender:
                        continue
                    body = _extract_body(message)
                    match = LINK_RE.search(body)
                    if match:
                        return match.group(0)
        except (imaplib.IMAP4.error, OSError):
            return None
        return None


class TempMailVerifier:
    """Poll a TempMail-compatible HTTP API (e.g. mail.tm / 1secmail)."""

    def __init__(self, transport: Transport, api_base: str, api_key: str = "") -> None:
        self.transport = transport
        self.api_base = api_base.rstrip("/")
        self.api_key = api_key

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}

    def wait_for_link(self, address: str, *, timeout: float = 120.0) -> str | None:
        deadline = time.time() + timeout
        seen: set[str] = set()
        while time.time() < deadline:
            link = self._poll(address, seen)
            if link:
                return link
            time.sleep(3)
        return None

    def _poll(self, address: str, seen: set[str]) -> str | None:
        try:
            resp = self.transport.request(
                "GET", f"{self.api_base}/messages", headers=self._headers()
            )
            messages = resp.json() if resp.status_code == 200 else []
        except Exception:
            return None
        for message in messages if isinstance(messages, list) else []:
            msg_id = str(message.get("id") or message.get("uid") or "")
            if not msg_id or msg_id in seen:
                continue
            seen.add(msg_id)
            body = _collect_strings(message)
            match = LINK_RE.search(body) or OTP_RE.search(body)
            if match:
                return match.group(0)
        return None


def _extract_body(message: email.message.Message) -> str:
    chunks: list[str] = []
    if message.is_multipart():
        for part in message.walk():
            if part.get_content_type() in ("text/plain", "text/html"):
                payload = part.get_payload(decode=True)
                if payload:
                    chunks.append(payload.decode(part.get_content_charset() or "utf-8", "ignore"))
    else:
        payload = message.get_payload(decode=True)
        if payload:
            chunks.append(payload.decode(message.get_content_charset() or "utf-8", "ignore"))
    return "\n".join(chunks)


def _collect_strings(obj: object) -> str:
    out: list[str] = []
    if isinstance(obj, dict):
        for value in obj.values():
            out.append(_collect_strings(value))
    elif isinstance(obj, list):
        for value in obj:
            out.append(_collect_strings(value))
    elif isinstance(obj, str):
        out.append(obj)
    return "\n".join(out)


def build_verifier(settings) -> EmailVerifier:  # noqa: ANN001 - Settings, avoids import cycle
    """Factory driven by settings.verifier: none|imap|tempmail|mailtm|ncaori|temptf.

    The verifier kind and the temp.tf provider live on `Settings` (parsed once
    from the environment), so the CLI's `doctor` output and every caller agree
    on what is active without re-reading os.environ. Provider-specific
    credentials (IMAP_*, TEMPMAIL_*, NCAORI_*) stay on os.environ: they are
    secrets consumed only here, and they are never surfaced by `doctor`.
    """
    import os

    kind = (getattr(settings, "verifier", "") or "none").strip().lower()
    if kind == "temptf":
        return TempTfVerifier(
            Transport(settings),
            provider=getattr(settings, "temptf_provider", "") or "gmail",
        )
    if kind == "imap":
        return ImapVerifier(
            host=os.environ.get("IMAP_HOST", ""),
            user=os.environ.get("IMAP_USER", ""),
            password=os.environ.get("IMAP_PASSWORD", ""),
            port=int(os.environ.get("IMAP_PORT", "993")),
            folder=os.environ.get("IMAP_FOLDER", "INBOX"),
            sender_filter=os.environ.get("IMAP_SENDER_FILTER", ""),
        )
    if kind == "mailtm":
        return MailTmVerifier(Transport(settings))
    if kind == "ncaori":
        return NcaoriVerifier(
            Transport(settings),
            api_key=os.environ.get("NCAORI_API_KEY", ""),
            base_url=os.environ.get("NCAORI_API", "https://temp-mail.ncaori.my.id"),
        )
    if kind == "tempmail":
        return TempMailVerifier(
            Transport(settings),
            api_base=os.environ.get("TEMPMAlL_API", os.environ.get("TEMPMAIL_API", "")),
            api_key=os.environ.get("TEMPMAIL_KEY", ""),
        )
    return NoopVerifier()


# ---------------------------------------------------------------------------
# temp.tf (https://temp.tf) — real Gmail/Outlook/Hotmail addresses via plus-
# aliases from a managed pool. No API key. These domains pass Conso's signup
# allowlist (unlike mail.tm / ncaori).
# ---------------------------------------------------------------------------
class TempTfVerifier:
    """Provision + poll a temp.tf inbox.

    GET  /api/account?providers=<gmail|outlook|hotmail>&dot=0&plus=1 -> {email}
    POST /api/check {email, wait}                                    -> {data:[...]}
    """

    BASE = "https://temp.tf"

    def __init__(self, transport: Transport, *, provider: str = "gmail") -> None:
        self.transport = transport
        self.provider = provider
        self.address = ""

    def create_inbox(self, local_part: str = "") -> tuple[str, str]:
        """Provision a real-provider alias address. Password is unused (OTP flow)."""
        resp = self.transport.request(
            "GET",
            f"{self.BASE}/api/account?providers={self.provider}&dot=0&plus=1",
            retries=1,
        )
        data = resp.json() if resp.status_code == 200 else {}
        address = data.get("email", "")
        if not address:
            raise RuntimeError(f"temp.tf account failed ({resp.status_code}): {resp.text[:160]}")
        self.address = address
        return address, ""

    def wait_for_link(self, address: str, *, timeout: float = 120.0) -> str | None:
        """Return the first 6-digit OTP (or link) seen in the inbox."""
        deadline = time.time() + timeout
        seen: set[str] = set()
        while time.time() < deadline:
            code = self._poll(address, seen)
            if code:
                return code
            time.sleep(3)
        return None

    def wait_for_otp(self, address: str, *, timeout: float = 120.0) -> str | None:
        return self.wait_for_link(address, timeout=timeout)

    def fetch_latest_otp(self, address: str) -> str | None:
        """Return the newest 6-digit OTP currently in the inbox (single pass)."""
        try:
            resp = self.transport.request(
                "POST", f"{self.BASE}/api/check",
                headers={"Content-Type": "application/json"},
                json={"email": address, "wait": False}, retries=1, timeout=30,
            )
            data = resp.json() if resp.status_code == 200 else {}
        except Exception:
            return None
        messages = data.get("data", []) if isinstance(data, dict) else []
        for message in sorted(messages, key=lambda m: m.get("date", ""), reverse=True):
            match = OTP_RE.search(_collect_strings(message))
            if match:
                return match.group(1)
        return None

    def _poll(self, address: str, seen: set[str]) -> str | None:
        try:
            resp = self.transport.request(
                "POST", f"{self.BASE}/api/check",
                headers={"Content-Type": "application/json"},
                json={"email": address, "wait": True}, retries=0, timeout=45,
            )
            data = resp.json() if resp.status_code == 200 else {}
        except Exception:
            return None
        for message in data.get("data", []) if isinstance(data, dict) else []:
            msg_id = str(message.get("id", ""))
            if not msg_id or msg_id in seen:
                continue
            seen.add(msg_id)
            body = _collect_strings(message)
            match = OTP_RE.search(body) or LINK_RE.search(body)
            if match:
                return match.group(0)
        return None


# ---------------------------------------------------------------------------
# mail.tm (https://mail.tm) — free, no API key. Creates an inbox on demand.
# ---------------------------------------------------------------------------
class MailTmVerifier:
    """Provision + poll a mail.tm inbox.

    Flow: GET /domains -> POST /accounts -> POST /token -> poll /messages.
    The created address/password/token are exposed so the identity generator can
    reuse the same inbox for the account being registered.
    """

    BASE = "https://api.mail.tm"

    def __init__(self, transport: Transport, *, domain: str = "", password: str = "") -> None:
        self.transport = transport
        self.domain = domain
        self.password = password
        self.address = ""
        self.token = ""

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def create_inbox(self, local_part: str = "") -> tuple[str, str]:
        """Create an inbox; returns (address, password)."""
        domain = self.domain
        if not domain:
            resp = self.transport.request("GET", f"{self.BASE}/domains", retries=0)
            domains = resp.json() if resp.status_code == 200 else []
            active = [d for d in domains if d.get("isActive") and not d.get("isPrivate")]
            if not active:
                raise RuntimeError("mail.tm returned no active public domains")
            domain = active[0]["domain"]

        import secrets
        import string

        local = local_part or "".join(secrets.choice(string.ascii_lowercase + string.digits) for _ in range(12))
        address = f"{local}@{domain}"
        password = self.password or ("Pw!" + "".join(
            secrets.choice(string.ascii_letters + string.digits) for _ in range(14)))

        resp = self.transport.request(
            "POST", f"{self.BASE}/accounts", headers=self._headers(),
            json={"address": address, "password": password}, retries=0,
        )
        if resp.status_code not in (200, 201):
            raise RuntimeError(f"mail.tm account create failed ({resp.status_code}): {resp.text[:160]}")
        self.address = address
        self._login(address, password)
        return address, password

    def _login(self, address: str, password: str) -> None:
        resp = self.transport.request(
            "POST", f"{self.BASE}/token", headers={"Content-Type": "application/json"},
            json={"address": address, "password": password}, retries=0,
        )
        data = resp.json() if resp.status_code == 200 else {}
        self.token = data.get("token", "")
        if not self.token:
            raise RuntimeError(f"mail.tm token failed ({resp.status_code})")

    def wait_for_link(self, address: str, *, timeout: float = 120.0) -> str | None:
        if not self.token:
            if not self.address:
                self.address = address
            return None
        deadline = time.time() + timeout
        seen: set[str] = set()
        while time.time() < deadline:
            link = self._poll(seen)
            if link:
                return link
            time.sleep(3)
        return None

    def _poll(self, seen: set[str]) -> str | None:
        try:
            resp = self.transport.request(
                "GET", f"{self.BASE}/messages", headers=self._headers(), retries=0,
            )
            data = resp.json() if resp.status_code == 200 else {}
        except Exception:
            return None
        messages = data.get("hydra:member", []) if isinstance(data, dict) else []
        for message in messages:
            msg_id = str(message.get("id", ""))
            if not msg_id or msg_id in seen:
                continue
            seen.add(msg_id)
            # fetch the full message body
            detail = self.transport.request(
                "GET", f"{self.BASE}/messages/{msg_id}", headers=self._headers(), retries=0,
            )
            body = _collect_strings(detail.json() if detail.status_code == 200 else message)
            match = LINK_RE.search(body) or OTP_RE.search(body)
            if match:
                return match.group(0)
        return None


# ---------------------------------------------------------------------------
# Ncaori Mail+ (https://temp-mail.ncaori.my.id) — Developer API key required.
# ---------------------------------------------------------------------------
class NcaoriVerifier:
    """Poll a Ncaori Mail+ inbox via /api/v1/emails?recipient=<addr>."""

    def __init__(self, transport: Transport, *, api_key: str,
                 base_url: str = "https://temp-mail.ncaori.my.id") -> None:
        self.transport = transport
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")

    def wait_for_link(self, address: str, *, timeout: float = 120.0) -> str | None:
        deadline = time.time() + timeout
        seen: set[str] = set()
        while time.time() < deadline:
            link = self._poll(address, seen)
            if link:
                return link
            time.sleep(3)
        return None

    def _poll(self, address: str, seen: set[str]) -> str | None:
        try:
            resp = self.transport.request(
                "GET", f"{self.base_url}/api/v1/emails",
                headers={"Authorization": f"Bearer {self.api_key}", "Accept": "application/json"},
                data={"recipient": address}, retries=0,
            )
            data = resp.json() if resp.status_code == 200 else {}
        except Exception:
            return None
        for message in data.get("emails", []) if isinstance(data, dict) else []:
            msg_id = str(message.get("id", ""))
            if not msg_id or msg_id in seen:
                continue
            seen.add(msg_id)
            body = _collect_strings(message)
            match = LINK_RE.search(body) or OTP_RE.search(body)
            if match:
                return match.group(0)
        return None
