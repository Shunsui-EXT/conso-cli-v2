# NOTICE — Cloak solver attribution

The code under `src/conso/solver/cloak/` is adapted from
<https://github.com/waguriagentic/captcha-solver> (directories `turnstile/`,
`cloudflare/`, `common/`, plus `turnstile/template.html`). The upstream
repository ships with **no `LICENSE` / `COPYING` / `NOTICE` file and no SPDX
header on any source file**, so no license grant is in effect; the code is
vendored in-process at the project owner's explicit request for private use
and **MUST NOT be redistributed** outside this project without first obtaining
a written license grant from the upstream author
(<https://github.com/waguriagentic>).

Transitive dependency: this port drives [CloakBrowser]
(<https://pypi.org/project/cloakbrowser/>) — a Playwright-compatible stealth
Chromium published by CloakHQ under the **MIT License** (confirmed via
`pip show cloakbrowser` and PyPI metadata). CloakBrowser is linked at runtime
only; no CloakBrowser source is vendored.

## Files adapted from waguriagentic/captcha-solver

| This project                                          | Upstream                       |
| ----------------------------------------------------- | ------------------------------ |
| `src/conso/solver/cloak/browser.py`                   | `common/browser.py`            |
| `src/conso/solver/cloak/turnstile.py` (async core)    | `turnstile/solve.py`           |
| `src/conso/solver/cloak/cloudflare.py` (async core)   | `cloudflare/solve.py`          |
| `src/conso/solver/cloak/templates/turnstile_stub.html`| `turnstile/template.html`      |

Deviations from upstream in this port:

- No FastAPI / HTTP sidecar / SSRF guard / auth / monitoring layer.
- Env-based `browser_kwargs(prefix, proxy)` replaced with explicit
  `build_browser_kwargs(headless, geoip, proxy, humanize)` arguments —
  configuration lives in the solver constructors, not module globals.
- A blocking sync wrapper (`CloakTurnstileSolver`, `CloakCloudflareSolver`)
  owns a dedicated asyncio-loop thread and a persistent browser so the
  Conso worker pool can drive solves from blocking code paths.
- Conso-specific defaults layered on top: real-page solve mode, the
  Supabase-whitelisted `redirect_uri` is appended automatically, and the
  retry/cooldown/concurrency envelope (`SOLVER_*` env knobs) matches the
  measured production configuration (see `analysis/SOLVER_RELIABILITY.md`
  and `analysis/INTERNAL_SOLVER.md` in the sibling CONSO project).

## Camoufox solver

`src/conso/solver/camoufox.py` is ported near-verbatim from the sibling
CONSO project's `src/conso/internal_solver.py` (same copyright holder as
this project — see repository `LICENSE`). Camoufox itself is BSD-3-Clause
licensed; linked at runtime only.
