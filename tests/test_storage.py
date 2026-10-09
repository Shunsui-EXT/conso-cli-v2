"""Store behaviour: upsert semantics and durable round-trips."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from conso.store.storage import AccountRecord, Store  # noqa: E402


def _store(tmp_path: Path) -> Store:
    return Store(str(tmp_path))


def test_add_twice_for_same_email_replaces_not_appends(tmp_path: Path) -> None:
    """Re-adding a refreshed record must not leave a stale duplicate behind.

    The token refresh path re-adds the record with new tokens; appending would
    keep the old row (holding an already-rotated refresh token) alongside it.
    """
    store = _store(tmp_path)
    store.add(AccountRecord(email="a@x.com", refresh_token="OLD"))
    store.add(AccountRecord(email="a@x.com", refresh_token="NEW"))

    accounts = store.all()
    assert len(accounts) == 1
    assert accounts[0].refresh_token == "NEW"


def test_add_different_emails_both_kept(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.add(AccountRecord(email="a@x.com", refresh_token="A"))
    store.add(AccountRecord(email="b@x.com", refresh_token="B"))

    assert {a.email for a in store.all()} == {"a@x.com", "b@x.com"}


def test_upsert_persists_to_disk(tmp_path: Path) -> None:
    """A fresh Store over the same dir sees the replaced row, not two."""
    store = _store(tmp_path)
    store.add(AccountRecord(email="a@x.com", refresh_token="OLD"))
    store.add(AccountRecord(email="a@x.com", refresh_token="NEW"))

    raw = json.loads((tmp_path / "accounts.json").read_text())
    assert len(raw) == 1
    assert raw[0]["refresh_token"] == "NEW"

    reopened = _store(tmp_path)
    assert len(reopened.all()) == 1
    assert reopened.all()[0].refresh_token == "NEW"


def test_upsert_keeps_unset_fields_from_new_record(tmp_path: Path) -> None:
    """The newest record wins wholesale — no field merge across generations."""
    store = _store(tmp_path)
    store.add(AccountRecord(email="a@x.com", status="active", consoname="old-name"))
    store.add(AccountRecord(email="a@x.com", status="active", consoname="new-name"))

    account = store.all()[0]
    assert account.consoname == "new-name"


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-v"]))


def test_remove_deletes_row_and_persists(tmp_path: Path) -> None:
    """Dropping a dead account must survive a reopen, in JSON and CSV."""
    store = _store(tmp_path)
    store.add(AccountRecord(email="keep@x.com", refresh_token="K"))
    store.add(AccountRecord(email="drop@x.com", refresh_token="D"))

    assert store.remove("drop@x.com") is True
    assert [a.email for a in store.all()] == ["keep@x.com"]

    raw = json.loads((tmp_path / "accounts.json").read_text())
    assert [r["email"] for r in raw] == ["keep@x.com"]
    assert "drop@x.com" not in (tmp_path / "accounts.csv").read_text()

    reopened = _store(tmp_path)
    assert [a.email for a in reopened.all()] == ["keep@x.com"]


def test_remove_unknown_email_is_a_noop(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.add(AccountRecord(email="a@x.com"))
    assert store.remove("missing@x.com") is False
    assert [a.email for a in store.all()] == ["a@x.com"]
