"""
Earning engine: every zap-earning action beyond plain turn submission.

Surfaces (all verified against the live backend):

  - Daily check-in   : claim_daily_mission(daily-checkin-v1)          -> +2
  - Tweet mission     : claim_daily_mission(tweet-about-conso-v1)      -> +3
  - Article mission   : claim_bonus_mission(article-about-conso-v1)    -> +15
  - Referral codes    : redeem_referral_code(code)
  - Access codes      : redeem_access_code(code)
  - Turn farming      : append_prompt (see pipeline.farm_turns_for_account)

Claims are idempotent per day: a second attempt returns `rate_limited`, which
is treated as already-claimed. `get_todays_mission_claims` lists what was
already claimed today, so a run skips those first.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from ..net.client import ConsoAPIError, ConsoClient

LogFn = Callable[[str], None]

#: mission id -> (zaps_reward, claim kind)
MISSIONS: dict[str, dict] = {
    "daily-checkin-v1": {"reward": 2, "kind": "daily", "label": "Daily Check-in"},
    "tweet-about-conso-v1": {"reward": 3, "kind": "daily", "label": "Tweet about Conso"},
    "article-about-conso-v1": {"reward": 15, "kind": "bonus", "label": "Write about Conso"},
}

#: rate-limit / already-claimed markers returned by the claim RPCs
ALREADY_CLAIMED = ("rate_limited", "already_claimed", "already claimed", "cooldown")


@dataclass
class ClaimResult:
    mission_id: str
    status: str  # "claimed" | "already" | "failed" | "skipped"
    reward: int = 0
    note: str = ""


@dataclass
class EarningsSummary:
    claimed: list[ClaimResult] = field(default_factory=list)
    zaps_earned: float = 0.0
    total_zaps_before: float = 0.0
    total_zaps_after: float = 0.0
    rank_before: int | None = None
    rank_after: int | None = None

    @property
    def rank_delta(self) -> int | None:
        if self.rank_before is None or self.rank_after is None:
            return None
        return self.rank_before - self.rank_after


def _log(logger: LogFn | None, message: str) -> None:
    if logger:
        logger(message)


def fetch_claimed_today(client: ConsoClient) -> set[str]:
    try:
        data = client.get_todays_mission_claims()
    except ConsoAPIError:
        return set()
    if isinstance(data, list):
        return {str(x) for x in data}
    return set()


def _classify(exc: ConsoAPIError) -> ClaimResult | None:
    text = str(exc).lower()
    if any(marker in text for marker in ALREADY_CLAIMED):
        return ClaimResult("", "already", note=str(exc)[:120])
    return None


def claim_mission(client: ConsoClient, mission_id: str, *, logger: LogFn | None = None) -> ClaimResult:
    meta = MISSIONS.get(mission_id, {})
    reward = int(meta.get("reward", 0))
    kind = meta.get("kind", "daily")
    try:
        if kind == "bonus":
            client.claim_bonus_mission(mission_id)
        else:
            client.claim_daily_mission(mission_id)
        return ClaimResult(mission_id, "claimed", reward)
    except ConsoAPIError as exc:
        classified = _classify(exc)
        if classified:
            classified.mission_id = mission_id
            return classified
        return ClaimResult(mission_id, "failed", 0, str(exc)[:160])


def get_account_row(client: ConsoClient) -> dict:
    """Read the authenticated user's own consousers row."""
    session = client.session
    if session is None:
        return {}
    resp = client.transport.request(
        "GET",
        f"{client.settings.supabase_url}/rest/v1/consousers"
        f"?id=eq.{session.user_id}&select=total_zaps,daily_zaps_earned,current_streak,"
        f"longest_streak,is_banned,boost_factor,access_verified_at,referral_code",
        headers={
            "apikey": client.settings.supabase_key,
            "Authorization": f"Bearer {session.access_token}",
        },
    )
    if resp.status_code >= 400:
        return {}
    data = resp.json()
    return data[0] if isinstance(data, list) and data else {}


def run_earnings(
    client: ConsoClient,
    *,
    missions: list[str] | None = None,
    referral_code: str = "",
    access_code: str = "",
    skip_claimed: bool = True,
    logger: LogFn | None = None,
) -> EarningsSummary:
    """Claim all available missions/codes for the client's current session."""
    summary = EarningsSummary()

    before = get_account_row(client)
    summary.total_zaps_before = float(before.get("total_zaps", 0) or 0)
    summary.rank_before = _safe_rank(client)

    claimed_today = fetch_claimed_today(client) if skip_claimed else set()
    if claimed_today:
        _log(logger, f"earnings: already claimed today: {sorted(claimed_today)}")

    targets = missions if missions is not None else list(MISSIONS.keys())
    for mission_id in targets:
        if skip_claimed and mission_id in claimed_today:
            summary.claimed.append(ClaimResult(mission_id, "skipped", 0, "already claimed today"))
            continue
        result = claim_mission(client, mission_id, logger=logger)
        summary.claimed.append(result)
        if result.status == "claimed":
            summary.zaps_earned += result.reward
        _log(logger, f"earnings: {mission_id} -> {result.status} (+{result.reward if result.status == 'claimed' else 0})")

    if referral_code:
        summary.claimed.append(_redeem(client, "referral", referral_code, logger))
    if access_code:
        summary.claimed.append(_redeem(client, "access", access_code, logger))

    after = get_account_row(client)
    summary.total_zaps_after = float(after.get("total_zaps", 0) or 0)
    summary.rank_after = _safe_rank(client)
    return summary


def _redeem(client: ConsoClient, kind: str, code: str, logger: LogFn | None) -> ClaimResult:
    try:
        if kind == "referral":
            client.redeem_referral_code(code)
        else:
            client.redeem_access_code(code)
        return ClaimResult(f"{kind}:{code}", "claimed", note="redeemed")
    except ConsoAPIError as exc:
        classified = _classify(exc)
        if classified:
            classified.mission_id = f"{kind}:{code}"
            return classified
        return ClaimResult(f"{kind}:{code}", "failed", note=str(exc)[:160])


def _safe_rank(client: ConsoClient) -> int | None:
    try:
        rank = client.get_my_leaderboard_rank()
        return int(rank) if rank is not None else None
    except (ConsoAPIError, TypeError, ValueError):
        return None
