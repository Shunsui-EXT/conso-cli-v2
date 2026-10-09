"""
Authentication for the Conso CLI.

`SessionManager` is the only place that turns a stored `AccountRecord` into a
live Supabase `Session`. It exists because Supabase rotates the refresh token
on every use: any caller that refreshes without persisting the rotated token
silently loses the account. The manager reuses cached access tokens while they
are valid, refreshes and persists otherwise, and falls back to password / OTP
login only when the refresh chain has genuinely broken.
"""

from __future__ import annotations

from .session import SessionManager, SessionResult, jwt_expiry

__all__ = ["SessionManager", "SessionResult", "jwt_expiry"]
