#!/usr/bin/env python3
"""Re-establish sessions for every account whose refresh token is dead.

When does this happen: refresh tokens rotate on use, so whichever side runs
last owns them. If the store on this machine went stale (another host ran the
daily cycle, or a token was consumed without being persisted), every refresh
comes back `refresh_token_already_used` and the only way back is a fresh
credential grant.

Two paths are tried per account, cheapest first:

  1. refresh   — free, no captcha; works when the stored token is still live
  2. password  — one Turnstile solve; works when the account has a password

Accounts that fail both are reported and left untouched, so a rerun only
retries the failures.

Usage:
    python scripts/relogin_all.py                 # all active accounts
    python scripts/relogin_all.py --email a@b.com # just one
    python scripts/relogin_all.py --only-dead     # skip the free refresh pass
    python scripts/relogin_all.py --dry-run       # report, change nothing
"""

from __future__ import annotations

import argparse
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from conso.core.config import Settings  # noqa: E402
from conso.net.client import ConsoAPIError, ConsoClient  # noqa: E402
from conso.net.transport import Transport  # noqa: E402
from conso.solver import build_solver  # noqa: E402
from conso.store.storage import AccountRecord, Store  # noqa: E402

# A dead refresh token is a permanent condition for that token, not a transient
# error, so it is reported rather than retried.
DEAD_MARKERS = ("already_used", "invalid refresh token", "refresh token not found")


def _is_dead(exc: Exception) -> bool:
    text = str(exc).lower()
    return any(marker in text for marker in DEAD_MARKERS)


def relogin_one(record: AccountRecord, *, settings: Settings, solver, store: Store,
                force_password: bool, dry_run: bool) -> tuple[str, str]:
    """Return (status, detail). Status: refreshed | relogged | failed | skipped."""
    client = ConsoClient(settings)

    # 1) free path — reuse the stored refresh token while it is still valid
    if record.refresh_token and not force_password:
        try:
            session = client.refresh(record.refresh_token)
            if session.access_token:
                if not dry_run:
                    record.access_token = session.access_token
                    record.refresh_token = session.refresh_token
                    record.expires_at = session.expires_at
                    record.refreshed_at = time.strftime("%Y-%m-%dT%H:%M:%S")
                    store.add(record)          # upsert, not append
                return "refreshed", "refresh token was still live"
        except ConsoAPIError as exc:
            if not _is_dead(exc):
                # transient (network / rate limit) — do not burn a solve on it
                return "failed", f"refresh: {str(exc)[:70]}"

    # 2) credential path — costs one Turnstile solve
    if not record.password:
        return "skipped", "no password stored; needs email OTP"
    try:
        token = solver.solve_turnstile()
    except Exception as exc:  # noqa: BLE001
        return "failed", f"captcha: {str(exc)[:60]}"
    try:
        session = client.sign_in_password(record.email, record.password,
                                          captcha_token=token)
    except ConsoAPIError as exc:
        return "failed", f"password: {str(exc)[:70]}"

    if not dry_run:
        record.access_token = session.access_token
        record.refresh_token = session.refresh_token
        record.expires_at = session.expires_at
        record.refreshed_at = time.strftime("%Y-%m-%dT%H:%M:%S")
        store.add(record)
    return "relogged", "password grant"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--email", default="", help="Only this account.")
    ap.add_argument("--workers", type=int, default=1,
                    help="Parallel workers (solves are serialised anyway; >1 mainly overlaps the free refresh pass).")
    ap.add_argument("--only-dead", action="store_true",
                    help="Skip the free refresh pass and go straight to password.")
    ap.add_argument("--dry-run", action="store_true", help="Report only; change nothing.")
    args = ap.parse_args()

    settings = Settings.from_env()
    store = Store(settings.data_dir)
    transport = Transport(settings)
    solver = build_solver(transport)

    records = [a for a in store.all() if a.status == "active"]
    if args.email:
        records = [a for a in records if a.email == args.email]
    if not records:
        print("no matching active accounts")
        return 1

    print(f"relogin: {len(records)} account(s), workers={args.workers}"
          f"{' (dry-run)' if args.dry_run else ''}")

    counts: dict[str, int] = {}
    failures: list[str] = []

    def work(rec: AccountRecord):
        return rec.email, relogin_one(
            rec, settings=settings, solver=solver, store=store,
            force_password=args.only_dead, dry_run=args.dry_run,
        )

    done = 0
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        for email, (status, detail) in pool.map(work, records):
            done += 1
            counts[status] = counts.get(status, 0) + 1
            mark = {"refreshed": "OK ", "relogged": "OK ", "skipped": "-- "}.get(status, "ERR")
            print(f"[{done}/{len(records)}] {mark} {email[:36]:38} {status:9} {detail[:40]}")
            if status == "failed":
                failures.append(f"{email}: {detail}")

    print("\nresult: " + " · ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    if failures:
        print(f"\n{len(failures)} failure(s):")
        for line in failures[:15]:
            print("  ", line)
    return 0 if counts.get("failed", 0) == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
