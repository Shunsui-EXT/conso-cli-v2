"""
Conso protocol constants.

Every value in this module was extracted verbatim from the Conso browser
extension v0.1.4.0 (unpacked at extension_original/unpacked). Do not tune
these by hand: they define the client-side zap accounting contract that the
Supabase `append_prompt` RPC is expected to validate against.

Source files:
  - content-scripts/{chatgpt,claude,gemini,perplexity}-bridge.js
  - background.js
"""

# ---------------------------------------------------------------------------
# Backend endpoints
# ---------------------------------------------------------------------------
SUPABASE_URL = "https://jzxlayjrsdbyzykuiqns.supabase.co"
SUPABASE_PUBLISHABLE_KEY = "sb_publishable_clAiRg6ffCznEAtg_bn19Q_yY0W5Hyd"

CONSO_WEB = "https://www.conso.xyz"
CONSO_X_API = "https://www.conso.xyz/api/extension/x"
CONSO_VERIFY_HUMAN = "https://www.conso.xyz/verify-human"

# OAuth client used by the extension. Kept for reference only; automation does
# not reuse it (see auth module) because the redirect URI is bound to the
# extension ID and cannot be reproduced outside a Chrome extension context.
GOOGLE_OAUTH_CLIENT_ID = (
    "77519766304-hfrhb5gc5dp1kqano1ghsgroa2rf3ei1.apps.googleusercontent.com"
)
GOOGLE_OAUTH_SCOPES = ["openid", "email", "profile"]

# ---------------------------------------------------------------------------
# Zap economy coefficients  (bridge.js: r, i, a, o, s, g)
# ---------------------------------------------------------------------------
TOKEN_DIVISOR = 10_000.0          # r  -- tokens per "unit"
DEFAULT_MODEL_MULTIPLIER = 0.25   # i  -- fallback model weight
PLATFORM_MULTIPLIER = {           # a  -- fallback weight by platform
    "chatgpt": 0.3,
    "claude": 0.3,
    "gemini": 0.1,
    "perplexity": 0.3,
}
OUTPUT_ZAP_FACTOR = 2.5           # o  -- output token zap weight
MEDIA_ZAP_FACTOR = 0.5            # s  -- generated-media token zap weight
IMAGE_TOKEN_USD = 0.04            # g  -- USD value of one image token

# ---------------------------------------------------------------------------
# Model -> zap multiplier  (bridge.js: l)
# ---------------------------------------------------------------------------
MODEL_MULTIPLIER = {
    "auto": 0.3,
    "gpt-5-5-instant": 0.6,
    "gpt-5-5-thinking": 0.7,
    "gpt-5-6": 0.6,
    "gpt-5-6-thinking": 0.7,
    "claude-3-opus": 0.5,
    "claude-opus-4-6": 0.6,
    "claude-opus-4-7": 0.7,
    "claude-opus-4-8": 0.7,
    "claude-haiku-4-5": 0.3,
    "claude-sonnet-4-6": 0.4,
    "claude-sonnet-5": 0.7,
    "claude-opus-5": 0.7,
    "claude-fable-5": 1.4,
    "gemini-3-flash": 0.25,
    "gemini-3-pro": 0.56,
    "gemini-3.5-flash-lite": 0.1,
    "gemini-3.6-flash": 0.25,
    "gemini-3.6-thinking": 0.6,
    "gemini-3.1-pro": 0.7,
    "turbo": 0.4,
    "gpt56_terra": 0.6,
    "experimental": 0.3,
    "gemini37flash": 0.3,
    "claude50sonnet": 0.7,
    "kimik3thinking": 0.56,
    "glm_5_2": 0.7,
    "grok46low": 0.65,
    "nv_nemotron_3_ultra": 0.63,
    "pplx_asi": 0.5,
    "pplx_asi_opus": 0.7,
    "pplx_asi_glm": 0.7,
    "pplx_asi_deepseek_v4_pro": 0.67,
    "pplx_asi_kimi_k3": 0.56,
    "pplx_asi_grok_46": 0.63,
    "pplx_asi_fable_5": 1.4,
    "pplx_asi_gpt_56_sol": 0.67,
    "pplx_asi_sonnet": 0.7,
    "pplx_alpha": 0.45,
}

# ---------------------------------------------------------------------------
# Model -> USD price per million tokens  (bridge.js: f, p, m)
# ---------------------------------------------------------------------------
MODEL_PRICE_PER_MILLION = {
    "claude-opus-4": {"inputPerMillion": 5.0, "outputPerMillion": 25.0},
    "claude-sonnet-5": {"inputPerMillion": 3.0, "outputPerMillion": 15.0},
    "claude-haiku-4": {"inputPerMillion": 1.0, "outputPerMillion": 5.0},
    "gpt-5": {"inputPerMillion": 1.25, "outputPerMillion": 10.0},
    "gpt-4.1": {"inputPerMillion": 2.0, "outputPerMillion": 8.0},
    "gpt-4o": {"inputPerMillion": 2.5, "outputPerMillion": 10.0},
    "gpt-4o-mini": {"inputPerMillion": 0.15, "outputPerMillion": 0.6},
}
# bridge.js: p = f["gpt-5"] (used when model == "auto")
PRICE_FALLBACK_AUTO = MODEL_PRICE_PER_MILLION["gpt-5"]
# bridge.js: m (used for unknown models)
PRICE_FALLBACK_UNKNOWN = {"inputPerMillion": 3.0, "outputPerMillion": 15.0}

# ---------------------------------------------------------------------------
# Image token cost constants  (bridge.js: Ba, Va, Ha)
# ---------------------------------------------------------------------------
IMG_TOKENS_GPT_DEFAULT = 255       # Ba -- chatgpt / perplexity fallback
IMG_TOKENS_CLAUDE_DEFAULT = 1300   # Va -- claude fallback
IMG_TOKENS_GEMINI_DEFAULT = 516    # Ha -- gemini fallback (258 * 2)

# ---------------------------------------------------------------------------
# Non-image attachment multiplier  (bridge.js: f = round2(zaps * 3))
# ---------------------------------------------------------------------------
NON_IMAGE_ATTACHMENT_MULTIPLIER = 3.0

# ---------------------------------------------------------------------------
# Anti-abuse: per-platform processed-prompt dedup cache
#   chrome.storage.local key: "conso:processedPrompts:" + platform
#   bounded ring buffer of the last 500 requestKeys
# ---------------------------------------------------------------------------
DEDUP_KEY_PREFIX = "conso:processedPrompts:"
DEDUP_MAX_KEYS = 500

# ---------------------------------------------------------------------------
# Tokenizer: the extension bundles gpt-tokenizer using the o200k_base
# encoding (bridge.js: getEncodingApi("o200k_base", ...)). The Python port
# mirrors this with tiktoken's "o200k_base".
# ---------------------------------------------------------------------------
TOKENIZER_ENCODING = "o200k_base"

# ---------------------------------------------------------------------------
# Anti-abuse limits (empirically measured against the live backend)
#   - Daily turn cap: the server credits 0 from turn ~11 onward and bans if the
#     account keeps submitting. Stop at the first zero-credit turn.
#   - Mission claims are idempotent per day.
# ---------------------------------------------------------------------------
DAILY_TURN_CAP = 10          # credited turns per account per day (measured)

# Server-side zap accounting limits (measured; see analysis/REFERENCE_STUDY.md).
#   - The server multiplies the submitted p_base_zaps by the account's
#     boost_factor before crediting, and CREDITS 0 when that product exceeds
#     CREDIT_CEILING (a soft flag that precedes a ban).
#   - The practical daily budget is ~21 zaps; the hard server limit is ~30.
# Size each turn so base_zaps * boost_factor lands just under the ceiling.
CREDIT_CEILING = 3.2
DAILY_ZAP_CAP = 21.0
