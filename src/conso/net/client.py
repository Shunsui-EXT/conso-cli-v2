"""
Conso backend client.

Two surfaces are used:

1. Supabase (GoTrue auth + PostgREST RPC) at jzxlayjrsdbyzykuiqns.supabase.co.
   The extension calls PostgREST RPCs directly with the user's access token,
   e.g. `append_prompt`, `create_consouser`. This client does the same.

2. The conso.xyz Next.js API (`/api/extension/x/...`) which proxies the X
   OAuth handshake. Only relevant for linking an X account.

Everything is driven through the fingerprinted Transport.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from ..core.config import Settings
from .transport import Transport


class ConsoAPIError(RuntimeError):
    def __init__(self, message: str, *, status: int | None = None, payload: Any = None):
        super().__init__(message)
        self.status = status
        self.payload = payload


@dataclass
class Session:
    access_token: str
    refresh_token: str
    user_id: str
    email: str | None = None
    expires_at: int = 0
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.access_token}"}


class ConsoClient:
    def __init__(self, settings: Settings, transport: Transport | None = None) -> None:
        self.settings = settings
        self.transport = transport or Transport(settings)
        self.session: Session | None = None

    # -- low-level helpers -------------------------------------------------
    def _supabase_headers(self, *, authed: bool) -> dict[str, str]:
        headers = {
            "apikey": self.settings.supabase_key,
            "Content-Type": "application/json",
        }
        token = self.session.access_token if (authed and self.session) else self.settings.supabase_key
        headers["Authorization"] = f"Bearer {token}"
        return headers

    def _rpc(self, name: str, args: dict[str, Any], *, authed: bool = True) -> Any:
        url = f"{self.settings.supabase_url}/rest/v1/rpc/{name}"
        resp = self.transport.request(
            "POST", url, headers=self._supabase_headers(authed=authed), json=args
        )
        if resp.status_code >= 400:
            raise ConsoAPIError(
                f"RPC {name} failed ({resp.status_code}): {resp.text[:300]}",
                status=resp.status_code,
                payload=_safe_json(resp),
            )
        return _safe_json(resp)

    # -- auth --------------------------------------------------------------
    def _auth_body(self, body: dict[str, Any], captcha_token: str | None) -> dict[str, Any]:
        """Attach a Turnstile token in Supabase's expected envelope.

        The Conso Supabase project enforces Turnstile on all auth endpoints;
        the token travels as gotrue_meta_security.captcha_token.
        """
        if captcha_token:
            body = {**body, "gotrue_meta_security": {"captcha_token": captcha_token}}
        return body

    def sign_up_email(
        self, email: str, password: str, *, captcha_token: str | None = None
    ) -> dict[str, Any]:
        url = f"{self.settings.supabase_url}/auth/v1/signup"
        body = self._auth_body({"email": email, "password": password}, captcha_token)
        resp = self.transport.request(
            "POST", url, headers=self._supabase_headers(authed=False), json=body,
        )
        payload = _safe_json(resp) or {}
        if resp.status_code >= 400:
            raise ConsoAPIError(
                f"signup failed: {payload.get('msg') or payload.get('error_description') or resp.text[:200]}",
                status=resp.status_code,
                payload=payload,
            )
        return payload

    def sign_in_password(
        self, email: str, password: str, *, captcha_token: str | None = None
    ) -> Session:
        url = f"{self.settings.supabase_url}/auth/v1/token?grant_type=password"
        body = self._auth_body({"email": email, "password": password}, captcha_token)
        resp = self.transport.request(
            "POST", url, headers=self._supabase_headers(authed=False), json=body,
        )
        payload = _safe_json(resp) or {}
        if resp.status_code >= 400:
            raise ConsoAPIError(
                f"login failed: {payload.get('error_description') or payload.get('msg') or resp.text[:200]}",
                status=resp.status_code,
                payload=payload,
            )
        return self._session_from_token_response(payload, email=email)

    def sign_in_otp(self, email: str, *, captcha_token: str | None = None) -> dict[str, Any]:
        """Send a magic-link / OTP email (Supabase signInWithOtp)."""
        url = f"{self.settings.supabase_url}/auth/v1/otp"
        body = self._auth_body({"email": email, "create_user": True}, captcha_token)
        resp = self.transport.request(
            "POST", url, headers=self._supabase_headers(authed=False), json=body,
        )
        payload = _safe_json(resp) or {}
        if resp.status_code >= 400:
            raise ConsoAPIError(
                f"otp send failed: {payload.get('msg') or resp.text[:200]}",
                status=resp.status_code, payload=payload,
            )
        return payload

    def verify_otp(self, email: str, token: str) -> Session:
        """Exchange an email OTP code for a Supabase session."""
        url = f"{self.settings.supabase_url}/auth/v1/verify"
        resp = self.transport.request(
            "POST", url, headers=self._supabase_headers(authed=False),
            json={"type": "email", "email": email, "token": token},
        )
        payload = _safe_json(resp) or {}
        if resp.status_code >= 400:
            raise ConsoAPIError(
                f"otp verify failed: {payload.get('msg') or resp.text[:200]}",
                status=resp.status_code, payload=payload,
            )
        return self._session_from_token_response(payload, email=email)

    def sign_in_id_token(self, provider: str, id_token: str, nonce: str | None = None) -> Session:
        """Exchange a Google/OIDC id_token for a Supabase session."""
        url = f"{self.settings.supabase_url}/auth/v1/token?grant_type=id_token"
        body: dict[str, Any] = {"provider": provider, "id_token": id_token}
        if nonce:
            body["nonce"] = nonce
        resp = self.transport.request(
            "POST", url, headers=self._supabase_headers(authed=False), json=body
        )
        payload = _safe_json(resp) or {}
        if resp.status_code >= 400:
            raise ConsoAPIError(
                f"id_token login failed: {payload.get('error_description') or resp.text[:200]}",
                status=resp.status_code,
                payload=payload,
            )
        return self._session_from_token_response(payload)

    def refresh(self, refresh_token: str) -> Session:
        url = f"{self.settings.supabase_url}/auth/v1/token?grant_type=refresh_token"
        resp = self.transport.request(
            "POST", url, headers=self._supabase_headers(authed=False),
            json={"refresh_token": refresh_token},
        )
        payload = _safe_json(resp) or {}
        if resp.status_code >= 400:
            raise ConsoAPIError(
                f"refresh failed: {payload.get('error_description') or resp.text[:200]}",
                status=resp.status_code,
                payload=payload,
            )
        return self._session_from_token_response(payload)

    def _session_from_token_response(self, payload: dict[str, Any], email: str | None = None) -> Session:
        user = payload.get("user") or {}
        self.session = Session(
            access_token=payload.get("access_token", ""),
            refresh_token=payload.get("refresh_token", ""),
            user_id=user.get("id", ""),
            email=user.get("email") or email,
            expires_at=int(payload.get("expires_at") or 0),
            raw=payload,
        )
        return self.session

    def get_user(self) -> dict[str, Any] | None:
        if not self.session:
            return None
        url = f"{self.settings.supabase_url}/auth/v1/user"
        resp = self.transport.request("GET", url, headers=self._supabase_headers(authed=True))
        if resp.status_code >= 400:
            return None
        return _safe_json(resp)

    # -- Conso RPCs --------------------------------------------------------
    def create_consouser(self, google_id: str) -> dict[str, Any]:
        return self._rpc("create_consouser", {"p_google_id": google_id})

    def set_consoname(self, consoname: str) -> dict[str, Any]:
        """Set the display name (extension does a direct table UPDATE).

        Returns the updated row; raises on cooldown/taken.
        """
        if not self.session:
            raise ConsoAPIError("not signed in")
        url = (
            f"{self.settings.supabase_url}/rest/v1/consousers"
            f"?id=eq.{self.session.user_id}"
        )
        headers = {**self._supabase_headers(authed=True), "Prefer": "return=representation"}
        resp = self.transport.request("PATCH", url, headers=headers, json={"consoname": consoname})
        if resp.status_code >= 400:
            payload = _safe_json(resp) or {}
            raise ConsoAPIError(
                payload.get("message", f"set_consoname failed ({resp.status_code})"),
                status=resp.status_code, payload=payload,
            )
        return _safe_json(resp)

    def is_consoname_available(self, consoname: str) -> bool:
        result = self._rpc("is_consoname_available", {"p_consoname": consoname})
        return bool(result)

    def append_prompt(
        self, entry: dict[str, Any], base_zaps: float, spend_usd: float
    ) -> dict[str, Any]:
        """Submit a detected turn. `entry` = p_entry (see economy.build_entry)."""
        return self._rpc(
            "append_prompt",
            {"p_entry": entry, "p_base_zaps": base_zaps, "p_spend_usd": spend_usd},
        )

    def redeem_referral_code(self, code: str) -> dict[str, Any]:
        return self._rpc("redeem_referral_code", {"p_code": code.strip()})

    def redeem_access_code(self, code: str) -> dict[str, Any]:
        return self._rpc("redeem_access_code", {"p_code": code.strip()})

    def claim_daily_mission(self, mission_id: Any, claim_ref: str | None = None) -> dict[str, Any]:
        return self._rpc(
            "claim_daily_mission", {"p_mission_id": mission_id, "p_claim_ref": claim_ref}
        )

    def claim_bonus_mission(self, mission_id: Any) -> dict[str, Any]:
        return self._rpc("claim_bonus_mission", {"p_mission_id": mission_id})

    def get_todays_mission_claims(self) -> Any:
        return self._rpc("get_todays_mission_claims", {})

    def get_my_leaderboard_rank(self) -> Any:
        return self._rpc("get_my_leaderboard_rank", {})

    def get_lifetime_platform_stats(self) -> Any:
        return self._rpc("get_lifetime_platform_stats", {})

    def get_daily_activity(self, start: str, end: str, platform: str | None = None) -> Any:
        return self._rpc(
            "get_daily_activity", {"p_start": start, "p_end": end, "p_platform": platform}
        )

    def get_hourly_activity(self, platform: str | None = None) -> Any:
        return self._rpc("get_hourly_activity", {"p_platform": platform})

    def get_avg_session_minutes(self, platform: str | None, days: int) -> Any:
        return self._rpc("get_avg_session_minutes", {"p_platform": platform, "p_days": days})

    # -- conso.xyz Next.js API (X linking) ---------------------------------
    def x_oauth_start(self) -> str:
        resp = self.transport.request("POST", f"{self.settings.conso_web}/api/extension/x/start")
        payload = _safe_json(resp) or {}
        if resp.status_code >= 400 or not payload.get("url"):
            raise ConsoAPIError(f"x_oauth_start failed ({resp.status_code})", status=resp.status_code)
        return payload["url"]

    def x_oauth_callback(self, code: str, state: str) -> dict[str, Any]:
        resp = self.transport.request(
            "POST",
            f"{self.settings.conso_web}/api/extension/x/callback",
            headers={"Content-Type": "application/json"},
            json={"code": code, "state": state},
        )
        payload = _safe_json(resp) or {}
        if resp.status_code >= 400 or not payload.get("ok"):
            raise ConsoAPIError(
                payload.get("error", "x callback failed"), status=resp.status_code, payload=payload
            )
        return payload

    def close(self) -> None:
        self.transport.close()


def _safe_json(resp: Any) -> Any:
    try:
        return resp.json()
    except Exception:
        try:
            return json.loads(resp.text)
        except Exception:
            return None
