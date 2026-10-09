"""Earnings tests — mission classification, idempotency, summary math."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from conso.net.client import ConsoAPIError  # noqa: E402
from conso.ops import earnings  # noqa: E402


class FakeClient:
    """Records RPC calls; can be told to fail a specific mission."""

    def __init__(self, already=None, fail=None):
        self.calls = []
        self.already = set(already or [])
        self.fail = fail or {}

    def claim_daily_mission(self, mission_id, claim_ref=None):
        self.calls.append(("daily", mission_id))
        if mission_id in self.already:
            raise ConsoAPIError("rate_limited")
        if mission_id in self.fail:
            raise ConsoAPIError(self.fail[mission_id])
        return None

    def claim_bonus_mission(self, mission_id):
        self.calls.append(("bonus", mission_id))
        if mission_id in self.already:
            raise ConsoAPIError("rate_limited")
        if mission_id in self.fail:
            raise ConsoAPIError(self.fail[mission_id])
        return None

    def get_todays_mission_claims(self):
        return list(self.already)

    def get_my_leaderboard_rank(self):
        return 100

    def redeem_referral_code(self, code):
        self.calls.append(("referral", code))
        return None

    def redeem_access_code(self, code):
        self.calls.append(("access", code))
        return None

    class transport:
        @staticmethod
        def request(*a, **k):
            raise AssertionError("not used")

    class settings:
        supabase_url = "x"
        supabase_key = "y"

    session = None


def test_claim_mission_daily_and_bonus():
    c = FakeClient()
    r1 = earnings.claim_mission(c, "daily-checkin-v1")
    r2 = earnings.claim_mission(c, "article-about-conso-v1")
    assert r1.status == "claimed" and r1.reward == 2
    assert r2.status == "claimed" and r2.reward == 15
    assert ("daily", "daily-checkin-v1") in c.calls
    assert ("bonus", "article-about-conso-v1") in c.calls


def test_claim_mission_already_claimed():
    c = FakeClient(already={"daily-checkin-v1"})
    r = earnings.claim_mission(c, "daily-checkin-v1")
    assert r.status == "already"


def test_claim_mission_failure():
    c = FakeClient(fail={"tweet-about-conso-v1": "not connected to X"})
    r = earnings.claim_mission(c, "tweet-about-conso-v1")
    assert r.status == "failed"
    assert "X" in r.note


def test_run_earnings_skips_claimed(monkeypatch):
    c = FakeClient(already={"daily-checkin-v1"})
    # patch the row reader to avoid network (monkeypatch, not a global
    # assignment, so the stub cannot leak into other test modules)
    monkeypatch.setattr(earnings, "get_account_row", lambda client: {"total_zaps": 5.0})
    summary = earnings.run_earnings(c, missions=["daily-checkin-v1", "article-about-conso-v1"])
    statuses = {r.mission_id: r.status for r in summary.claimed}
    assert statuses["daily-checkin-v1"] == "skipped"
    assert statuses["article-about-conso-v1"] == "claimed"
    assert summary.zaps_earned == 15


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-v"]))
