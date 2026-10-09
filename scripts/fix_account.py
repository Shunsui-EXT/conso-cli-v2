#!/usr/bin/env python3
"""Re-establish a session for one account whose refresh token went stale.

A rotated-but-unpersisted refresh token invalidates the stored one, and an
expired access token leaves no way back except email OTP. This triggers a fresh
OTP, waits for the code to actually CHANGE (the inbox still holds the previous
code, so reading it immediately just replays an expired one), and verifies it.

Usage:
    python scripts/fix_account.py --email user@example.com
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from conso.core.config import Settings  # noqa: E402
from conso.core.identity import generate_password  # noqa: E402
from conso.net.client import ConsoClient  # noqa: E402
from conso.net.transport import Transport  # noqa: E402
from conso.ops.verifiers import build_verifier  # noqa: E402
from conso.solver import build_solver  # noqa: E402
from conso.store.storage import Store  # noqa: E402

POLL_SECONDS = 120


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--email", required=True)
    args = ap.parse_args()

    settings = Settings.from_env()
    store = Store(settings.data_dir)
    rec = next((a for a in store.all() if a.email == args.email), None)
    if rec is None:
        print(f"not found: {args.email}")
        return 1

    transport = Transport(settings)
    client = ConsoClient(settings)
    solver = build_solver(transport)
    verifier = build_verifier(settings)

    # Snapshot the code that is already sitting in the inbox. Reusing it is
    # what "Token has expired or is invalid" means, so wait for a different one.
    # Not every verifier exposes a raw fetch (it is outside the EmailVerifier
    # protocol), so feature-detect it and fall back to waiting blind.
    fetch_otp = getattr(verifier, "fetch_latest_otp", None)
    old_code = ""
    if callable(fetch_otp):
        try:
            old_code = fetch_otp(args.email) or ""
        except Exception:
            old_code = ""
    if not callable(fetch_otp):
        print("verifier has no fetch_latest_otp — cannot detect a stale code")
    print(f"inbox already has: {old_code or '(none)'} — waiting for a NEW code")

    print("solving captcha ...")
    token = solver.solve_turnstile()
    print(f"captcha solved (len={len(token)})")

    client.sign_in_otp(args.email, captcha_token=token)
    print("otp requested")

    deadline = time.time() + POLL_SECONDS
    new_code = ""
    while time.time() < deadline:
        time.sleep(5)
        if not callable(fetch_otp):
            time.sleep(5)
            continue
        try:
            code = fetch_otp(args.email) or ""
        except Exception:
            continue
        if code and code != old_code:
            new_code = code
            break
    if not new_code:
        print("no new code arrived in time")
        return 1
    print(f"new code: {new_code}")

    session = client.verify_otp(args.email, new_code)
    print(f"verified — access_len={len(session.access_token)} "
          f"refresh={session.refresh_token[:10]}")

    # Persist a password too, so the next recovery can use the cheaper
    # password grant instead of waiting on an inbox that may be gone.
    import random

    password = rec.password or generate_password(random.Random(), settings.password_length)
    rec.access_token = session.access_token
    rec.refresh_token = session.refresh_token
    rec.expires_at = session.expires_at
    rec.password = password
    rec.status = "active"
    rec.note = "fixed-otp"
    store.add(rec)
    print(f"saved — status={rec.status} password={'yes' if rec.password else 'no'}")

    # Optionally attach the password to the account so password grant works.
    try:
        client.session = session
        url = f"{settings.supabase_url}/auth/v1/user"
        resp = transport.request(
            "PUT", url,
            headers={**client._supabase_headers(authed=True),
                     "Content-Type": "application/json"},
            json={"password": password}, timeout=30,
        )
        print(f"password set on server: HTTP {resp.status_code}")
    except Exception as exc:  # noqa: BLE001
        print(f"password update skipped: {type(exc).__name__}: {exc}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
