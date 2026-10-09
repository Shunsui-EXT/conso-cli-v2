"""
Daily-limit tracking.

Conso tracks daily earnings per account (`daily_zaps_earned` / `daily_zaps_date`
on the `consousers` row) and rate-limits claims per day. This module surfaces
that state so a run can stop before tripping a limit, and classifies the
server's limit responses.

Server limit responses observed (see the extension's mission error map):
  - `rate_limited`                 -> short window ("try again in a minute")
  - `mission already claimed today`-> daily mission already claimed
  - `mission already claimed`      -> one-time mission already claimed
  - `account_banned`               -> hard stop

`daily_zaps_earned` resets when `daily_zaps_date` rolls over to a new day.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from ..net.client import ConsoClient

#: substrings that indicate a limit (vs a hard failure). Order matters:
#: daily/one-time markers are checked before the generic "rate" marker.
BAN_MARKERS = ("account_banned", "banned")
DAILY_LIMIT_MARKERS = ("already claimed today", "claimed this mission today", "check back tomorrow")
ONETIME_MARKERS = ("already claimed", "one-time", "once", "already used")
RATE_LIMIT_MARKERS = ("rate_limited", "too many attempts", "try again")

#: sum of daily mission rewards (check-in + tweet), the guaranteed daily floor
DAILY_MISSION_FLOOR = 5.0


@dataclass
class DailyStatus:
    total_zaps: float
    daily_zaps_earned: float
    daily_zaps_date: str
    current_streak: int
    longest_streak: int
    is_banned: bool
    boost_factor: float
    is_stale: bool  # daily counter is from a previous day -> reset pending

    @property
    def effective_daily(self) -> float:
        """Daily earned today (0 if the counter is stale)."""
        return 0.0 if self.is_stale else self.daily_zaps_earned


def _today_utc() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def classify_limit(message: str) -> str | None:
    """Classify a server message: 'rate' | 'daily' | 'onetime' | 'ban' | None."""
    text = message.lower()
    if any(m in text for m in BAN_MARKERS):
        return "ban"
    if any(m in text for m in RATE_LIMIT_MARKERS):
        return "rate"
    if any(m in text for m in DAILY_LIMIT_MARKERS):
        return "daily"
    if any(m in text for m in ONETIME_MARKERS):
        return "onetime"
    return None


def get_daily_status(client: ConsoClient) -> DailyStatus | None:
    """Read the account's daily-earning state from its own consousers row."""
    session = client.session
    if session is None:
        return None
    resp = client.transport.request(
        "GET",
        f"{client.settings.supabase_url}/rest/v1/consousers"
        f"?id=eq.{session.user_id}&select=total_zaps,daily_zaps_earned,daily_zaps_date,"
        f"current_streak,longest_streak,is_banned,boost_factor",
        headers={
            "apikey": client.settings.supabase_key,
            "Authorization": f"Bearer {session.access_token}",
        },
    )
    if resp.status_code >= 400:
        return None
    data = resp.json()
    if not isinstance(data, list) or not data:
        return None
    row = data[0]
    day = row.get("daily_zaps_date") or ""
    return DailyStatus(
        total_zaps=float(row.get("total_zaps", 0) or 0),
        daily_zaps_earned=float(row.get("daily_zaps_earned", 0) or 0),
        daily_zaps_date=day,
        current_streak=int(row.get("current_streak", 0) or 0),
        longest_streak=int(row.get("longest_streak", 0) or 0),
        is_banned=bool(row.get("is_banned", False)),
        boost_factor=float(row.get("boost_factor", 1) or 1),
        is_stale=bool(day) and day != _today_utc(),
    )


def daily_headroom(status: DailyStatus | None, cap: float | None) -> float | None:
    """Remaining daily zaps before `cap` (None = unknown cap -> None)."""
    if status is None or cap is None:
        return None
    return max(0.0, cap - status.effective_daily)
