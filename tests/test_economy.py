"""Economy engine tests — pins the ported zap/USD/quality math."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from conso.core import economy  # noqa: E402


def test_model_multiplier_exact_and_fallback():
    assert economy.model_multiplier("claude-fable-5", "claude") == 1.4
    assert economy.model_multiplier("gpt-5-6", "chatgpt") == 0.6
    # unknown model -> platform fallback
    assert economy.model_multiplier("mystery-model", "gemini") == 0.1
    # unknown model + unknown platform -> global default
    assert economy.model_multiplier("mystery-model", "nope") == 0.25


def test_model_multiplier_suffix_strip():
    # trailing -YYYYMMDD is stripped before lookup
    assert economy.model_multiplier("gpt-5-6-20250101", "chatgpt") == 0.6


def test_zap_formula_matches_hand_calc():
    # claude-opus-4-8 multiplier = 0.7
    # in=10000, out=20000, media=0, quality=1
    # zaps = round2((10000+20000)/1e4 * 1 * 0.7 * 2.5) = round2(5.25) = 5.25
    zaps = economy.compute_zaps(10000, 20000, "claude-opus-4-8", "claude", 0, 1.0)
    assert zaps == 5.25


def test_media_carve_out():
    # media portion uses the 0.5 factor instead of 2.5
    # primary = (30000-10000)/1e4 * 0.7 * 2.5 = 3.5 ; media = 10000/1e4 * 0.7 * 0.5 = 0.35
    zaps = economy.compute_zaps(10000, 20000, "claude-opus-4-8", "claude", 10000, 1.0)
    assert zaps == 3.85


def test_quality_bands_and_combine():
    short = economy.quality_score("hi there")
    long_structured = economy.quality_score(
        "Write a detailed markdown table comparing json, xml and csv output formats.\n"
        "Provide a multi-line example for each and explain the trade-offs in depth, "
        "including performance and readability considerations for large payloads."
    )
    assert short < 3.0
    assert long_structured >= 4.0
    assert economy.quality_breakdown("a b c d\njson").structure.signals == ["Multi-line prompt", "Output format specified"]


def test_usd_cost():
    cost = economy.compute_usd_cost(1_000_000, 1_000_000, "gpt-5")
    assert round(cost["totalCost"], 6) == round(1.25 + 10.0, 6)


def test_image_tokens():
    assert economy.image_tokens("chatgpt", {"width": 512, "height": 512}) == 85 + 170
    assert economy.image_tokens("gemini", {"width": 384, "height": 384}) == 258
    assert economy.image_tokens("claude", {}) == 1300


def test_account_turn_end_to_end():
    account = economy.account_turn(
        platform="claude",
        model="claude-opus-4-8",
        prompt_text="Write a python function.\njson output please",
        response_text="def f(): pass",
    )
    assert account.inputTokens > 0
    assert account.outputTokens > 0
    assert account.zaps > 0
    assert account.spend_usd > 0
    entry = economy.build_entry(account, timestamp="2026-01-01T00:00:00Z")
    assert set(entry) == {
        "model", "platform", "timestamp", "inputTokens", "outputTokens",
        "inputFilesCount", "outputFilesCount", "promptQuality",
    }


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-v"]))
