"""Structured persistence for provisioned accounts and run state."""

from __future__ import annotations

import csv
import json
import os
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

SCHEMA_FIELDS = [
    "email",
    "password",
    "consoname",
    "user_id",
    "access_token",
    "refresh_token",
    "expires_at",
    "refreshed_at",
    "proxy",
    "created_at",
    "status",
    "total_zaps",
    "note",
]


@dataclass
class AccountRecord:
    email: str
    password: str = ""
    consoname: str = ""
    user_id: str = ""
    access_token: str = ""
    refresh_token: str = ""
    expires_at: int = 0          # unix seconds; access_token expiry
    refreshed_at: str = ""       # iso timestamp of last successful refresh
    proxy: str = ""
    created_at: str = ""
    status: str = "pending"
    total_zaps: float = 0.0
    note: str = ""

    @classmethod
    def now(cls, **kwargs: Any) -> "AccountRecord":
        kwargs.setdefault("created_at", datetime.now(timezone.utc).isoformat())
        return cls(**kwargs)


class Store:
    """Thread-safe JSON + CSV store with atomic writes and checkpointing."""

    def __init__(self, data_dir: str = "data") -> None:
        self.data_dir = data_dir
        os.makedirs(data_dir, exist_ok=True)
        self.json_path = os.path.join(data_dir, "accounts.json")
        self.csv_path = os.path.join(data_dir, "accounts.csv")
        self.state_path = os.path.join(data_dir, "state.json")
        self._lock = threading.Lock()
        self._accounts: list[AccountRecord] = self._load_json()

    def _load_json(self) -> list[AccountRecord]:
        if not os.path.exists(self.json_path):
            return []
        try:
            with open(self.json_path, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
            return [AccountRecord(**{k: v for k, v in row.items() if k in SCHEMA_FIELDS}) for row in raw]
        except (json.JSONDecodeError, OSError, TypeError):
            return []

    def _flush_locked(self) -> None:
        tmp = self.json_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump([asdict(a) for a in self._accounts], fh, indent=2, ensure_ascii=False)
        os.replace(tmp, self.json_path)

        tmp_csv = self.csv_path + ".tmp"
        with open(tmp_csv, "w", encoding="utf-8", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=SCHEMA_FIELDS)
            writer.writeheader()
            for account in self._accounts:
                writer.writerow({k: asdict(account).get(k, "") for k in SCHEMA_FIELDS})
        os.replace(tmp_csv, self.csv_path)

    def add(self, account: AccountRecord) -> None:
        """Insert a new account, or replace the existing row for that email.

        Upsert, not append: callers routinely re-add a record after refreshing
        its tokens (re-login, recover, farm), and appending would leave stale
        duplicates behind — the store then reports two rows for one account and
        a later read may pick the row holding an already-rotated refresh token.
        """
        with self._lock:
            for i, existing in enumerate(self._accounts):
                if existing.email == account.email:
                    self._accounts[i] = account
                    break
            else:
                self._accounts.append(account)
            self._flush_locked()

    def update(self, email: str, **fields: Any) -> None:
        with self._lock:
            for account in self._accounts:
                if account.email == email:
                    for key, value in fields.items():
                        if key in SCHEMA_FIELDS:
                            setattr(account, key, value)
                    break
            self._flush_locked()

    def remove(self, email: str) -> bool:
        """Delete the row for ``email``; True when something was removed.

        Used to drop accounts that can never earn again (a failed
        registration, a banned login). Removing by email — rather than
        rebuilding the store from a filtered list — keeps the atomic-write
        and locking guarantees identical to ``add``/``update``.
        """
        with self._lock:
            before = len(self._accounts)
            self._accounts = [a for a in self._accounts if a.email != email]
            removed = len(self._accounts) != before
            if removed:
                self._flush_locked()
            return removed

    def all(self) -> list[AccountRecord]:
        with self._lock:
            return list(self._accounts)

    def emails(self) -> set[str]:
        with self._lock:
            return {a.email for a in self._accounts}

    # -- checkpointing for resumable runs ---------------------------------
    def save_state(self, state: dict[str, Any]) -> None:
        with self._lock:
            tmp = self.state_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(state, fh, indent=2)
            os.replace(tmp, self.state_path)

    def load_state(self) -> dict[str, Any]:
        if not os.path.exists(self.state_path):
            return {}
        try:
            with open(self.state_path, "r", encoding="utf-8") as fh:
                return json.load(fh)
        except (json.JSONDecodeError, OSError):
            return {}
