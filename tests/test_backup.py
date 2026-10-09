"""
Tests for store snapshots — the cheap insurance against losing account access.

WHY these exist: the account store holds the rotated refresh tokens. Losing
``data/`` costs the ability to log in without a fresh OTP for every account,
and the password is not recoverable at all. The snapshot is the cheap
insurance, so three behaviours are pinned here:

1. every file in ``data/`` is copied,
2. pruning counts copies of the SAME file — ``accounts.json`` and
   ``accounts.csv`` are different files and must not evict each other,
3. the daily loop snapshots on its own once a cycle ends, because that is the
   moment the newest tokens first exist.

Point 2 is a regression guard: an earlier version grouped by file stem alone,
so a single ``--keep 2`` run deleted the ``accounts.json`` copy and left only
the CSV behind.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from conso.app.commands import maintenance  # noqa: E402
from conso.core.config import Settings  # noqa: E402
from conso.ops import backup as backup_mod  # noqa: E402


@pytest.fixture()
def workspace(tmp_path, monkeypatch):
    """A throwaway cwd with a data/ dir, so backups/ lands beside it."""
    data = tmp_path / "data"
    data.mkdir()
    (data / "accounts.json").write_text(json.dumps([{"email": "a@example.com"}]))
    (data / "accounts.csv").write_text("email\na@example.com\n")
    (data / "state.json").write_text(json.dumps({"daily_loop": {}}))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        Settings, "from_env", classmethod(lambda cls: Settings(data_dir=str(data)))
    )
    return tmp_path


def _families(root: Path) -> dict[str, int]:
    """Count backups per source-file family, e.g. {'accounts.json': 2, ...}."""
    counts: dict[str, int] = {}
    for path in (root / "backups").iterdir():
        # 'accounts-20261009-205044.json' -> 'accounts.json'
        stem, _, _rest = path.name.partition("-")
        key = f"{stem}{path.suffix}"
        counts[key] = counts.get(key, 0) + 1
    return counts


# --- the command ------------------------------------------------------------

def test_backup_copies_every_file(workspace):
    maintenance.backup(keep=20)

    out = workspace / "backups"
    assert out.is_dir()
    assert _families(workspace) == {
        "accounts.json": 1,
        "accounts.csv": 1,
        "state.json": 1,
    }
    # The copy must be the real content, not an empty placeholder.
    copy = next(out.glob("accounts-*.json"))
    assert json.loads(copy.read_text()) == [{"email": "a@example.com"}]


def test_prune_keeps_each_file_separately(workspace):
    """--keep counts copies of the same file, not of the same stem."""
    for _ in range(3):
        maintenance.backup(keep=2)
        # The stamp has one-second resolution; without a gap the runs collide
        # on the same filename and overwrite instead of accumulating.
        time.sleep(1.05)

    counts = _families(workspace)
    # Regression: accounts.json must survive alongside accounts.csv.
    assert counts["accounts.json"] == 2, counts
    assert counts["accounts.csv"] == 2, counts
    assert counts["state.json"] == 2, counts


def test_backup_errors_without_data_dir(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        Settings,
        "from_env",
        classmethod(lambda cls: Settings(data_dir=str(tmp_path / "missing"))),
    )
    with pytest.raises(Exception):
        maintenance.backup(keep=20)
    assert not (tmp_path / "backups").exists()


# --- the shared module ------------------------------------------------------

def test_snapshot_reports_counts(workspace):
    copied, pruned = backup_mod.snapshot(workspace / "data", keep=20)
    assert copied == 3
    assert pruned == 0


def test_snapshot_raises_on_empty_dir(tmp_path):
    empty = tmp_path / "data"
    empty.mkdir()
    with pytest.raises(FileNotFoundError):
        backup_mod.snapshot(empty)


def test_snapshot_keep_zero_disables_pruning(workspace):
    for _ in range(3):
        backup_mod.snapshot(workspace / "data", keep=0)
        time.sleep(1.05)
    assert _families(workspace)["accounts.json"] == 3


# --- the loop's automatic hook ----------------------------------------------

class _StubStore:
    """Minimal store stand-in: the hook only needs its data_dir."""

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = str(data_dir)


def _loop(store, *, keep: int):
    from conso.ops.scheduler import DailyLoop, LoopConfig

    return DailyLoop.__new__(DailyLoop), LoopConfig(backup_keep=keep)


def test_loop_snapshots_after_a_cycle(workspace, monkeypatch):
    """The cycle rotates tokens, so the hook must fire once it returns."""
    from conso.ops.scheduler import DailyLoop, LoopConfig

    loop = DailyLoop.__new__(DailyLoop)          # skip __init__ (needs settings)
    loop.store = _StubStore(workspace / "data")
    loop.config = LoopConfig(backup_keep=5)
    loop.logger = None

    loop._snapshot()

    assert _families(workspace)["accounts.json"] == 1


def test_loop_snapshot_can_be_disabled(workspace):
    from conso.ops.scheduler import DailyLoop, LoopConfig

    loop = DailyLoop.__new__(DailyLoop)
    loop.store = _StubStore(workspace / "data")
    loop.config = LoopConfig(backup_keep=0)
    loop.logger = None

    loop._snapshot()

    assert not (workspace / "backups").exists()


def test_loop_snapshot_failure_never_raises(workspace):
    """Insurance must not be able to break the run it is protecting."""
    from conso.ops.scheduler import DailyLoop, LoopConfig

    loop = DailyLoop.__new__(DailyLoop)
    loop.store = _StubStore(workspace / "missing")   # no such dir
    loop.config = LoopConfig(backup_keep=5)
    loop.logger = None

    loop._snapshot()          # must swallow the FileNotFoundError


# --- gitignore guards -------------------------------------------------------

def test_backup_dir_is_gitignored():
    """A backup holds the same credentials as data/, so it must stay untracked."""
    gitignore = Path(__file__).resolve().parents[1] / ".gitignore"
    lines = {
        line.strip().split()[0]
        for line in gitignore.read_text().splitlines()
        if line.strip() and not line.strip().startswith("#")
    }
    assert "backups/" in lines
    assert "data/" in lines


def test_gitignore_has_no_trailing_comments():
    """A trailing comment turns a rule into a literal pattern that never matches.

    Regression: `backups/   # timestamped copies...` silently stopped ignoring
    backups/, leaving credential-bearing copies one `git add -A` from a push.
    Git has no inline-comment syntax, so every rule must be comment-free.
    """
    gitignore = Path(__file__).resolve().parents[1] / ".gitignore"
    offenders = [
        line
        for line in gitignore.read_text().splitlines()
        if line.strip() and not line.strip().startswith("#") and "#" in line
    ]
    assert not offenders, f"gitignore rules with a trailing comment: {offenders}"
