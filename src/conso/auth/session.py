"""
Session manager: keep accounts logged in without re-authenticating.

Supabase access tokens expire (default 1h) and the refresh token **rotates on
every use**, so a naive "refresh but don't save" loses the account. This module:

  - loads the stored access/refresh token,
  - reuses the access token while it is valid (with a safety margin),
  - otherwise refreshes, and **persists the rotated refresh token + new expiry**
    back to the Store,
  - falls back to password login (Turnstile) only when the refresh chain breaks.

It is thread-safe per account and safe to call from concurrent farm workers.
"""

from __future__ import annotations

import base64
import json
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone

from ..core.config import Settings
from ..net.client import ConsoAPIError, ConsoClient, Session
from ..net.transport import Transport
from ..store.storage import AccountRecord, Store


def jwt_expiry(access_token: str) -> int:
    """Return the `exp` claim (unix seconds) of a JWT, or 0 if unreadable."""
    try:
        payload = access_token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload))
        return int(data.get("exp", 0))
    except Exception:
        return 0


@dataclass
class SessionResult:
    session: Session
    source: str  # "cached" | "refreshed" | "password" | "otp"


class SessionManager:
    """Resolve a live session for an account, refreshing and persisting tokens."""

    #: refresh when the access token has less than this many seconds left
    EXPIRY_MARGIN = 120

    def __init__(self, settings: Settings, store: Store, *, transport: Transport | None = None,
                 verifier=None) -> None:
        self.settings = settings
        self.store = store
        self.transport = transport
        self.verifier = verifier
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()

    def _lock_for(self, email: str) -> threading.Lock:
        with self._locks_guard:
            lock = self._locks.get(email)
            if lock is None:
                lock = threading.Lock()
                self._locks[email] = lock
            return lock

    def ensure_session(
        self,
        record: AccountRecord,
        *,
        solver=None,  # CaptchaSolver | None
        force_refresh: bool = False,
    ) -> SessionResult:
        """Return a valid session, refreshing/persisting as needed.

        Raises ConsoAPIError if no path succeeds.
        """
        with self._lock_for(record.email):
            fresh = self._current_record(record.email) or record

            # 1) reuse the cached access token while valid
            if not force_refresh and fresh.access_token:
                exp = fresh.expires_at or jwt_expiry(fresh.access_token)
                if exp and exp - time.time() > self.EXPIRY_MARGIN:
                    return SessionResult(
                        self._session_from(fresh), "cached",
                    )

            # 2) refresh (persists the rotated refresh token)
            if fresh.refresh_token:
                try:
                    return SessionResult(self._refresh(fresh), "refreshed")
                except ConsoAPIError:
                    pass

            # 3) password login (Turnstile)
            if fresh.password:
                token = solver.solve_turnstile() if solver is not None else None
                client = self._client()
                try:
                    session = client.sign_in_password(
                        fresh.email, fresh.password, captcha_token=token
                    )
                finally:
                    client.close()
                self._persist(fresh.email, session)
                return SessionResult(session, "password")

            raise ConsoAPIError(f"no valid session for {fresh.email}")

    # -- recovery ----------------------------------------------------------
    def recover(
        self,
        record: AccountRecord,
        *,
        solver=None,
        verifier=None,
        otp_timeout: float = 120.0,
    ) -> SessionResult:
        """Recover a session when refresh + password both fail.

        Order: refresh -> password login (Turnstile) -> email OTP to the stored
        address (which keeps receiving mail as long as the temp.tf provider
        account exists). Persists the new session on success.
        """
        with self._lock_for(record.email):
            fresh = self._current_record(record.email) or record

            if fresh.refresh_token:
                try:
                    return SessionResult(self._refresh(fresh), "refreshed")
                except ConsoAPIError:
                    pass

            client = self._client()
            try:
                if fresh.password:
                    token = solver.solve_turnstile() if solver is not None else None
                    try:
                        session = client.sign_in_password(
                            fresh.email, fresh.password, captcha_token=token
                        )
                        self._persist(fresh.email, session)
                        return SessionResult(session, "password")
                    except ConsoAPIError:
                        pass

                # OTP fallback: request a fresh code, read it, exchange it.
                if verifier is not None:
                    otp_fn = getattr(verifier, "fetch_latest_otp", None)
                    token = solver.solve_turnstile() if solver is not None else None
                    client.sign_in_otp(fresh.email, captcha_token=token)
                    code = None
                    if callable(otp_fn):
                        # poll the inbox for the freshly delivered code
                        import time as _time

                        deadline = _time.time() + otp_timeout
                        while _time.time() < deadline:
                            code = otp_fn(fresh.email)
                            if code:
                                break
                            _time.sleep(3)
                    if code:
                        session = client.verify_otp(fresh.email, code)
                        self._persist(fresh.email, session)
                        return SessionResult(session, "otp")
            finally:
                client.close()

            raise ConsoAPIError(f"could not recover session for {fresh.email}")

    # -- internals ---------------------------------------------------------
    def _client(self) -> ConsoClient:
        return ConsoClient(self.settings, self.transport)

    def _current_record(self, email: str) -> AccountRecord | None:
        for account in self.store.all():
            if account.email == email:
                return account
        return None

    def _session_from(self, record: AccountRecord) -> Session:
        return Session(
            access_token=record.access_token,
            refresh_token=record.refresh_token,
            user_id=record.user_id,
            email=record.email,
            expires_at=record.expires_at,
        )

    def _refresh(self, record: AccountRecord) -> Session:
        client = self._client()
        try:
            session = client.refresh(record.refresh_token)
        finally:
            client.close()
        self._persist(record.email, session)
        return session

    def _persist(self, email: str, session: Session) -> None:
        exp = session.expires_at or jwt_expiry(session.access_token)
        self.store.update(
            email,
            access_token=session.access_token,
            refresh_token=session.refresh_token,
            expires_at=exp,
            user_id=session.user_id or "",
            refreshed_at=datetime.now(timezone.utc).isoformat(),
        )
