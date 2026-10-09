# Solver

Conso enforces Cloudflare Turnstile on every Supabase auth endpoint, so a
registration or a re-login needs a solved challenge. Everything in this document
lives under `src/conso/solver/`.

---

## 1. What was ported

`src/conso/solver/cloak/` is an **in-process port** of
[waguriagentic/captcha-solver](https://github.com/waguriagentic/captcha-solver).
The upstream project is a FastAPI service exposing a `/solve` endpoint; here the
same browser logic is a library, with no HTTP server, no SSRF guard and no auth
layer.

| This project | Upstream |
|---|---|
| `src/conso/solver/cloak/browser.py` | `common/browser.py` |
| `src/conso/solver/cloak/turnstile.py` | `turnstile/solve.py` |
| `src/conso/solver/cloak/cloudflare.py` | `cloudflare/solve.py` |
| `src/conso/solver/cloak/templates/turnstile_stub.html` | `turnstile/template.html` |

Three async entry points are preserved (minus the env-driven kwargs):

- `solve_turnstile(...)` — route-intercept stub-page solve;
- `solve_and_verify(...)` — the same, plus verification from the same session;
- `solve_turnstile_realpage(...)` — navigate the real page, run `pre_actions`,
  humanized-click the checkbox, read `cf-turnstile-response`.

Deviations from upstream:

- `browser_kwargs(prefix, proxy)` (env-driven module globals) became explicit
  `build_browser_kwargs(headless, geoip, proxy, humanize)` arguments —
  configuration lives in the solver constructor.
- A blocking sync wrapper (`CloakTurnstileSolver`) owns a dedicated asyncio-loop
  thread and a persistent browser, so the Conso worker pool can drive solves
  from blocking code paths.
- Conso-specific defaults: real-page solve mode, the Supabase-whitelisted
  `redirect_uri` appended automatically, and the `SOLVER_*` retry / cooldown /
  concurrency envelope matching the measured production configuration.

`src/conso/solver/camoufox.py` is the other in-process provider, ported
near-verbatim from the sibling CONSO project's `internal_solver.py` (same
copyright holder). It drives [Camoufox](https://camoufox.com) — a stealth
Firefox build — and is the provider with production evidence behind it.

### Licensing — read before redistributing

Upstream `waguriagentic/captcha-solver` ships **no `LICENSE`, no `COPYING`, no
`NOTICE`, and no SPDX header on any source file**. No license grant is therefore
in effect. The code is vendored in-process for **private use only** and must not
be redistributed outside this project without a written grant from the upstream
author. The full attribution table and deviation list live in
[`src/conso/solver/cloak/NOTICE.md`](../src/conso/solver/cloak/NOTICE.md).

The transitive runtime dependency, [CloakBrowser](https://pypi.org/project/cloakbrowser/),
is MIT-licensed and linked at runtime only — no CloakBrowser source is vendored.

---

## 2. How the chain works

```
CAPTCHA_PROVIDER=internal,camoufox,capsolver,2captcha
                 └────────┬───────┘  └──────┬───────┘
                  in-process browsers      managed HTTP APIs
```

`build_solver()` reads `CAPTCHA_PROVIDER` (comma or pipe separated), builds each
provider it can, and returns:

- the single provider, when only one is configured;
- a **`FallbackSolver`** when there are two or more — it tries providers in
  order and returns the first token, collecting `last_errors` for diagnostics;
- a `NoopSolver` when the chain is empty (it raises on first solve unless
  `CAPTCHA_TOKEN` is set).

Providers whose credentials or dependencies are missing are **skipped, not
fatal**: `capsolver` without `CAPSOLVER_API_KEY` silently drops out, so
`internal,capsolver` is safe before you buy a key. Unknown names are skipped the
same way. Default when `CAPTCHA_PROVIDER` is unset: `cloak,camoufox`
(`internal` is folded to `cloak`).

### Warm-up

`ensure_solver_ready(logger=...)` runs before a register/farm/loop. It launches
the browser providers listed in the chain so the first account does not pay
launch latency. It never raises and never starts a provider that is not in the
chain; managed APIs have nothing to start, so a chain of only those returns
immediately.

### Per-account proxy pinning

`pin_account_proxy(proxy)` forwards the account's HTTP proxy to every provider
that implements it (`cloak`, `camoufox`). The solve then shares an exit IP with
that account's signup calls, which is what spreads the server's
`signup_velocity_exceeded` across IPs. `FallbackSolver` skips providers that do
not define the method, so pinning is safe with mixed chains.

### Real-page solve and the redirect whitelist

Supabase rejects a stub-page token for the Conso sitekey with
`invalid-input-response`. The accepted token must be minted on the live page:

```
https://www.conso.xyz/verify-human?redirect_uri=<whitelisted>
sitekey = 0x4AAAAAAEzmjKoKI6TA61_6
```

The Next.js widget only mounts when `redirect_uri` is one of the extension's own
redirect URIs (see `CONSO_REDIRECT_URIS` in `solver/base.py`):

```
https://bjibbmkefnaamkenamdppfengeepadpi.chromiumapp.org/
https://mfjolkgcoehffnccojgdegniohfejfml.chromiumapp.org/
consomobileapp://verify-human
```

The cloak wrapper appends `CONSO_REDIRECT_URIS[0]` automatically when the caller
passes the Conso verify page without one. Without a whitelisted redirect the
widget never renders and solves hang until timeout.

### Fail-fast retries and cooldown

A rate-flagged IP never returns a token, so a solve is split into short attempts
instead of one long timeout:

```
attempt 1  -> direct IP / pinned proxy   (SOLVER_ATTEMPT_TIMEOUT, default 45s)
attempt 2  -> next proxy in the file     (round-robin)
attempt 3  -> next proxy                 (SOLVER_BACKOFF seconds apart, default 3s)
   ...     -> SOLVER_ATTEMPTS total (default 3)
```

Between solves, `SOLVER_SOLVE_DELAY` enforces a minimum gap. Cloudflare hands
out **one usable challenge flow per source IP**, so solves are additionally
serialised process-wide (`SOLVER_SERIAL=1`) — overlapping solves from one IP
make every participant time out. Set `SOLVER_SERIAL=0` only when each worker
exits through its own residential IP.

---

## 3. Providers

| `CAPTCHA_PROVIDER` value | Implementation | Requirement |
|---|---|---|
| `internal` (alias `cloak`) | `cloak/turnstile.py` — CloakBrowser (stealth Chromium) | `pip install cloakbrowser` + `python -m playwright install chromium` |
| `camoufox` | `camoufox.py` — Camoufox (stealth Firefox) | `camoufox[geoip]>=0.4.0` + `playwright==1.60` + `python -m camoufox fetch` |
| `capsolver` | `managed.py` — pure-HTTP Capsolver client (`CAPSOLVER_MODE=sdk` selects the official SDK) | `CAPSOLVER_API_KEY` |
| `2captcha` | `managed.py` — 2Captcha HTTP client | `TWOCAPTCHA_API_KEY` |
| `manual` | `managed.py` — operator pastes a token | `CAPTCHA_TOKEN` |
| `none` | `managed.py` — `NoopSolver` | `CAPTCHA_TOKEN` |

---

## 4. Env knobs

| Variable | Default | Meaning |
|---|---|---|
| `CAPTCHA_PROVIDER` | `cloak,camoufox` | Comma-chain; first token wins. |
| `CAPTCHA_TOKEN` | *(empty)* | Pre-solved token for `manual`/`none`. |
| `SOLVER_HEADLESS` | `1` | Run the browser headless (`0` to watch it). |
| `SOLVER_TIMEOUT` | `90` | Whole-solve budget in seconds. |
| `SOLVER_MAX_CONCURRENT` | `8` (`.env.example`; code default `2`) | Browser concurrency. 8 is the measured sweet spot; 16 overloads a single browser. |
| `SOLVER_SOLVE_DELAY` | `5` | Minimum seconds between solves (keeps the IP from being flagged). |
| `SOLVER_ATTEMPTS` | `3` | Attempts per solve before the account is marked failed. |
| `SOLVER_ATTEMPT_TIMEOUT` | `45` | Per-attempt budget in seconds. Lower = fail faster on a flagged IP. |
| `SOLVER_BACKOFF` | `3` | Seconds between attempts. |
| `SOLVER_SERIAL` | `1` | Serialise solves process-wide (one usable flow per IP). |
| `SOLVER_PROXY_FILE` | *(empty)* | Proxy list for the solver only (separate from `PROXY_FILE`). |
| `SOLVER_PROXY_MODE` | `attempt` | `attempt` = rotate per retry; `worker` = one sticky entry per worker thread. |
| `CLOAK_HEADLESS` | `1` | Cloak provider headless toggle (falls back to `SOLVER_HEADLESS`). |
| `CLOAK_GEOIP` | `0` | Attach a GeoIP DB so timezone/locale match the exit IP. |
| `CLOAK_MAX_CONCURRENT` | `2` | Cloak sessions are heavier than Camoufox contexts. |
| `CAPSOLVER_API_KEY` | *(empty)* | Managed Capsolver key. |
| `CAPSOLVER_MODE` | `api` | `api` (pure HTTP) or `sdk`. |
| `CAPSOLVER_PROXY` | *(empty)* | Proxy handed to Capsolver for its own egress. |
| `TWOCAPTCHA_API_KEY` | *(empty)* | Managed 2Captcha key. |

There is **no sidecar**: `SOLVER_URL`, `SOLVER_TOKEN` and `SOLVER_REAL_PAGE`
were removed with the HTTP service. Everything is in-process now.

---

## 5. Measured reliability

| Provider | Setup | Result |
|---|---|---|
| `camoufox` (Camoufox, in-process) | production, one residential IP | **median ~37 s/solve**; 8/8 accounts on one batch |
| `cloak` (CloakBrowser, in-process) | this port | new code — no production batch behind it yet; benchmark it yourself |
| managed APIs | Capsolver / 2Captcha | only exercised when a key is present |

Notes that shaped the defaults:

- **Solve is stochastic.** The real-page token lands roughly **1 in 2–5
  attempts**; the retry envelope (`SOLVER_ATTEMPTS`) absorbs that, and the chain
  degrades to the next provider (or a paid API) when the IP is exhausted.
- **Concurrency is not free.** 4 parallel solves from one IP scored 0/4; 3
  serial solves with `SOLVER_SOLVE_DELAY=45` scored 3/3. With Camoufox,
  `SOLVER_MAX_CONCURRENT=8` on one browser gave 8/8 in 88 s, while 16 timed out
  on page navigation.
- **Datacenter proxies cannot pass Turnstile.** 100 proxies from one hosting ASN
  scored 0/2. Only residential/mobile exit IPs get a solvable challenge — and a
  VPS's own IP is a hosting ASN, which is why registration belongs on a
  residential connection and daily earning (no captcha) can run anywhere.

Benchmark a chain on your own machine and IP:

```bash
python scripts/bench_solver.py --runs 3 --delay 20
python scripts/bench_solver.py --only internal,camoufox --runs 5
```

---

## 6. Setup scripts

```bash
bash scripts/setup_camoufox.sh   # camoufox[geoip] + playwright==1.60 + browser fetch + launch check
bash scripts/setup_cloak.sh      # pip install cloakbrowser + playwright install chromium + import check
```

`setup_camoufox.sh` pins `playwright==1.60` deliberately: Camoufox's fetched
browser build and the Playwright version must match, and 1.61+ re-downloads on
every launch (and hangs). Both scripts finish by telling you to run
`python main.py solve` as a sanity check.

`setup_cloak.sh` prints the NOTICE warning again on purpose — the cloak
subpackage has no upstream license (section 1).
