#!/usr/bin/env bash
# Set up the cloak (internal) Turnstile solver: cloakbrowser + its Chromium.
#
# `internal` in CAPTCHA_PROVIDER is the in-process port of
# waguriagentic/captcha-solver that lives in src/conso/solver/cloak/.
# It drives CloakBrowser (a Playwright-compatible stealth Chromium), so both
# the Python package and its browser build must be present.
#
#   bash scripts/setup_cloak.sh
#
# Then: CAPTCHA_PROVIDER=internal   (or keep the default `cloak,camoufox` chain)
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PY="${PYTHON:-python3}"
if [ -d "$ROOT/.venv" ]; then
    # shellcheck disable=SC1091
    source "$ROOT/.venv/bin/activate"
    PY="python"
fi

echo "[cloak] installing cloakbrowser ..."
"$PY" -m pip install -q cloakbrowser

echo "[cloak] installing the Chromium build cloakbrowser drives ..."
"$PY" -m playwright install chromium

echo "[cloak] verifying import + browser availability ..."
"$PY" - <<'PY'
try:
    import cloakbrowser
except ImportError as exc:
    raise SystemExit(
        f"cloakbrowser import failed: {exc}\n"
        "install it with: pip install cloakbrowser"
    )

print("[cloak] cloakbrowser import OK:", getattr(cloakbrowser, "__version__", "?"))
try:
    from conso.solver.cloak.turnstile import CloakTurnstileSolver
except Exception as exc:  # noqa: BLE001 - report, don't traceback
    raise SystemExit(f"[cloak] solver import failed: {type(exc).__name__}: {exc}")
print("[cloak] CloakTurnstileSolver import OK (is_available=%s)"
      % CloakTurnstileSolver.is_available())
PY

cat <<'WARN'
[cloak] ---------------------------------------------------------------
[cloak] NOTICE: the ported solver code has NO upstream license.
[cloak] src/conso/solver/cloak/ is adapted from
[cloak]   https://github.com/waguriagentic/captcha-solver
[cloak] which ships no LICENSE/COPYING/NOTICE file and no SPDX headers,
[cloak] so no license grant is in effect. It is vendored for PRIVATE USE
[cloak] ONLY — do NOT redistribute this project without first obtaining a
[cloak] written grant from the upstream author.
[cloak] See src/conso/solver/cloak/NOTICE.md for the full attribution.
[cloak] ---------------------------------------------------------------
WARN

echo "[cloak] done. Sanity check: python main.py solve"
