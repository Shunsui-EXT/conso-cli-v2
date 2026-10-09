# conso-cli

A reverse-engineered automation CLI for the **Conso: AI Usage Tracker** Chrome
extension (v0.1.4.0). It talks directly to the Conso backend (Supabase +
`conso.xyz`) — API-first, with no browser needed for the account or turn logic.

It can **provision accounts**, **earn zaps** (missions + synthetic AI-usage
turns), **keep sessions alive**, and **run on a schedule**, all from the CLI.
On a TTY, running it with no arguments opens a **dashboard** — a one-screen
menu over the same commands, with the store state and a recent-results line.
While any run is active it renders a single live panel (header + progress +
zaps + log tail) and prints a summary when it finishes. There is no curses
app and no keybindings: with no terminal (cron, pipes, CI) the dashboard stays
out of the way and the CLI prints plain help, so it runs fine over SSH, in
cron, or under systemd.

**Provenance.** The protocol was recovered by unpacking the published extension
(`bjibbmkefnaamkenamdppfengeepadpi`) and probing the backend it talks to; the
findings are written up in [`docs/RE_NOTES.md`](docs/RE_NOTES.md). This
repository (`conso-cli`) is a clean rebuild of the original pipeline into a
layered package, with the captcha solver ported in-process (see
[`docs/SOLVER.md`](docs/SOLVER.md)).

> **Scope / disclaimer.** This is a reverse-engineering research tool that
> automates a third-party service. It is published for research and educational
> purposes. You are responsible for how you use it and for complying with the
> target service's terms and any applicable law. See [License](#license).

---

## Table of contents

1. [Architecture](#architecture)
2. [Requirements](#requirements)
3. [Install](#install)
4. [Configure (.env)](#configure-env)
5. [Command reference](#command-reference)
6. [Quickstart](#quickstart)
7. [Dashboard](#dashboard)
8. [Limits & anti-abuse](#limits--anti-abuse)
9. [Captcha solver](#captcha-solver)
10. [Scripts](#scripts)
11. [License](#license)

---

## Architecture

```
main.py                     python main.py <cmd>  (adds src/ to sys.path)
src/conso/
  core/     constants, config, economy, identity      dependency-light, no I/O
  net/      transport (curl_cffi TLS fingerprint), client (Supabase + RPCs)
  solver/   base, chain (build_solver/ensure_solver_ready), managed, camoufox,
            cloak/ (in-process port of waguriagentic/captcha-solver)
  store/    storage — JSON+CSV account store, atomic writes, checkpoints
  auth/     session — cached / refreshed / password / OTP session resolution
  ops/      concurrency, limits, verifiers, earnings, pipeline, scheduler,
            orchestrator, notify, events
  app/      cli, ui, monitor, commands/
scripts/    setup + ops helpers (see Scripts)
docs/       ARCHITECTURE.md, SOLVER.md, RE_NOTES.md
```

Data flow, in one line each:

- **register** — `identity` mints an email/password → `solver` produces a
  Turnstile token → `net.client` signup + OTP verify → `create_consouser` →
  referral → onboarding (`consoname`) → `store`.
- **farm** — `auth.session` resolves a live session → `core.economy` sizes the
  turn → `append_prompt` credits it → the loop stops at the first zero-credit
  turn.
- **daily loop** — `ops.scheduler` walks every active account, resuming from
  `state.json`, calling the same farm path with per-account pacing.

Layer map and dependency rules: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

---

## Requirements

- **Python 3.10+**
- A **Linux/macOS** shell (Windows works via WSL; native Windows is untested).
- Internet access to the Conso backend, `temp.tf` (email), and Cloudflare.
- One captcha backend (see [Captcha solver](#captcha-solver)): a browser solver
  (extra install, ~1.5 GB for Camoufox) or a paid API key.

---

## Install

**One command sets everything up** — venv, dependencies, the solver, its
browser, `.env`, and the pre-flight:

```bash
bash scripts/setup_all.sh
```

When it finishes, the only command left is `python main.py` (or
`.venv/bin/python main.py`).

```bash
bash scripts/setup_all.sh                 # camoufox solver (recommended)
bash scripts/setup_all.sh --with-cloak    # also install the cloak provider
bash scripts/setup_all.sh --skip-browser  # deps + config only, no 1.3 GB fetch
```

It is safe to re-run: every step detects what is already done and skips it.
The browser build is **pinned** (`152.0.4-beta.28` + `playwright==1.60`)
because a version mismatch makes Camoufox re-download and hang on launch.

<details>
<summary>Manual install (what the script does, step by step)</summary>

```bash
# 1. Virtual environment
python3 -m venv .venv
source .venv/bin/activate

# 2. Runtime dependencies
pip install -r requirements.txt

# 3. Optional: install the package so `conso ...` works from anywhere
pip install -e .

# 4. A captcha backend (Camoufox must come before the playwright pin)
pip install 'camoufox[geoip]>=0.4.0' && pip install playwright==1.60
python -m camoufox set official/stable/152.0.4-beta.28
bash scripts/setup_cloak.sh        # optional: in-process cloak port

# 5. Config + pre-flight
cp .env.example .env
python main.py doctor
python main.py test
```

</details>

`main.py` inserts `src/` itself, so `python main.py ...` works from a fresh
clone with no `PYTHONPATH` and no package install. `pip install -e .` is only
needed for the `conso` console script.

---

## Configure (.env)

`cp .env.example .env` gives a working default (temp.tf email inbox + the
`internal,camoufox` captcha chain). Values you are most likely to change:

| Variable | Default | Meaning |
|---|---|---|
| `VERIFIER` | `temptf` | Email adapter for signup/OTP. `temptf` yields real gmail/outlook aliases. |
| `TEMPTF_PROVIDER` | `gmail` | `gmail` \| `outlook` \| `hotmail`. |
| `CAPTCHA_PROVIDER` | `internal,camoufox` | Comma-chain of solvers; first token wins. |
| `DEFAULT_REFERRAL_CODE` | *(empty)* | Referral applied to every new account (redeemed before onboarding). |
| `PROXY_FILE` / `PROXY_URLS` | *(empty)* | Proxy pool for account traffic. |
| `PROXY_PER_ACCOUNT` | `1` | Pin one proxy per account (spreads signup velocity). |
| `MIN_REMAINING_ZAPS` | `1.0` | Skip farming an account with less headroom (`0` disables). |
| `MIN_DELAY_SECONDS` / `MAX_DELAY_SECONDS` | `20` / `60` | Jittered pacing between turns on one account. |
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` | *(empty)* | Run summaries + `scripts/telegram_bot.py`. |

`SUPABASE_URL` / `SUPABASE_KEY` are the extension's public values and do not
need changing. The full list — including every `SOLVER_*` / `CLOAK_*` knob — is
documented inline in [`.env.example`](.env.example).

---

## Command reference

Every command is `python main.py <command>` (or `conso <command>` after
`pip install -e .`). Add `--help` to any command for its flags.

| Command | Purpose |
|---|---|
| `test` | Pre-flight: economy sanity, backend reachability, captcha posture, proxy health |
| `register N` | Provision N accounts (`--dry-run`, `--referral`, `--earn`, `--turns`; worker count via `CONCURRENCY`/`MAX_CONCURRENCY`) |
| `farm` | Missions + synthetic turns (`--turns`, `--email`, `--earn/--no-earn`, `--dry-run`) |
| `earn` | Claim missions/codes (`--list`, `--referral`, `--access`, `--missions`) |
| `status` | Daily-limit / earning state per account (`--email`) |
| `loop` | Recurring daily cycle (`--once`, `--interval-hours`, `--workers`, `--daily-cap`, `--min-remaining`) |
| `session` | Show/refresh stored sessions without re-login (`--force`) |
| `recover` | Refresh → password → email OTP recovery (`--email`) |
| `solve` | Solve one Turnstile challenge (solver sanity check) |
| `export` | Dump the account store (`--fmt csv\|json`) |
| `verify` | Verify the proxy pool is usable |
| `proxies` | Health-check a proxy list (`--file`, `--limit`) |
| `doctor` | Report the solver chain, browser readiness, and whether the core is browser-free |
| `report` | Headless status (`--fmt table\|json\|csv`, `--refresh`) |
| `pipeline` | One-shot chain: `--register N --earn --loop --only-new --workers N` |
| `notify` | Send a Telegram test message (verifies bot token + chat id) |

---

## Quickstart

**One account, fully provisioned, then earn:**

```bash
python main.py register 3 --earn
```

**Earn on existing accounts:**

```bash
python main.py farm --turns 5
```

**One daily cycle, then exit (what cron runs):**

```bash
python main.py loop --once
```

**Live status polled from the backend:**

```bash
python main.py report --refresh
```

Registration needs a residential IP; daily earning does not. A cheap split
layout is therefore: register locally, then push the store and let a server run
`loop --once` (see [`scripts/sync_accounts.sh`](scripts/sync_accounts.sh) and
[`scripts/install_cron.sh`](scripts/install_cron.sh)).

---

## Dashboard

Running the CLI with **no subcommand** on an interactive terminal opens a
one-screen dashboard over the same commands:

```bash
python main.py       # or: conso
```

It renders a bordered `CONSO MONITOR` console. Reading top to bottom:

- **header** — a status indicator (`● READY` / `● ATTENTION` when accounts are
  failed/banned / `● EMPTY` with no accounts), the current time, and session
  uptime;
- **FLEET** — total accounts, how many are ready to earn (with a meter), and a
  problems line that only appears when there are failures or bans;
- **TODAY** — the daily-loop ledger: zaps collected (with a meter against the
  practical daily budget), credited turns, the ok/capped split, and the
  per-turn average. Shows `idle` until the first run of the day;
- **HEALTH** — how many solver providers are ready (with a `●` per provider),
  the verifier, the proxy pool/pinned count, and the store path;
- **TREND 7d** — a sparkline of the last seven days' zaps, with the series
  total and best day;
- the **menu**, presented as tabs — one per category (`EARN`, `ACCOUNTS`,
  `TOOLS`, `INFO`) plus `ALL` which shows every category at once. Type `t` to
  cycle to the next tab, or `t <name>` (any case, or a unique prefix such as
  `t acc`) to jump straight to one. Within a tab, each section is a
  `SECTION ──────` heading whose rule fills the row, keys are right-aligned and
  labels share one column. Two entries per row when the terminal is wide
  enough, one per row below that. **Numbers always work on every tab** — tabs
  change what you see, never what a key does;
- **LOG** — the last few actions of this session with timestamps;
- a footer hint (`number = run, t = tab, S = settings, Q = quit`).

The panel re-renders after every action, so counts and the log stay current.
It is built from Rich tables, so columns line up, dividers match the terminal
width exactly, and long values fold instead of being cut off with an ellipsis.

| Key | Action | Asks for |
|---|---|---|
| `1` | Daily run (`loop --once`) | force, workers, turns, min-remaining |
| `2` | Farm turns | turns, email, dry-run, missions |
| `3` | Claim missions | email, referral, missions, access |
| `4` | Earn on ONE account | account (number or email), referral |
| `5` | Accounts status | email |
| `6` | Sessions (refresh) | email, force |
| `7` | Recover sessions | email |
| `8` | Register new accounts | count, referral, earn now, turns, dry-run |
| `9` | Doctor (readiness) | — |
| `10` | Test (pre-flight) | platform, model |
| `11` | Solve captcha | sitekey, url |
| `12` | Proxies | list file, limit |
| `13` | Export store | fmt (csv\|json) |
| `14` | Report | fmt (table\|json\|csv), refresh |
| `15` | Verify proxies | — |
| `16` | Notify test | — |
| `17` | Pipeline (register -> earn -> loop) | count, earn, loop, turns, workers, interval, cycles, referral, daily-cap, only-new, solver-concurrent |
| `S` | Summary / settings | — |
| `Q` | Quit | — |

Notes:

- Every prompt shows its default in brackets; pressing Enter accepts it.
  `farm` and `earn` default to **all accounts**, and `loop --once` defaults
  `min-remaining` to the `.env` value (`MIN_REMAINING_ZAPS`).
- Long or destructive actions (daily run, farm, register, recover, earn,
  pipeline) print a confirmation naming exactly what will run; Enter or `y`
  proceeds.
- The dashboard orchestrates the *same* command callables as
  `python main.py <cmd>` — identical pacing, daily caps and store writes.
- Errors (`ConsoAPIError`, a command exiting non-zero, any unexpected
  exception) are printed and the menu comes back; Ctrl+C inside an action
  returns to the menu, Ctrl+C at the menu exits cleanly.
- With **no TTY** (cron, pipes, CI) nothing changes: `python main.py` prints
  the banner and plain `--help` and exits 0, so scripts never hang on a menu.

---

## Limits & anti-abuse

Measured against the live backend; the pipeline enforces all of these.

| Limit | Value | Consequence of ignoring it |
|---|---|---|
| Credited turns | **~10 per account per day** | Turn 11 returns `200 0` (soft flag); turn 12 → `account_banned` |
| Credit ceiling | **3.2** per turn (× `boost_factor`) | The server credits 0 above the ceiling |
| Practical daily zaps | **~21** (hard limit ~30) | The tail of the budget yields ~0.02 zaps/turn for a dozen requests |
| Zero-credit turn | stop immediately | The next turn bans the account |
| Referral | redeem **before** onboarding | A referral after onboarding is rejected/ignored |
| `consoname` | set **before** any turn | A turn without onboarding bans the account |

The pipeline stops at the first zero-credit turn, clamps `--turns` to the daily
cap, and sizes each turn so `base_zaps × boost_factor` lands just under the
ceiling (≈2.5–3 zaps/turn). `MIN_REMAINING_ZAPS` skips accounts whose remaining
budget is not worth the requests.

`signup_velocity_exceeded` on rapid signups is spread by pinning a different
proxy per account (`PROXY_PER_ACCOUNT=1`). Turnstile rate-flags an IP that
solves too fast — keep `SOLVER_SOLVE_DELAY` and lower `SOLVER_MAX_CONCURRENT`
if solves start timing out.

---

## Captcha solver

Conso enforces Cloudflare Turnstile on **every** Supabase auth endpoint
(signup, password login, OTP). A token is therefore required to register or to
re-authenticate. `CAPTCHA_PROVIDER` is a comma-chain — the first provider that
returns a token wins, and providers whose dependencies/keys are missing are
skipped silently:

| Provider | Kind | Requires | Notes |
|---|---|---|---|
| `internal` | in-process browser | `pip install cloakbrowser` (`scripts/setup_cloak.sh`) | The port of waguriagentic/captcha-solver; no sidecar. See [docs/SOLVER.md](docs/SOLVER.md). |
| `camoufox` | in-process browser | `scripts/setup_camoufox.sh` | Stealth Firefox. The provider with production evidence. |
| `capsolver` | managed API | `CAPSOLVER_API_KEY` | Browser-free, paid. |
| `2captcha` | managed API | `TWOCAPTCHA_API_KEY` | Browser-free, paid. |
| `manual` | operator | `CAPTCHA_TOKEN` | You paste a token per solve. |
| `none` | — | `CAPTCHA_TOKEN` | No solving at all. |

Default when `CAPTCHA_PROVIDER` is unset: `cloak,camoufox` (`internal` is the
alias for `cloak`). A resilient chain that degrades to a paid API only when the
local IP is flagged:

```bash
CAPTCHA_PROVIDER=internal,camoufox,capsolver,2captcha
```

Practical notes:

- **Solving is stochastic.** The accepted token must come from the real
  `verify-human` page rendered with a whitelisted `redirect_uri`; a stub-page
  token is rejected by Supabase (`invalid-input-response`). Expect roughly **1
  in 2–5 attempts** to land, which is why the solver retries internally
  (`SOLVER_ATTEMPTS`) before the chain moves on.
- **A residential IP is required for the browser providers.** Cloudflare does
  not issue a solvable challenge to hosting ASNs: measured, a residential ASN
  solved while an AWS ASN timed out even though `conso.xyz` returned 200.
  Datacenter proxies score 0/2; only residential/mobile exit IPs work.
- **Solves are serialised process-wide** (`SOLVER_SERIAL=1`) because Cloudflare
  hands out one usable challenge flow per source IP — 4 parallel solves on one
  IP scored 0/4, while 3 serial solves at `SOLVER_SOLVE_DELAY=45` scored 3/3.
- **Daily earning never touches the captcha**: `loop` reuses and refreshes
  stored sessions, so a VPS can run the daily cycle with no solver at all.

Full detail, including the measured Camoufox median (~37 s/solve) and the
license warning for the cloak port: [`docs/SOLVER.md`](docs/SOLVER.md).

---

## Scripts

| Script | Purpose |
|---|---|
| `scripts/setup_all.sh` | **One-shot setup** — venv, deps, solver, pinned browser, `.env`, pre-flight. Start here. |
| `scripts/setup_camoufox.sh` | Install + fetch the Camoufox stealth browser, verify it launches |
| `scripts/setup_cloak.sh` | Install `cloakbrowser` + Chromium, verify the import, print the NOTICE warning |
| `scripts/install_cron.sh` | Install/remove the daily `loop --once` cron job (idempotent, marker comment) |
| `scripts/sync_accounts.sh` | Push `data/accounts.json` to the remote host that runs the daily cycle |
| `scripts/relogin_all.py` | Re-establish every account's session (free refresh first, then password grant) |
| `scripts/fix_account.py` | Recover one account via email OTP when the refresh token went stale |
| `scripts/bench_solver.py` | Benchmark providers head-to-head (`--only internal,camoufox`) |
| `scripts/telegram_bot.py` | Telegram command bot: `/status`, `/report`, `/solver`, `/cron` |
| `scripts/parallel_register.sh` | Run N register processes with separate stores, then merge them |
| `scripts/fetch_extension.sh` | Download + unpack the extension CRX for analysis |

---

## License

MIT — see [`LICENSE`](LICENSE). The reverse-engineered constants and protocol
details are provided for interoperability research.

**Exception:** `src/conso/solver/cloak/` is adapted from
[waguriagentic/captcha-solver](https://github.com/waguriagentic/captcha-solver),
which ships **no license of any kind**. No grant is in effect, so that
subpackage is **private use only — do not redistribute this project** without
first obtaining a written license grant from the upstream author. The full
attribution table is in
[`src/conso/solver/cloak/NOTICE.md`](src/conso/solver/cloak/NOTICE.md).
