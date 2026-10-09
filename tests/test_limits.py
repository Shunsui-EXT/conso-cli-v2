"""Limits tests — classification and daily-status math."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from conso.ops import limits  # noqa: E402


def test_classify_ban():
    assert limits.classify_limit('{"message":"account_banned"}') == "ban"


def test_classify_rate():
    assert limits.classify_limit("rate_limited") == "rate"
    assert limits.classify_limit("Too many attempts — try again in a minute.") == "rate"


def test_classify_daily_and_onetime():
    assert limits.classify_limit("mission already claimed today") == "daily"
    assert limits.classify_limit("You've already claimed this one-time mission.") == "onetime"
    assert limits.classify_limit("tweet already used") == "onetime"


def test_classify_none():
    assert limits.classify_limit("some other error") is None


def test_daily_status_stale():
    from conso.ops.limits import DailyStatus

    st = DailyStatus(total_zaps=20, daily_zaps_earned=7, daily_zaps_date="2000-01-01",
                     current_streak=1, longest_streak=1, is_banned=False,
                     boost_factor=1.0, is_stale=True)
    assert st.effective_daily == 0.0
    assert limits.daily_headroom(st, 10) == 10.0


def test_daily_headroom():
    from conso.ops.limits import DailyStatus

    st = DailyStatus(total_zaps=20, daily_zaps_earned=7, daily_zaps_date="",
                     current_streak=1, longest_streak=1, is_banned=False,
                     boost_factor=1.0, is_stale=False)
    assert st.effective_daily == 7.0
    assert limits.daily_headroom(st, 10) == 3.0
    assert limits.daily_headroom(st, 5) == 0.0
    assert limits.daily_headroom(st, None) is None


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-v"]))
