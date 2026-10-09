"""
Provisioning + turn-farming pipeline.

Two flows:

  register()  : create an account (Supabase auth) -> create the Conso user
                record -> optionally set consoname / redeem referral.

  farm_turns(): log in -> submit synthetic AI-usage turns via append_prompt,
                respecting the client-side zap accounting and pacing.

Both are driven through ConsoClient and persist through Store, with adaptive
concurrency and resumable checkpoints. Progress is reported on the shared
event bus (:mod:`conso.ops.events`) so the app layer can render it without
this module importing any application code.
"""

from __future__ import annotations

import itertools
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from ..auth.session import SessionManager, jwt_expiry
from ..core import constants as C
from ..core import economy
from ..core.config import Settings
from ..core.identity import build_identity
from ..net.client import ConsoAPIError, ConsoClient, Session
from ..net.transport import Transport
from ..solver import CaptchaSolver
from ..store.storage import AccountRecord, Store
from .concurrency import AdaptiveConcurrency, Pacer
from .events import emit
from .verifiers import EmailVerifier

LogFn = Callable[[str], None]

# Locked (model, platform) pairs: the highest-multiplier real model id on each
# platform. Payloads outside this set fall to the platform floor (0.1 on gemini).
TOP_MODELS: tuple[tuple[str, str], ...] = (
    ("claude-fable-5", "claude"),
    ("pplx_asi_fable_5", "perplexity"),
    ("gpt-5-6-thinking", "chatgpt"),
    ("gemini-3.1-pro", "gemini"),
)


@dataclass
class PipelineResult:
    email: str
    status: str
    note: str = ""


def _log(default: LogFn | None, message: str) -> None:
    # The logger callback is the single funnel for log output; the app layer
    # passes a callback that also emits to the event bus, so no bus emit happens here.
    if default:
        default(message)


def _wait_for_code(verifier: EmailVerifier, address: str, *, timeout: float) -> str | None:
    """Fetch a confirmation code/link from the verifier (wait_for_otp or link)."""
    otp_fn = getattr(verifier, "wait_for_otp", None)
    if callable(otp_fn):
        result = otp_fn(address, timeout=timeout)
        return str(result) if result else None
    result = verifier.wait_for_link(address, timeout=timeout)
    return str(result) if result else None


def register_account(
    settings: Settings,
    *,
    index: int = 0,
    verifier: EmailVerifier | None = None,
    solver: CaptchaSolver | None = None,
    referral_code: str = "",
    proxy: str = "",
    logger: LogFn | None = None,
) -> AccountRecord:
    """Provision a single account end-to-end.

    `proxy` (when set) is pinned to every request for this account, so signup
    and create_consouser originate from a distinct IP per account.

    Ordering is load-bearing (mirrors the extension's onboarding screens):
    signup -> OTP verify -> create_consouser (velocity-retried) -> redeem
    referral -> set_consoname. Redeeming before onboarding matters because the
    server credits referral zaps to an account that already has a Conso row,
    and set_consoname is what marks the profile as ready for turns.
    """
    rng = random.Random()
    try:
        identity = build_identity(settings, rng=rng, index=index)
    except ValueError:
        # No EMAIL_DOMAIN configured; the verifier must provision its own inbox.
        from ..core.identity import Identity, generate_consoname, generate_password

        identity = Identity(
            email="", password=generate_password(rng, settings.password_length),
            display_name="", consoname=generate_consoname(rng),
        )

    # If the verifier can provision its own inbox (mail.tm), use that address
    # instead of the EMAIL_DOMAIN-derived one.
    create_inbox = getattr(verifier, "create_inbox", None)
    if callable(create_inbox):
        try:
            address, password = create_inbox()
            identity.email = address
            if password:
                identity.password = password
        except Exception as exc:  # noqa: BLE001
            _log(logger, f"register: inbox provision failed: {exc}")

    # Supabase signup always requires a password, even for OTP-only inboxes.
    if not identity.password:
        from ..core.identity import generate_password

        identity.password = generate_password(rng, settings.password_length)

    if not identity.email:
        record = AccountRecord.now(email="", status="failed", note="no email domain / inbox")
        return record

    record = AccountRecord.now(email=identity.email, password=identity.password, consoname=identity.consoname)
    record.proxy = proxy
    client = ConsoClient(settings, transport=Transport(settings, pin_proxy=proxy or None))
    try:
        # Conso enforces Turnstile on signup + login; solve once and reuse.
        # The solver runs DIRECT (no proxy): the pool is datacenter and cannot
        # pass Turnstile. Only the HTTP calls (signup/OTP/RPC) use the account
        # proxy.
        captcha_token: str | None = None
        if solver is not None:
            emit("ACCOUNT_STAGE", email=identity.email, stage="captcha")
            captcha_token = solver.solve_turnstile()
            _log(logger, f"register: {identity.email} captcha solved (solver=direct, http_proxy={proxy or 'direct'})")

        emit("ACCOUNT_STAGE", email=identity.email, stage="signup")
        _log(logger, f"register: {identity.email} -> supabase signup")
        signup = client.sign_up_email(identity.email, identity.password, captcha_token=captcha_token)

        # Conso confirms via a 6-digit email OTP (not a link). Fetch it from the
        # verifier and exchange it for a session.
        session = None
        if verifier is not None:
            emit("ACCOUNT_STAGE", email=identity.email, stage="otp")
            code = _wait_for_code(verifier, identity.email, timeout=120.0)
            if code:
                _log(logger, f"register: {identity.email} got code {code}")
                session = client.verify_otp(identity.email, code)

        # Fallback: a project with auto-confirm lets password login through.
        if session is None:
            session = client.sign_in_password(
                identity.email, identity.password, captcha_token=captcha_token
            )

        record.user_id = session.user_id
        record.access_token = session.access_token
        record.refresh_token = session.refresh_token
        record.expires_at = session.expires_at or jwt_expiry(session.access_token)

        # Create the Conso user row (extension passes the OAuth subject id).
        # This MUST succeed: without the row the account cannot farm. On a
        # transient rejection (signup_velocity_exceeded, rate_limited) retry with
        # backoff before giving up — the auth account already exists, so a retry
        # later just completes provisioning.
        emit("ACCOUNT_STAGE", email=identity.email, stage="create")
        row_ok = False
        last_err = ""
        for attempt in range(1, 4):
            try:
                client.create_consouser(session.user_id)
                row_ok = True
                break
            except ConsoAPIError as exc:
                last_err = str(exc)
                if "velocity" in last_err.lower() or "rate_limit" in last_err.lower():
                    _log(logger, f"register: {identity.email} create_consouser transient ({last_err[:60]}), "
                                 f"retry {attempt}/3")
                    time.sleep(5 * attempt)
                    continue
                break
        if not row_ok:
            record.status = "failed"
            record.note = f"create_consouser: {last_err[:160]}"
            _log(logger, f"register: {identity.email} create_consouser failed: {last_err}")
            emit("ACCOUNT_FAILED", email=identity.email, reason=record.note)
            return record

        # Redeem the referral BEFORE onboarding: the extension shows the
        # referral screen (redeem referral/access code) right after
        # createConsouser and before picking a consoname.
        if referral_code:
            emit("ACCOUNT_STAGE", email=identity.email, stage="referral")
            try:
                client.redeem_referral_code(referral_code)
                _log(logger, f"register: {identity.email} referral={referral_code}")
            except ConsoAPIError as exc:
                _log(logger, f"register: referral note: {exc}")

        # Complete onboarding: set the display name, matching the extension flow
        # (createConsouser -> referral -> pick consoname -> ready) before any turn.
        if identity.consoname:
            emit("ACCOUNT_STAGE", email=identity.email, stage="consoname")
            try:
                client.set_consoname(identity.consoname)
                _log(logger, f"register: {identity.email} consoname={identity.consoname}")
            except ConsoAPIError as exc:
                _log(logger, f"register: set_consoname note: {exc}")

        record.status = "active"
        record.note = "registered"
        emit("ACCOUNT_COMPLETED", email=identity.email, proxy=proxy)
        return record
    except ConsoAPIError as exc:
        record.status = "failed"
        record.note = str(exc)[:200]
        return record
    except Exception as exc:  # noqa: BLE001 - surface any transport failure
        record.status = "failed"
        record.note = f"{type(exc).__name__}: {exc}"[:200]
        emit("ACCOUNT_FAILED", email=record.email, reason=record.note)
        return record
    finally:
        client.close()


def run_registration(
    settings: Settings,
    count: int,
    *,
    referral_code: str = "",
    store: Store | None = None,
    verifier: EmailVerifier | None = None,
    solver: CaptchaSolver | None = None,
    logger: LogFn | None = None,
) -> list[PipelineResult]:
    store = store or Store(settings.data_dir)
    emit("BATCH_STARTED", target=count, solver=type(solver).__name__ if solver else "",
         referral=referral_code)
    engine = AdaptiveConcurrency(initial=settings.concurrency, maximum=settings.max_concurrency)
    # Per-worker pacing: each account signs up from its own identity (and its
    # own pinned proxy when PROXY_PER_ACCOUNT=1), so a shared clock would only
    # serialise the pool. Registration also uses its own (much shorter) delay
    # range — the 20-60s turn pacing is for reusing one account's session.
    pacer = Pacer(
        settings.register_min_delay_seconds, settings.register_max_delay_seconds,
        per_worker=True,
    )
    results: list[PipelineResult] = []
    lock = threading.Lock()
    state = store.load_state()
    done = int(state.get("register_done", 0))

    # Per-account proxy: round-robin the pool so each account signs up from a
    # distinct IP (spreads the server's signup-velocity limit).
    proxy_pool = list(settings.proxy.urls) if settings.proxy.per_account else []
    proxy_cycle = itertools.cycle(proxy_pool) if proxy_pool else None

    def task(i: int) -> AccountRecord:
        pacer.wait()
        acct_proxy = next(proxy_cycle) if proxy_cycle else ""
        record = register_account(
            settings, index=i, verifier=verifier, solver=solver,
            referral_code=referral_code, proxy=acct_proxy, logger=logger,
        )
        if record.status == "active":
            engine.report_success()
        else:
            engine.report_error()
        return record

    with ThreadPoolExecutor(max_workers=settings.max_concurrency) as pool:
        futures: dict[Any, int] = {}
        pending = list(range(done, done + count))
        cursor = 0

        def submit_more() -> None:
            nonlocal cursor
            while cursor < len(pending) and len(futures) < engine.limit:
                i = pending[cursor]
                cursor += 1
                futures[pool.submit(task, i)] = i

        submit_more()
        while futures:
            for future in as_completed(list(futures)):
                futures.pop(future, None)
                record = future.result()
                store.add(record)
                with lock:
                    results.append(PipelineResult(record.email, record.status, record.note))
                    done += 1
                _log(logger, f"[{record.status}] {record.email} :: {record.note}")
                submit_more()
                break
            store.save_state({"register_done": done})

    emit("BATCH_COMPLETED", status="COMPLETED")
    return results


# ---------------------------------------------------------------------------
# Turn farming
# ---------------------------------------------------------------------------
@dataclass
class TurnSpec:
    platform: str
    model: str
    prompt_text: str
    response_text: str
    has_non_image_attachment: bool = False


def make_synthetic_turn(rng: random.Random, index: int) -> TurnSpec:
    """Generate a plausible, high-quality turn payload.

    Uses the locked (model, platform) pairs — the highest-multiplier real model
    id on each platform — so payloads earn at the top of the range instead of
    falling to the platform floor (0.1 on gemini).
    """
    platform, model = rng.choice(TOP_MODELS)
    prompt = (
        "You are the on-call engineer writing the post-incident review for a "
        "payments outage. Reconstruct the analysis from the timeline below and "
        "produce a defensible remediation plan.\n\n"
        "Timeline (UTC):\n"
        "- 14:02 deploy v2.14.3 completes; adds a retry loop around the charge handler.\n"
        "- 14:07 p99 latency on POST /charge climbs from 180 ms to 2.4 s.\n"
        "- 14:11 error rate reaches 12%, almost all HTTP 504 from the gateway.\n"
        "- 14:14 on-call rolls back to v2.14.2.\n"
        "- 14:19 latency and error rate return to baseline.\n\n"
        "The retry loop uses exponential backoff starting at 200 ms, five attempts, "
        "no jitter, and no circuit breaker. The gateway enforces a 2 s request "
        "timeout and a per-merchant concurrency limit of 10 in-flight requests. "
        "The handler holds a DB connection for the full duration of every attempt; "
        "the pool is 20 per instance across 8 instances.\n\n"
        "Produce the review with exactly these sections: root cause, contributing "
        "factors, why staging missed it, remediation (jitter strategy, backoff "
        "curve, retry budget, circuit-breaker thresholds, per-layer timeout "
        "budget), detection, and prevention.\n\n"
        "Format the answer as a markdown document, one section per point, ending "
        "with a table comparing four retry strategies across columns: strategy, "
        "thundering-herd risk, convergence time, complexity, recommendation.\n"
        f"Case id: {index}-{rng.randint(100000, 999999)}"
    )
    response = (
        "Below is the analysis.\n\n"
        "## Root cause\nThe retry storm amplifies load by ~5x against a gateway "
        "capped at 10 in-flight per merchant, so the pool saturates and every "
        "attempt queues behind the 2 s timeout.\n\n"
        "```python\n"
        "# jittered backoff + circuit breaker\n"
        "delay = min(cap, base * 2 ** attempt) * random.uniform(0.5, 1.5)\n"
        "```\n\n"
        "| strategy | herd risk | convergence | complexity | recommendation |\n"
        "|----------|-----------|-------------|------------|----------------|\n"
        "| fixed | high | slow | low | no |\n"
        "| exp | medium | medium | low | yes |\n"
        "| exp+jitter | low | fast | low | yes |\n"
        "| adaptive | low | fast | high | maybe |\n"
    ) * rng.randint(3, 5)
    return TurnSpec(platform=platform, model=model, prompt_text=prompt, response_text=response,
                    has_non_image_attachment=rng.random() < 0.15)


def farm_turns_for_account(
    settings: Settings,
    record: AccountRecord,
    turns: int,
    *,
    solver: CaptchaSolver | None = None,
    store: Store | None = None,
    verifier: EmailVerifier | None = None,
    logger: LogFn | None = None,
) -> tuple[int, float]:
    """Submit `turns` synthetic turns for one account. Returns (ok_count, zaps).

    Ceiling-aware (see analysis/REFERENCE_STUDY.md):
      - reads the account's boost_factor and remaining daily budget,
      - sizes each turn so ``base_zaps * boost_factor`` lands just under
        CREDIT_CEILING (overshooting credits 0 and precedes a ban),
      - stops when the daily budget is filled or on the zero-credit soft flag.

    Sessions are resolved through SessionManager (cached / refresh+persist /
    password fallback).
    """
    store = store or Store(settings.data_dir)
    manager = SessionManager(settings, store, verifier=verifier)
    client = ConsoClient(settings)
    pacer = Pacer(settings.min_delay_seconds, settings.max_delay_seconds)
    ok = 0
    credited = 0.0

    effective_turns = min(turns, C.DAILY_TURN_CAP)
    if effective_turns < turns:
        _log(logger, f"farm: {record.email} clamping {turns} -> {effective_turns} (daily cap)")
    try:
        try:
            result = manager.ensure_session(record, solver=solver)
        except ConsoAPIError:
            verifier = getattr(manager, "verifier", None)
            result = manager.recover(record, solver=solver, verifier=verifier)
        client.session = result.session

        # Read profile: boost factor + remaining daily budget.
        from .earnings import get_account_row
        from .limits import get_daily_status

        row = get_account_row(client) or {}
        boost = float(row.get("boost_factor") or 1.0) or 1.0
        # Use the stale-aware status: daily_zaps_earned is a raw counter that
        # still reads yesterday's total until the first credit of the new day,
        # so trusting it directly would skip an account that has actually
        # reset. effective_daily returns 0 when the counter is from a past date.
        status = get_daily_status(client)
        daily_used = status.effective_daily if status else 0.0
        remaining = max(0.0, C.DAILY_ZAP_CAP - daily_used)
        if row.get("is_banned") or (status and status.is_banned):
            _log(logger, f"farm: {record.email} banned — skipping")
            return 0, 0.0
        if remaining <= 0:
            _log(logger, f"farm: {record.email} daily cap reached ({daily_used:.2f})")
            return 0, 0.0
        per_turn = economy.send_zaps_for_target(
            round(remaining / max(1, effective_turns), 2), boost
        )
        _log(logger, f"farm: {record.email} session={result.source} boost={boost:.2f} "
                     f"daily={daily_used:.2f} remaining={remaining:.2f} per_turn={per_turn}")

        rng = random.Random()
        for i in range(effective_turns):
            pacer.wait()
            spec = make_synthetic_turn(rng, i)
            account = economy.account_turn(
                platform=spec.platform,
                model=spec.model,
                prompt_text=spec.prompt_text,
                response_text=spec.response_text,
                has_non_image_attachment=spec.has_non_image_attachment,
            )
            # Rescale tokens so the credited zaps land on the per-turn target.
            account = economy.scale_tokens_for_zaps(
                account, platform=spec.platform, model=spec.model, target_zaps=per_turn,
                has_non_image_attachment=spec.has_non_image_attachment,
            )
            entry = economy.build_entry(
                account, timestamp=economy.js_isoformat(datetime.now(timezone.utc))
            )
            try:
                result = client.append_prompt(entry, account.zaps, account.spend_usd)
                ok += 1
                turn_credited = float(_extract_credited(result, account.zaps))
                credited += turn_credited
                _log(logger, f"farm: {record.email} {spec.platform}/{spec.model} "
                             f"sent={account.zaps} credited={turn_credited}")
                emit("TURN_CREDITED", email=record.email, zaps=turn_credited, platform=spec.platform)

                if turn_credited <= 0:
                    _log(logger, f"farm: {record.email} credited={turn_credited} — soft flag, stopping")
                    break
                if credited >= remaining:
                    _log(logger, f"farm: {record.email} daily budget filled ({credited:.2f})")
                    break
            except ConsoAPIError as exc:
                _log(logger, f"farm: {record.email} append_prompt rejected: {exc}")
                if exc.status in (429, 403) or "account_banned" in str(exc):
                    break
        return ok, round(credited, 2)
    finally:
        client.close()


def _extract_credited(result: Any, fallback: float) -> float:
    """The append_prompt RPC returns the credited zaps directly (a number).

    Older/alternate shapes return an object; handle both.
    """
    if isinstance(result, bool):  # bool is an int subclass; not a credit
        return fallback
    if isinstance(result, (int, float)):
        return float(result)
    if isinstance(result, dict):
        for key in ("creditedZaps", "credited_zaps", "zaps"):
            if key in result and isinstance(result[key], (int, float)):
                return float(result[key])
    return fallback
