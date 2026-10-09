# Architecture

`conso-cli` is a layered rebuild of the original single-package pipeline. The
goal of the split is to make the dependency direction explicit and testable:
the protocol/economy logic must be usable (and unit-testable) with no browser,
no network, and no CLI installed.

---

## 1. Layer map

```
                 app/     cli · ui · monitor · commands/
                  │       (typer commands, Rich rendering, event subscription)
                  ▼
                 ops/     pipeline · scheduler · orchestrator · earnings ·
                  │       limits · verifiers · concurrency · notify · events
                  ▼
   auth/ ── store/ ── solver/          session · storage · captcha providers
                  │
                  ▼
                 net/     transport (curl_cffi) · client (Supabase + RPCs)
                  │
                  ▼
                 core/    constants · config · economy · identity
```

| Package | Owns | Must not |
|---|---|---|
| `core/` | Constants extracted from the extension, `.env` settings, the zap/USD/quality model, identity generation | Import anything else in the tree; do network or browser I/O |
| `net/` | TLS/JA3-impersonating HTTP transport with the proxy pool; Supabase auth + PostgREST RPC client | Know about accounts, sessions, solvers, or the CLI |
| `solver/` | Turnstile providers behind one protocol, the fallback chain, the factory, warm-up | Import `net`/`ops`/`app`; import a browser at module scope |
| `store/` | Durable account store (JSON + CSV, atomic writes) and the daily ledger | Know about the network or the CLI |
| `auth/` | Session resolution: cached → refresh (+persist) → password → email OTP | Own captcha policy (the solver is passed in) |
| `ops/` | Registration, farming, missions, limits, pacing, proxies, scheduling, Telegram | Import `app/` |
| `app/` | typer CLI, Rich output, the live monitor, command wiring | Contain protocol logic that other layers need |

---

## 2. Dependency rules

1. **`core/` is dependency-light.** It imports the standard library plus
   `tiktoken` (tokenizer) and `python-dotenv` (settings). No `curl_cffi`, no
   `rich`, no browser. `python -c "import conso.core.economy"` must work on a
   bare interpreter — this is what lets the economy be tested offline and what
   the `doctor` command asserts ("core src/ browser imports: NONE").
2. **One direction only.** `core → net → {solver, store, auth} → ops → app`.
   Nothing imports "upwards". In particular **`ops/` never imports `app/`**,
   and `solver/`/`store/` never import `ops/`.
3. **`ops/` reports progress through `ops/events.py`, not through the UI.**
   Ops code calls the event publisher (`emit(name, **data)`); the subscriber
   lives in `app/monitor.py`, which renders the live panel. This is what keeps
   ops importable and runnable headless (cron, tests, scripts) — with no
   subscriber attached, emitting is a no-op. Consequence: a worker may never
   `print()` from inside ops; user-visible output is either an event or a line
   returned to the CLI layer.
4. **Browser dependencies are lazy inside solver providers.** Importing
   `conso.solver` must succeed with no `cloakbrowser` / `camoufox` / `playwright`
   installed; each provider imports its heavy dependency inside the method that
   needs it and raises a `SolverError` naming the missing extra. The same rule
   applies to `ops/`: importing `conso.ops.pipeline` must not import a browser.
5. **Credentials and env are read lazily.** `conso.core.config.Settings` is the
   only module that reads `os.environ` at import time (via `load_dotenv()`);
   the solver factory reads `CAPTCHA_PROVIDER` and friends at *call* time so
   tests can monkeypatch between calls.
6. **Errors cross layers as exceptions, not exit codes.** `ConsoAPIError` and
   `SolverError` propagate; only `app/cli.py` decides what becomes a non-zero
   exit status.

---

## 3. Data flow

### register (provisioning)

```
app/cli.register
  └─ ops.pipeline.run_registration(n, settings, solver, verifier)
       ├─ conso.solver.ensure_solver_ready()      warm the browser providers once
       └─ per account (worker pool):
            core.identity.build_identity()        email / password / consoname
            solver.pin_account_proxy(proxy)       solve from the account's exit IP
            solver.solve_turnstile()              Turnstile token (retries internally)
            net.client.sign_up_email(..., captcha_token)
            ops.verifiers.build_verifier().wait_for_otp()
            net.client.verify_otp(email, code)    -> Session
            net.client.create_consouser(user_id)
            net.client.redeem_referral_code()     BEFORE onboarding
            net.client.set_consoname(name)        onboarding — REQUIRED before a turn
            store.Store.add(AccountRecord)
```

Ordering is load-bearing: referral before onboarding, `consoname` before the
first turn. Violating either trips the backend's anti-abuse (see
[`RE_NOTES.md`](RE_NOTES.md)).

### farm (earning)

```
app/cli.farm
  └─ ops.pipeline.farm_turns_for_account(settings, record, turns, solver, store)
       ├─ auth.session.SessionManager.ensure_session(record)   cached|refreshed|password
       ├─ ops.limits.get_daily_status(client)                  headroom / banned / cap
       ├─ for each turn (bounded by the daily cap):
       │    core.economy.account_turn(...)  -> entry, base_zaps, spend_usd
       │    net.client.append_prompt(entry, base_zaps, spend_usd)
       │    credited == 0  -> STOP (the next turn would ban the account)
       │    ops.concurrency.Pacer.wait()   jittered 20-60 s between turns
       └─ store.Store.add(record)  (zap totals, refreshed tokens)
```

### daily loop

```
app/cli.loop
  └─ ops.scheduler.DailyLoop(settings, store, config).run_cycle()   # or run_forever()
       ├─ store.load_state()                "already ran today" ledger
       ├─ for each active account not done today:
       │    ops.limits / auth.session       skip: banned, full, below MIN_REMAINING_ZAPS
       │    pipeline.farm_turns_for_account same path as `farm`
       │    loop._mark_done(day, email)     resumable across restarts
       ├─ store.save_state()
       └─ ops.notify.notify("LOOP DONE", rows)   Telegram, best-effort
```

`ops.orchestrator.Orchestrator` composes the three: `register → earn → loop`
(`pipeline` command).

---

## 4. Frozen cross-module interfaces

These are the seams other layers are written against. Changing one means
changing its callers in the same commit.

### core

| Symbol | Contract |
|---|---|
| `core.config.Settings` / `Settings.from_env()` | Dataclass of every `.env` knob; `from_env()` is the only constructor the CLI uses. |
| `core.config.ProxyConfig` | `urls: list[str]`, `per_account: bool`, `enabled`. |
| `core.constants` | `DAILY_TURN_CAP`, `CREDIT_CEILING`, `DAILY_ZAP_CAP`, `TURNSTILE`-related endpoints, model/price tables. Treated as data, not policy. |
| `core.economy.account_turn(...)` | Pure function → the `(entry, base_zaps, spend_usd)` triple for one synthetic turn. |
| `core.economy.build_entry(...)`, `compute_zaps(...)`, `quality_score(...)`, `send_zaps_for_target(...)` | Pure; no I/O. |
| `core.identity.build_identity(settings, rng, index)`, `generate_password(rng, length)`, `generate_consoname(rng)` | Pure; deterministic under a seeded `random.Random`. |

### net

| Symbol | Contract |
|---|---|
| `net.transport.Transport(settings, pin_proxy=None)` | `request(method, url, *, headers, json, data, retries, timeout, no_proxy)`; `close()`; holds the proxy pool. |
| `net.transport.ProxyPool` | `acquire()`, `report(url, ok)`, `health_check(impersonate)`; `pin_proxy` on a `Transport` overrides the pool for that instance. |
| `net.client.ConsoClient(settings, transport=None)` | `.session` is set by the auth calls; every method raises `ConsoAPIError` on failure. |
| `net.client.Session` | `access_token`, `refresh_token`, `user_id`, `email`, `expires_at`, `raw`. |
| `net.client.ConsoAPIError` | `status`, `payload` — callers classify on the message text. |

Client methods relied on by scripts/ops: `sign_up_email`, `sign_in_password`,
`sign_in_otp`, `verify_otp`, `refresh`, `create_consouser`, `set_consoname`,
`append_prompt`, `redeem_referral_code`, `claim_daily_mission`,
`get_todays_mission_claims`, `get_my_leaderboard_rank`.

### solver

| Symbol | Contract |
|---|---|
| `solver.build_solver(transport=None)` | Env-driven factory. Returns one provider, a `FallbackSolver`, or a `NoopSolver`. Never raises on missing config — it skips. |
| `solver.ensure_solver_ready(logger=None)` | Best-effort warm-up; never raises; returns whether any browser provider came up. |
| `CaptchaSolver` (protocol) | `solve_turnstile(*, sitekey, page_url, timeout) -> str`. Optional extras providers may implement: `warm()`, `warm_async()`, `pin_account_proxy(proxy)`, `solve_cf_clearance(url, ...)`. |
| `solver.TURNSTILE_SITEKEY` / `TURNSTILE_PAGE_URL` / `CONSO_REDIRECT_URIS` | The Conso sitekey, the verify-human page, and the whitelist of `redirect_uri` values that make the widget render. |
| `solver.load_proxies(path)` | Reads `SOLVER_PROXY_FILE` (one URL per line, `#` comments allowed). |
| Provider names | `internal` (alias of `cloak`), `camoufox`, `capsolver`, `2captcha`, `manual`, `none`. `CAPTCHA_PROVIDER` is a comma-chain; unknown names are skipped, not fatal. |

### store

| Symbol | Contract |
|---|---|
| `store.storage.AccountRecord` | Dataclass with the persisted fields (`email`, `password`, `consoname`, `user_id`, `access_token`, `refresh_token`, `expires_at`, `refreshed_at`, `proxy`, `created_at`, `status`, `total_zaps`, `note`); `SCHEMA_FIELDS` lists them for merging/export. |
| `store.storage.Store(data_dir)` | `add(record)` is an **upsert keyed on email** (a refreshed record must not leave a stale duplicate), `all()`, `update(email, **fields)`, `save_state(dict)` / `load_state()`. |

### auth

| Symbol | Contract |
|---|---|
| `auth.session.SessionManager(settings, store, transport=None, verifier=None)` | |
| `SessionManager.ensure_session(record, solver=None, force_refresh=False) -> SessionResult` | `SessionResult.session` is live; `.source` ∈ `cached \| refreshed \| password`. Persists rotated tokens to the store before returning. |
| `auth.session.jwt_expiry(token)` | Best-effort `exp` read; `0` when unparsable. |

### ops

| Symbol | Contract |
|---|---|
| `ops.events.emit(name, **data)` | Fire-and-forget progress publication; no subscriber is required. |
| `ops.pipeline.register_account(...)` / `run_registration(...)` | Registration, concurrent. |
| `ops.pipeline.farm_turns_for_account(settings, record, turns, *, solver, store) -> (turns_done, zaps)` | Stops at the first zero-credit turn; clamps to `DAILY_TURN_CAP`. |
| `ops.scheduler.DailyLoop(settings, store, config=LoopConfig())` | `run_cycle()`, `run_forever()`, `run_account(record, force=False)`; ledger in `state.json`. |
| `ops.limits.get_daily_status(client) -> DailyStatus \| None`, `daily_headroom(status, cap)`, `classify_limit(msg)` | Daily-cap / ban / boost-factor view. |
| `ops.earnings.run_earnings(...)`, `get_account_row(client)` | Mission + code claims. |
| `ops.verifiers.build_verifier(settings)` | Returns an object with `wait_for_link(...)`; verifiers that can read raw codes additionally expose `fetch_latest_otp(address)`. |
| `ops.notify.notify(title, rows, ...)`, `render_html(...)`, `is_configured()` | Telegram summaries; never raises into a run. |
| `ops.concurrency.Pacer(min_seconds, max_seconds)`, `AdaptiveConcurrency` | Jittered pacing and adaptive worker limits. |

### app

| Symbol | Contract |
|---|---|
| `app.cli.main()` / `app.cli.app` | Entry point (also `main.py` and the `conso` console script). 16 commands. |
| `app.ui.log(message, level="")` | Human-readable output; the only place that prints. |
| `app.monitor` | Subscribes to `ops.events` and renders the live panel; degrades to plain lines when stdout is not a TTY. |
