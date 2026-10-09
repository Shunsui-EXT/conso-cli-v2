"""
Account persistence for the Conso CLI.

`Store` is the single durable source of truth for provisioned accounts: one
upserted row per email in a JSON + CSV pair, plus a checkpoint file for
resumable runs. It is deliberately stdlib-only (no config, no network) so it
can be imported by every layer — including `auth.session`, which persists the
rotated Supabase refresh token back through it on every refresh.
"""

from __future__ import annotations

from .storage import SCHEMA_FIELDS, AccountRecord, Store

__all__ = ["AccountRecord", "SCHEMA_FIELDS", "Store"]
