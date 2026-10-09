"""
Zap economy engine.

Faithful Python port of the client-side accounting functions found in the
Conso extension bridges (content-scripts/*-bridge.js). The extension computes
the zap credit, the USD spend and the prompt-quality score *locally* and then
hands them to the Supabase `append_prompt` RPC. Any automation that talks to
the backend directly must reproduce these numbers exactly, otherwise the
credit either differs from what the real client would submit or is rejected.

Ported functions (original minified names in parentheses):

  model_multiplier            (u)
  compute_zaps                (d)
  generated_image_tokens      (ee)
  compute_usd_cost            (te)
  model_price                 (h)
  quality_score               (le -> ce)
  quality_length_band         (re)
  quality_lexical             (ie)
  quality_structure           (oe)
  quality_combine             (se)
  count_words                 (ne)
  count_tokens                (Ts -> vs)
  image_tokens                (Ka -> Ua / Wa / Ga)
  round2                     (c)
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from . import constants as C

# ---------------------------------------------------------------------------
# Tokenizer. The extension bundles gpt-tokenizer with the o200k_base encoding.
# tiktoken reproduces that encoding exactly. If tiktoken is unavailable we fall
# back to a documented heuristic so the module still imports and runs.
# ---------------------------------------------------------------------------
try:  # pragma: no cover - exercised implicitly
    import tiktoken

    _ENCODING = tiktoken.get_encoding(C.TOKENIZER_ENCODING)
except Exception:  # pragma: no cover
    _ENCODING = None


def js_round(value: float) -> int:
    """JavaScript Math.round: round half toward +infinity."""
    return int(math.floor(value + 0.5))


def js_isoformat(dt: datetime) -> str:
    """Reproduce JavaScript `new Date().toISOString()` (UTC, millisecond, Z).

    The extension submits `timestamp: new Date().toISOString()` — e.g.
    `2026-10-01T18:17:11.429Z`. Python's `datetime.isoformat()` yields
    `+00:00` and microsecond precision, which the backend rejects/mis-handles
    (observed: the turn is recorded with 0 credit and the account is then
    banned). Always emit the JS form.
    """
    dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def round2(value: float) -> float:
    """JS `Math.round(e*100)/100` (bridge.js: c)."""
    return js_round(value * 100) / 100.0


def count_tokens(text: str) -> int:
    """Token count (bridge.js: Ts -> vs, o200k_base)."""
    if not text:
        return 0
    if _ENCODING is not None:
        return len(_ENCODING.encode(text))
    # Fallback heuristic: ~4 chars/token, only used if tiktoken is missing.
    return max(1, js_round(len(text) / 4))


# ---------------------------------------------------------------------------
# Model / platform weighting
# ---------------------------------------------------------------------------
def model_multiplier(model: str, platform: str | None = None) -> float:
    """bridge.js: u(e, t). Exact match -> suffix-stripped match -> longest
    substring match -> platform fallback -> global default."""
    table = C.MODEL_MULTIPLIER
    if model in table:
        return table[model]

    normalized = re.sub(r"-\d{8}$", "", model)
    if normalized in table:
        return table[normalized]

    best: str | None = None
    for key in table:
        if "-" in key and key in normalized:
            if best is None or len(key) > len(best):
                best = key
    if best is not None:
        return table[best]

    if platform and platform in C.PLATFORM_MULTIPLIER:
        return C.PLATFORM_MULTIPLIER[platform]
    return C.DEFAULT_MODEL_MULTIPLIER


def compute_zaps(
    input_tokens: int,
    output_tokens: int,
    model: str,
    platform: str,
    media_tokens: int = 0,
    quality: float = 1.0,
) -> float:
    """bridge.js: d(e, t, n, i, a, l) -> round2(...).

    zaps = round2( ((in + out - media)/1e4 * quality * modelMult * 2.5)
                 + (media/1e4 * quality * modelMult * 0.5) )
    """
    mult = model_multiplier(model, platform)
    primary = (
        (input_tokens + output_tokens - media_tokens)
        / C.TOKEN_DIVISOR
        * quality
        * mult
        * C.OUTPUT_ZAP_FACTOR
    )
    media_part = media_tokens / C.TOKEN_DIVISOR * quality * mult * C.MEDIA_ZAP_FACTOR
    return round2(primary + media_part)


# ---------------------------------------------------------------------------
# USD spend
# ---------------------------------------------------------------------------
def model_price(model: str) -> dict[str, float]:
    """bridge.js: h(e). `auto` -> gpt-5 table, unknown -> sonnet-5 table."""
    if model == "auto":
        return C.PRICE_FALLBACK_AUTO
    return C.MODEL_PRICE_PER_MILLION.get(model, C.PRICE_FALLBACK_UNKNOWN)


def compute_usd_cost(input_tokens: int, output_tokens: int, model: str) -> dict[str, float]:
    """bridge.js: te(e, t, n) -> {inputCost, outputCost, totalCost}."""
    price = model_price(model)
    input_cost = input_tokens / 1e6 * price["inputPerMillion"]
    output_cost = output_tokens / 1e6 * price["outputPerMillion"]
    return {
        "inputCost": input_cost,
        "outputCost": output_cost,
        "totalCost": input_cost + output_cost,
    }


def generated_image_tokens(model: str, image_count: int) -> int:
    """bridge.js: ee(e, t). Token-equivalent credited for generated images."""
    if image_count <= 0:
        return 0
    price = model_price(model)
    usd = image_count * C.IMAGE_TOKEN_USD
    return js_round(usd / price["outputPerMillion"] * 1e6)


# ---------------------------------------------------------------------------
# Prompt quality (bridge.js: ne / re / ie / oe / se / ce / le)
# ---------------------------------------------------------------------------
_WORD_RE = re.compile(r"\b[a-z']+\b")
_STRUCTURE_RULES = [
    (re.compile(r"\n"), 0.4, "Multi-line prompt"),
    (re.compile(r"\b(json|xml|csv|markdown|table|list)\b", re.IGNORECASE), 0.6,
     "Output format specified"),
]


def count_words(text: str) -> list[str]:
    """bridge.js: ne(e). Lowercased word tokens."""
    return _WORD_RE.findall(text.lower())


@dataclass
class QualityBand:
    score: float
    label: str
    band: str


def quality_length_band(word_count: int) -> QualityBand:
    """bridge.js: re(e) keyed on the number of words."""
    t = word_count
    if t < 3:
        return QualityBand(0.0, f"{t} tokens - trivial", "trivial")
    if t < 6:
        return QualityBand(0.03, f"{t} tokens - very short", "very_short")
    if t < 12:
        return QualityBand(0.10, f"{t} tokens - brief", "brief")
    if t < 20:
        return QualityBand(0.25, f"{t} tokens - short", "short")
    if t < 35:
        return QualityBand(0.45, f"{t} tokens - moderate", "moderate")
    if t < 60:
        return QualityBand(0.65, f"{t} tokens - substantial", "substantial")
    if t < 100:
        return QualityBand(0.80, f"{t} tokens - detailed", "detailed")
    if t < 150:
        return QualityBand(0.91, f"{t} tokens - long-form", "long")
    if t < 250:
        return QualityBand(0.97, f"{t} tokens - comprehensive", "comprehensive")
    return QualityBand(1.0, f"{t} tokens - expert-level", "expert")


@dataclass
class LexicalResult:
    score: float
    ttr: float
    unique: int


def quality_lexical(words: list[str]) -> LexicalResult:
    """bridge.js: ie(e). Type-token ratio bands."""
    if not words:
        return LexicalResult(0.0, 0.0, 0)
    unique = len(set(words))
    ttr = unique / len(words)
    if ttr < 0.25:
        score = 0.2
    elif ttr < 0.40:
        score = 0.5
    elif ttr < 0.55:
        score = 0.75
    elif ttr < 0.70:
        score = 0.9
    else:
        score = 1.0
    return LexicalResult(score, float(f"{ttr:.3f}"), unique)


@dataclass
class StructureResult:
    score: float
    signals: list[str] = field(default_factory=list)


def quality_structure(text: str) -> StructureResult:
    """bridge.js: oe(e)."""
    total = 0.0
    signals: list[str] = []
    for pattern, value, label in _STRUCTURE_RULES:
        if pattern.search(text):
            total += value
            signals.append(label)
    return StructureResult(min(total, 1.0), signals)


def quality_combine(
    length: QualityBand, lexical: LexicalResult, structure: StructureResult
) -> float:
    """bridge.js: se(e, t, n) -> +Math.min(1 + r**0.45 * 4, 5).toFixed(1)."""
    weighted = length.score * 0.6 + structure.score * 0.3 + lexical.score * 0.1
    combined = min(1.0 + (weighted ** 0.45) * 4.0, 5.0)
    return float(f"{combined:.1f}")


@dataclass
class QualityBreakdown:
    q: float
    length: QualityBand
    lexical: LexicalResult
    structure: StructureResult


def quality_breakdown(text: str) -> QualityBreakdown:
    """bridge.js: ce(e)."""
    words = count_words(text)
    length = quality_length_band(len(words))
    lexical = quality_lexical(words)
    structure = quality_structure(text)
    return QualityBreakdown(quality_combine(length, lexical, structure), length, lexical, structure)


def quality_score(text: str) -> float:
    """bridge.js: le(e)."""
    return quality_breakdown(text).q


# ---------------------------------------------------------------------------
# Image token estimation (bridge.js: Ka dispatch -> Ua / Wa / Ga)
# ---------------------------------------------------------------------------
def _claude_image_tokens(width: int | None, height: int | None) -> int:
    """bridge.js: Wa(e)."""
    if not width or not height:
        return C.IMG_TOKENS_CLAUDE_DEFAULT
    w, h = width, height
    longest = max(w, h)
    if longest > 1568:
        scale = 1568 / longest
        w, h = js_round(w * scale), js_round(h * scale)
    return math.ceil(w * h / 750)


def _gemini_image_tokens(width: int | None, height: int | None) -> int:
    """bridge.js: Ga(e)."""
    if not width or not height:
        return C.IMG_TOKENS_GEMINI_DEFAULT
    if width <= 384 and height <= 384:
        return 258
    return math.ceil(width / 768) * math.ceil(height / 768) * 258


def _gpt_image_tokens(width: int | None, height: int | None) -> int:
    """bridge.js: Ua(e) -- chatgpt / perplexity."""
    if not width or not height:
        return C.IMG_TOKENS_GPT_DEFAULT
    w, h = width, height
    longest = max(w, h)
    if longest > 2048:
        scale = 2048 / longest
        w, h = js_round(w * scale), js_round(h * scale)
    shortest = min(w, h)
    if shortest > 768:
        scale = 768 / shortest
        w, h = js_round(w * scale), js_round(h * scale)
    return 85 + 170 * (math.ceil(w / 512) * math.ceil(h / 512))


def image_tokens(platform: str, image: dict[str, Any]) -> int:
    """bridge.js: Ka(platform, image). image = {"width": int, "height": int}."""
    width = image.get("width")
    height = image.get("height")
    if platform == "claude":
        return _claude_image_tokens(width, height)
    if platform == "gemini":
        return _gemini_image_tokens(width, height)
    return _gpt_image_tokens(width, height)


# ---------------------------------------------------------------------------
# Full turn accounting (bridge.js: Ds / appendPrompt payload)
# ---------------------------------------------------------------------------
@dataclass
class TurnAccount:
    model: str
    platform: str
    inputTokens: int
    outputTokens: int
    inputFilesCount: int
    outputFilesCount: int
    promptQuality: float
    zaps: float
    spend_usd: float
    media_tokens: int


def account_turn(
    *,
    platform: str,
    model: str,
    prompt_text: str,
    response_text: str,
    prompt_images: list[dict[str, Any]] | None = None,
    response_images: list[dict[str, Any]] | None = None,
    response_media_text: str = "",
    input_files_count: int = 0,
    has_non_image_attachment: bool = False,
    generated_files_count: int = 0,
) -> TurnAccount:
    """Reproduce the exact token/zap/USD accounting of bridge.js Ds().

    Returns the values the client would submit to `append_prompt`, where
    `zaps` is the credited amount (already including the 3x attachment
    multiplier) and `spend_usd` is the USD cost passed as `p_spend_usd`.
    """
    prompt_images = prompt_images or []
    response_images = response_images or []

    prompt_image_tokens = sum(image_tokens(platform, img) for img in prompt_images)
    input_tokens = count_tokens(prompt_text) + prompt_image_tokens

    response_image_count = len(response_images)
    media_token_equiv = generated_image_tokens(model, response_image_count) + count_tokens(
        response_media_text
    )
    output_tokens = count_tokens(response_text) + media_token_equiv

    output_files_count = response_image_count + generated_files_count
    media_tokens = prompt_image_tokens + media_token_equiv

    quality = quality_score(prompt_text)
    zaps = compute_zaps(input_tokens, output_tokens, model, platform, media_tokens, quality)
    if has_non_image_attachment:
        zaps = round2(zaps * C.NON_IMAGE_ATTACHMENT_MULTIPLIER)

    spend = compute_usd_cost(input_tokens, output_tokens, model)

    return TurnAccount(
        model=model,
        platform=platform,
        inputTokens=input_tokens,
        outputTokens=output_tokens,
        inputFilesCount=input_files_count,
        outputFilesCount=output_files_count,
        promptQuality=quality,
        zaps=zaps,
        spend_usd=spend["totalCost"],
        media_tokens=media_tokens,
    )


def build_entry(
    account: TurnAccount,
    *,
    timestamp: str,
) -> dict[str, Any]:
    """Build the `p_entry` object exactly as bridge.js Ds() does."""
    return {
        "model": account.model,
        "platform": account.platform,
        "timestamp": timestamp,
        "inputTokens": account.inputTokens,
        "outputTokens": account.outputTokens,
        "inputFilesCount": account.inputFilesCount,
        "outputFilesCount": account.outputFilesCount,
        "promptQuality": account.promptQuality,
    }


# ---------------------------------------------------------------------------
# Ceiling-aware target sizing (see analysis/REFERENCE_STUDY.md)
#
# The server multiplies the submitted p_base_zaps by the account's
# boost_factor before crediting, and credits 0 when the product exceeds
# CREDIT_CEILING. So the value to SEND is  target / boost_factor, capped so the
# sent figure itself stays under the ceiling.
# ---------------------------------------------------------------------------
def send_zaps_for_target(target_credited: float, boost_factor: float,
                         ceiling: float = C.CREDIT_CEILING) -> float:
    """p_base_zaps to submit so the credited result lands on `target_credited`.

    A 5% margin is kept below the ceiling: `sent * boost` must stay strictly
    under it, or the server credits 0 (or a negative delta) — rounding at the
    boundary otherwise overshoots (e.g. 3.05 * 1.05 = 3.203 > 3.2).
    """
    boost = boost_factor or 1.0
    safe = ceiling * 0.95
    want = min(target_credited, safe)
    return round(min(want, want / boost, safe) * 100) / 100


def scale_tokens_for_zaps(
    account: TurnAccount,
    *,
    platform: str,
    model: str,
    target_zaps: float,
    quality: float | None = None,
    has_non_image_attachment: bool = False,
) -> TurnAccount:
    """Rescale an account's token counts so its base zaps ≈ `target_zaps`.

    Tokens drive zaps linearly, so scaling both input and output by
    `target / current` moves base_zaps to the target (within rounding).
    """
    current = compute_zaps(
        account.inputTokens, account.outputTokens, model, platform,
        account.media_tokens, quality if quality is not None else account.promptQuality,
    )
    if has_non_image_attachment:
        current = round2(current * C.NON_IMAGE_ATTACHMENT_MULTIPLIER)
    if current <= 0 or target_zaps <= 0:
        return account
    scale = target_zaps / current
    account.inputTokens = max(1, js_round(account.inputTokens * scale))
    account.outputTokens = max(1, js_round(account.outputTokens * scale))
    # Recompute the derived figures for the new token counts — the caller sends
    # account.zaps / account.spend_usd, so they MUST reflect the scaled tokens.
    q = quality if quality is not None else account.promptQuality
    zaps = compute_zaps(
        account.inputTokens, account.outputTokens, model, platform,
        account.media_tokens, q,
    )
    if has_non_image_attachment:
        zaps = round2(zaps * C.NON_IMAGE_ATTACHMENT_MULTIPLIER)
    account.zaps = zaps
    account.spend_usd = compute_usd_cost(
        account.inputTokens, account.outputTokens, model
    )["totalCost"]
    return account
