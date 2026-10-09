"""
Store snapshots — the cheap insurance against losing account access.

WHY this is its own module: two callers need it (the ``backup`` command and
the daily loop's end-of-run hook), and the rules that matter are subtle enough
that a second copy would drift:

- the store holds the ROTATED refresh tokens. Losing ``data/`` costs a fresh
  OTP for every account, and the password is not recoverable at all;
- a snapshot holds the same credentials as ``data/``, so the destination
  (``backups/``) must stay gitignored;
- pruning counts copies of the SAME file. ``accounts.json`` and
  ``accounts.csv`` share a stem but are different files, and grouping by stem
  alone makes them evict each other.
"""

from __future__ import annotations

import shutil
import time
from pathlib import Path
from typing import Callable

LogFn = Callable[[str], None]


def snapshot(
    data_dir: str | Path,
    *,
    out_dir: str | Path = "backups",
    keep: int = 20,
    stamp: str | None = None,
    logger: LogFn | None = None,
) -> tuple[int, int]:
    """Copy every file in ``data_dir`` into ``out_dir``, pruning old copies.

    Returns ``(copied, pruned)``. Raises ``FileNotFoundError`` when there is
    nothing to copy — callers decide whether that is fatal (the ``backup``
    command) or merely worth a log line (the loop's automatic hook).

    ``keep`` counts copies of each individual file, so ``--keep 2`` leaves two
    ``accounts.json`` copies *and* two ``accounts.csv`` copies. ``keep <= 0``
    disables pruning.
    """
    data_path = Path(data_dir)
    if not data_path.is_dir():
        raise FileNotFoundError(f"{data_path}/ does not exist")

    sources = sorted(p for p in data_path.iterdir() if p.is_file())
    if not sources:
        raise FileNotFoundError(f"{data_path}/ holds no files")

    out_path = Path(out_dir)
    out_path.mkdir(parents=True, exist_ok=True)
    stamp = stamp or time.strftime("%Y%m%d-%H%M%S")

    copied = 0
    for src in sources:
        shutil.copy2(src, out_path / f"{src.stem}-{stamp}{src.suffix}")
        copied += 1

    # The family pattern keeps the extension: `accounts.json` and `accounts.csv`
    # are different files, and a stem-only pattern would prune one of each pair.
    pruned = 0
    if keep > 0:
        for pattern in {f"{p.stem}-*{p.suffix}" for p in sources}:
            family = sorted(out_path.glob(pattern), key=lambda p: p.stat().st_mtime)
            for old in family[:-keep]:
                old.unlink()
                pruned += 1

    if logger:
        note = f"backup: {copied} file(s) -> {out_path}/ ({stamp})"
        logger(note + (f", pruned {pruned} old" if pruned else ""))

    return copied, pruned
