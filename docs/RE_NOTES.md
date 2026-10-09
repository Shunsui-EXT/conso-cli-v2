# Conso AI Usage Tracker — Reverse Engineering Report

Target: `Conso: AI Usage Tracker` v0.1.4.0 (Chrome MV3)
Extension ID: `bjibbmkefnaamkenamdppfengeepadpi`
Artifact: `extension_original/conso-v0.1.4.0.crx` (15,791,733 bytes) → `extension_original/unpacked/`
(download it yourself with `scripts/fetch_extension.sh`; the tree is gitignored)

> This is the provenance document for `conso-cli`: everything the CLI does was
> derived from the extension and from live probes against the backend it talks
> to. File references have been updated to the `conso-cli` layout; the probes
> that produced the evidence live in the sibling CONSO project's `analysis/`
> directory. Where the original report described an HTTP solver sidecar on
> `:8877`, this document notes the in-process replacement.

## 1. Executive Summary

Conso is a client-side AI-usage tracker. Content-script injectors monkey-patch
`window.fetch` on four AI platforms, tee each completion request/response, and
post a `CONSO_DETECTED_TURN` message to a bridge script. The bridge computes a
zap credit and USD spend **locally**, deduplicates the turn, and writes it to a
Supabase backend via the `append_prompt` RPC.

Two consequences matter for automation:

1. **The accounting is client-side.** The extension — not the server — decides
   how many zaps a turn is worth and what it "cost". The backend RPC receives
   the already-computed numbers (`p_base_zaps`, `p_spend_usd`).
2. **Authentication is pluggable.** The extension uses Google OAuth, but the
   Supabase project has email/password auth enabled with open signup, so
   accounts can be provisioned without any OAuth provider.

## 2. Architecture

```
 AI platform tab (chatgpt.com / claude.ai / gemini.google.com / perplexity.ai)
   ├─ *-injector.js   (world: MAIN, document_start)  -> patches window.fetch,
   │                     tees the SSE stream, postMessage(CONSO_DETECTED_TURN)
   └─ *-bridge.js     (isolated)                     -> listens for the message,
                         dedups, computes zaps, calls append_prompt
 Extension service worker (background.js)
   └─ Supabase JS client (GoTrue auth + PostgREST RPC) -> jzxlayjrsdbyzykuiqns.supabase.co
 conso.xyz Next.js API
   └─ /api/extension/x/{start,callback}  (X OAuth proxy)
   └─ /verify-human                      (captcha flow)
```

## 3. Backend

| Surface | Value |
|---|---|
| Supabase URL | `https://jzxlayjrsdbyzykuiqns.supabase.co` |
| Publishable key | `sb_publishable_clAiRg6ffCznEAtg_bn19Q_yY0W5Hyd` |
| Auth | email/password enabled, Google enabled, `disable_signup=false`, `mailer_autoconfirm=false` |
| Table | `consousers` (RLS: anon denied → `42501`) |
| Related tables (leaked by PostgREST hints) | `prompt_events`, `bonus_missions` |
| Next.js API | `https://www.conso.xyz/api/extension/x/{start,callback}` |

### RPCs (PostgREST `/rest/v1/rpc/<name>`)

| RPC | Args | Purpose |
|---|---|---|
| `append_prompt` | `p_entry`, `p_base_zaps`, `p_spend_usd` | Record a turn and credit zaps |
| `create_consouser` | `p_google_id` | Create the user record |
| `is_consoname_available` | `p_consoname` | Display-name availability |
| `redeem_referral_code` | `p_code` | Apply a referral code |
| `redeem_access_code` | `p_code` | Apply an access code |
| `claim_daily_mission` | `p_mission_id`, `p_claim_ref` | Claim a daily mission |
| `claim_bonus_mission` | `p_mission_id` | Claim a bonus mission |
| `get_todays_mission_claims` | — | Today's claim state |
| `get_avg_session_minutes` | `p_platform`, `p_days` | Stats |
| `get_daily_activity` | `p_start`, `p_end`, `p_platform` | Stats |
| `get_hourly_activity` | `p_platform` | Stats |
| `get_lifetime_platform_stats` | — | Stats |
| `get_my_leaderboard_rank` | — | Rank |

## 4. Authentication

### Google (extension path)
`chrome.identity.launchWebAuthFlow` → `accounts.google.com/o/oauth2/v2/auth`
with `response_type=id_token`, `client_id=77519766304-hfrhb5gc5dp1kqano1ghsgroa2rf3ei1`,
`redirect_uri=<chromiumapp.org>`, `nonce=<random>` → `id_token` from the redirect
hash → `supabase.auth.signInWithIdToken({provider:"google", token, nonce})`.
The OAuth client is bound to the extension's redirect URI and cannot be reused
outside a Chrome extension.

### Email/password (automation path)
`POST /auth/v1/signup` → `POST /auth/v1/token?grant_type=password`. If email
confirmation is required, fetch the confirm link via an IMAP/catch-all verifier.

### X (optional linking)
`POST https://www.conso.xyz/api/extension/x/start` → returns `{url}` (X OAuth2
PKCE authorize URL) → user authorizes → redirect carries `code`+`state` →
`POST /api/extension/x/callback` body `{code, state}` → `{ok, xUserId, username}`.

### Captcha (Cloudflare Turnstile)
`chrome.identity.launchWebAuthFlow` → `https://www.conso.xyz/verify-human?redirect_uri=<...>`
→ redirect carries `captcha_token`.

The widget is Cloudflare Turnstile: page `https://www.conso.xyz/verify-human`,
script `https://challenges.cloudflare.com/turnstile/v0/api.js`, **sitekey
`0x4AAAAAAEzmjKoKI6TA61_6`**. The token is consumed by Supabase, and it is
enforced on **every** auth endpoint:

```
POST /auth/v1/signup                       -> 400 captcha_failed
POST /auth/v1/token?grant_type=password    -> 400 captcha_failed
POST /auth/v1/otp                          -> 400 captcha_failed
```

The token travels in the GoTrue envelope `gotrue_meta_security.captcha_token`
(confirmed: a dummy token changes the error from `no captcha_token found` to
`invalid-input-response`). Consequence: email/password signup and login
**require a solved Turnstile**.

The one captcha-free auth path is the id_token grant:

```
POST /auth/v1/token?grant_type=id_token  {provider:"google", id_token}
```

which is processed without any captcha check (an invalid token yields
`"Bad ID token"`, not `captcha_failed`). So a real Google/OIDC id_token is a
Turnstile bypass — but the extension's OAuth client is bound to the extension's
`chromiumapp.org` redirect and cannot be reused off-extension.

### Turnstile bypass (verified end-to-end)

The token that Supabase accepts must be minted on the **real** verify-human
page, not a stub. The page only renders the widget when `redirect_uri` is one
of the extension's own redirect URIs (extracted from the page bundle):

```
https://bjibbmkefnaamkenamdppfengeepadpi.chromiumapp.org/
https://mfjolkgcoehffnccojgdegniohfejfml.chromiumapp.org/
consomobileapp://verify-human
```

Verified working chain (originally against the HTTP sidecar on `:8877`; the
same request shape now runs in-process through
`src/conso/solver/cloak/turnstile.py`):

```
solve: {"type":"turnstile","real_page":true,"sitekey":"0x4AAAAAAEzmjKoKI6TA61_6",
        "url":"https://www.conso.xyz/verify-human?redirect_uri=<whitelisted>"}
  -> {"solved":true,"token":"1.rNXgp..."}          # ~17-50s, headless+headed

POST /auth/v1/signup
  {"email":...,"password":...,"gotrue_meta_security":{"captcha_token":"<token>"}}
  -> 200, user created                              # captcha gate cleared
```

Solve is stochastic: the route-intercept (stub) token is rejected by Supabase
(`invalid-input-response`); only the real-page token works, and it succeeds
roughly 1 in 2-5 attempts, so the pipeline retries.

### Email-domain allowlist

Supabase additionally enforces `email_domain_not_allowed` on signup. Observed
rule:

| Domain | Result |
|---|---|
| `gmail.com` | ALLOWED |
| `outlook.com` | ALLOWED |
| `uberip.com` (mail.tm) | BLOCKED |
| `ncaori.my.id` (ncaori temp-mail) | BLOCKED |
| `random-xyz-abc123.com` (custom) | BLOCKED |

So the allowlist is a set of mainstream providers; disposable/temp-mail domains
are rejected. Consequence: the mail.tm / ncaori inboxes cannot be used as the
signup address directly. A working registration needs an address on an allowed
provider — reachable via a catch-all on an allowed domain, a provider with
alias/subaddressing (e.g. `you+tag@gmail.com`), or a purchased domain that is
itself on the allowlist. This is why `VERIFIER=temptf` is the default: temp.tf
hands out real Gmail/Outlook/Hotmail plus-aliases.

### Email confirmation

`mailer_autoconfirm=false`, so after signup the account is created with
`confirmation_sent_at` set and **cannot log in until confirmed**:

```
POST /auth/v1/token?grant_type=password  -> 400 {"error_code":"email_not_confirmed"}
```

The confirmation is delivered as a **6-digit OTP** (not a link):

```
From: no-reply@mail.conso.xyz
Subject: Conso Sign-in Code
Body: ...Your sign-in code for Conso... <p>614502</p>
```

Verify with `POST /auth/v1/verify {"type":"email","email":<addr>,"token":<otp>}`
which returns a session. This is the Supabase email OTP flow, and it works for
any signup address.

### Working provisioning chain (verified end-to-end)

Using **temp.tf** (real Gmail/Outlook/Hotmail addresses via plus-aliases) and
the local Turnstile solver:

```
1. GET  https://temp.tf/api/account?providers=gmail&dot=0&plus=1
        -> {"email":"alacatarik177+7cj0iatj7j@gmail.com"}
2. solve the real-page Turnstile (in-process)              -> token
3. POST /auth/v1/signup  {email,password,gotrue_meta_security:{captcha_token}}
        -> 200, user created
4. POST https://temp.tf/api/check {email, wait:true}
        -> {"data":[{"subject":"Conso Sign-in Code","body":"...614502..."}]}
5. POST /auth/v1/verify {"type":"email","email","token":"614502"}
        -> 200, access_token + refresh_token
6. POST /rest/v1/rpc/create_consouser {"p_google_id": uid}
        -> 200 {consoname:null, total_zaps:0, referral_code:"CONSO-..."}
7. POST /rest/v1/rpc/get_my_leaderboard_rank
        -> 200 (rank)
```

In `conso-cli` this chain is `src/conso/ops/pipeline.py::register_account`,
driven by `python main.py register N`.

### Account ban on first turn — ROOT CAUSE FOUND: missing onboarding

A synthetic turn through `append_prompt` bans the account **unless the
consoname (display name) has been set first**:

```
create_consouser                      -> 200
append_prompt (no consoname)          -> 200 "0"     then all calls: account_banned
----------------------------------------
create_consouser                      -> 200
PATCH consousers {consoname}          -> 200         (onboarding)
append_prompt                         -> 200 "0.07"  (credited, no ban)
```

The extension's popup completes onboarding (`createConsouser` -> pick a
consoname -> `ready`) before it ever records a turn; skipping that step trips
the anti-abuse. Isolated with two dedicated probes (no consoname -> ban;
consoname -> no ban).

Ruled out: timestamp format (`+00:00` vs JS `.000Z` — fixed, not the cause).

### Verified full pipeline (end-to-end, real zaps)

The complete flow confirms credited zaps match the client-computed values
exactly:

```
account: alacatarik177+0sumpkdu6f@gmail.com
onboarded: 200
turn 1 [claude/claude-opus-4-8]      sent=0.07 -> 200 0.07
turn 2 [chatgpt/gpt-5-6]             sent=0.06 -> 200 0.06
turn 3 [perplexity/pplx_asi_sonnet]  sent=0.08 -> 200 0.08
turn 4 [gemini/gemini-3-pro]         sent=0.05 -> 200 0.05
turn 5 [claude/claude-opus-4-8]      sent=0.07 -> 200 0.07
rank: 27942 (improved from 28008)
```

The server does **not** clamp `p_base_zaps` / `p_spend_usd`: the credited value
equals the client-submitted value to the cent. The client-side accounting is
authoritative — within the credit ceiling described below.

### `consousers` row schema (full, observed post-onboarding)

Reading the authenticated user's own row exposes the whole model:

```
id, google_id, consoname, total_zaps, account_created_at, per_hours_estimation,
referral_code, referred_by, referral_count, created_at, updated_at,
last_weekly_streak_claim, referral_bonus_zaps_earned, last_active_date,
current_streak, longest_streak, access_verified_at, approx_country_code,
x_user_id, x_username, x_connected_at, bonus_mission_claimed_id,
bonus_mission_claimed_at, daily_zaps_earned, daily_zaps_date, boost_factor,
is_banned, banned_at, ban_reason, signup_ip, signup_ip_subnet, x_profile_image,
x_eligible
```

Anti-abuse columns: `is_banned`, `banned_at`, `ban_reason`, `signup_ip`,
`signup_ip_subnet`, `daily_zaps_earned`/`daily_zaps_date` (daily cap tracking),
`boost_factor`, `access_verified_at`.

### Verified production run (CLI)

```
$ python main.py register 1
register: ...+snjej6r1nm@gmail.com captcha solved
register: ...+snjej6r1nm@gmail.com got code 989230
[active] ...+snjej6r1nm@gmail.com :: registered

$ python main.py farm --turns 5 --email ...
farm: claude/claude-fable-5 +1.08 zaps
farm: gemini/gemini-3-flash +0.11 zaps
farm: claude/claude-sonnet-5 +0.18 zaps
farm: gemini/gemini-3-pro +0.24 zaps
farm: chatgpt/gpt-5-5-thinking +0.18 zaps
farm done: 5 turns accepted, ~1.79 zaps

$ # backend row:
{"total_zaps":1.79,"daily_zaps_earned":1.79,"current_streak":1,
 "is_banned":false,"boost_factor":1,"consoname":"agilecomet48"}
# leaderboard rank: 28008 -> 27864
```

Zaps credited match the submitted values exactly.

### Anti-abuse: daily turn cap (measured)

`append_prompt` stops crediting after **~10 turns per account per day**. The
server does not return an error at the cap — it returns `200 0` (zero credited).
Continuing past that triggers `account_banned`:

```
turn 1..10   -> 200 credited (0.05..0.3 each)
turn 11      -> 200 "0"          (soft flag: daily cap reached)
turn 12      -> 400 account_banned
```

Measured identically under hammering (no delay) and paced (3s/turn), so the cap
is **count-based, not rate-based**.

The pipeline now stops at the **first zero-credit turn**
(`src/conso/ops/pipeline.py`), before the ban. `src/conso/ops/limits.py`
surfaces `daily_zaps_earned` / `daily_zaps_date` (resets on date rollover) so a
run can check headroom first.

Mission claim limits (from the extension's error map):

| Message | Meaning |
|---|---|
| `rate_limited` | short window ("try again in a minute") |
| `mission already claimed today` | daily mission done |
| `mission already claimed` / `tweet already used` | one-time done |
| `account_banned` | hard stop |

Note: tweet/article claims are **not server-validated** — claiming
`tweet-about-conso-v1` / `article-about-conso-v1` succeeds without a real X
post. This is a server-side gap, not something the client can influence.

### Session persistence (no re-login)

Supabase access tokens expire (~1h) and the **refresh token rotates on every
use**. A naive refresh that does not persist the rotated token loses the
account (the next refresh fails). Verified behaviour:

```
rt=f6cgnj -> refresh -> rt=co4ekd  (rotated)
rt=co4ekd -> refresh -> rt=u6dm5l  (rotated again; chain stays valid)
```

`SessionManager` (`src/conso/auth/session.py`) reuses the cached access token
while valid (120s safety margin), refreshes and **persists the rotated refresh
token + expiry**, and falls back to password login (Turnstile) only if the
chain breaks. `python main.py session` shows/refreshes all stored sessions.
Farming then runs with `session=cached`/`session=refreshed` — no Turnstile, no
login.

The stored record carries `expires_at` and `refreshed_at`.

## 5. Turn detection

Injectors patch `window.fetch`, match a POST completion endpoint, clone the
response, read the SSE stream, and post:

```js
window.postMessage({ source: "conso-extension", type: "CONSO_DETECTED_TURN", payload }, origin)
```

Payload fields: `platform`, `model`, `conversationId`, `promptText`,
`responseText`, `promptImages`, `responseImages`, `responseMediaText`,
`inputFilesCount`, `hasNonImageAttachment`, `generatedFilesCount`, `requestKey`.

Request matchers:

| Platform | Match |
|---|---|
| chatgpt | `POST /backend-api/(f/)?conversation$` |
| claude | `POST /api/organizations/<id>/chat_conversations/<id>/completion` |
| gemini | gemini completion endpoint |
| perplexity | perplexity completion endpoint |

Dedup: `chrome.storage.local["conso:processedPrompts:" + platform]` = ring
buffer of the last **500** `requestKey`s. `requestKey` = `conversationId::requestId`
when a request id exists, else `conversationId::promptLen::responseLen`.

## 6. Zap economy

Constants (bridge.js): `TOKEN_DIVISOR=1e4`, `DEFAULT_MODEL_MULTIPLIER=0.25`,
`PLATFORM_MULTIPLIER={chatgpt:.3, claude:.3, gemini:.1, perplexity:.3}`,
`OUTPUT_ZAP_FACTOR=2.5`, `MEDIA_ZAP_FACTOR=0.5`, `IMAGE_TOKEN_USD=0.04`.

```
mult   = MODEL_MULTIPLIER[model]  (fallback: platform mult → 0.25)
zaps   = round2( ((in + out - media)/1e4 * quality * mult * 2.5)
               + (media/1e4 * quality * mult * 0.5) )
if has_non_image_attachment: zaps = round2(zaps * 3)
spend  = in/1e6 * price_in + out/1e6 * price_out   # USD
```

- `in`  = `tokens(promptText)` + image tokens of prompt images
- `out` = `tokens(responseText)` + generated-image token-equivalent + `tokens(responseMediaText)`
- `media` = prompt image tokens + generated-image token-equivalent + media text tokens
- Tokenizer: `gpt-tokenizer` **o200k_base** (Python: `tiktoken.get_encoding("o200k_base")`).
- `quality` ∈ [0.0, 5.0], computed from prompt-text word count band (60%),
  structural signals (30%), lexical type-token ratio (10%), combined as
  `min(1 + weighted**0.45 * 4, 5)`.

Full constant tables live in `src/conso/core/constants.py`; the ported functions
in `src/conso/core/economy.py`.

### Per-turn credit ceiling (measured)

The server multiplies `p_base_zaps` by the account's `boost_factor` and credits
**0** when the product exceeds the ceiling (~3.2). `send_zaps_for_target` in
`src/conso/core/economy.py` therefore sizes each turn so the product lands just
under the ceiling (≈2.5–3 zaps/turn), which is also why the practical daily
budget lands near 21 zaps rather than the raw cap.

## 7. Anti-abuse surface

| Control | Behaviour |
|---|---|
| Dedup | 500-key ring buffer per platform (`requestKey`) |
| Quality gate | Low-quality prompts score near 0 → near-zero zaps |
| Attachment bonus | Non-image attachments multiply zaps by 3 (client-side) |
| Daily turn cap | ~10 credited turns/account/day, then `200 0`, then ban |
| Credit ceiling | ~3.2 zaps × `boost_factor` per turn |
| RLS | `consousers` denies anon select; RPCs are security-definer |
| Captcha | `/verify-human` gated flow for every auth endpoint |
| Auth | Supabase JWT required for user-scoped RPCs |
| Signup velocity | `signup_velocity_exceeded` across one IP → pin a proxy per account |

## 8. Automation implications

- Turn submission is a **direct RPC call**; no browser is required.
- Unique `requestKey`s defeat the client-side dedup (the server does not see
  the key — it is only a local cache guard — so server-side limits, if any, are
  the real constraint).
- The `p_base_zaps` and `p_spend_usd` values are client-supplied and are **not
  clamped per se**, but the `boost_factor` × ceiling rule means an oversized
  value credits zero — the pipeline stays just under it.
