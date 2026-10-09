#!/usr/bin/env bash
# One-shot setup: after this finishes, the only command you need is
# `python main.py`.
#
#   bash scripts/setup_all.sh                 # camoufox solver (recommended)
#   bash scripts/setup_all.sh --with-cloak    # also install the cloak provider
#   bash scripts/setup_all.sh --skip-browser  # deps + config only, no 1.3 GB fetch
#
# What it does, in order:
#   1. checks the Python version
#   2. creates .venv and installs requirements.txt + the package itself
#   3. installs the solver Python packages
#   4. fetches the stealth browser, PINNED to the build that works
#   5. creates .env from .env.example when missing
#   6. runs the pre-flight (`doctor`, then `test`)
#
# Safe to re-run: every step detects what is already done and skips it, so a
# second run is fast and changes nothing.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

# --- The browser build that is known to work -------------------------------
# playwright==1.60 only drives Camoufox's 152.0.4-beta.28 build; newer builds
# (beta.30+) need a newer playwright, and a mismatch makes every launch hang
# or re-download. Pin both ends: the package version below and the build here.
CAMOUFOX_BUILD="official/stable/152.0.4-beta.28"
CAMOUFOX_BUILD_ID="152.0.4-beta.28"
PLAYWRIGHT_PIN="playwright==1.60"

WITH_CLOAK=0
SKIP_BROWSER=0
for arg in "$@"; do
    case "$arg" in
        --with-cloak)   WITH_CLOAK=1 ;;
        --skip-browser) SKIP_BROWSER=1 ;;
        -h|--help)
            sed -n '2,18p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
            exit 0 ;;
        *)
            echo "unknown option: $arg (try --help)" >&2
            exit 2 ;;
    esac
done

step() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
note() { printf '    %s\n' "$*"; }
warn() { printf '\033[33m    %s\033[0m\n' "$*"; }

# --- 1. Python version ------------------------------------------------------
step "Checking Python"
PY="${PYTHON:-python3}"
command -v "$PY" >/dev/null 2>&1 || {
    echo "python3 not found; install Python 3.10+ first" >&2
    exit 1
}
"$PY" - <<'PY'
import sys
if sys.version_info < (3, 10):
    raise SystemExit(f"Python 3.10+ required, found {sys.version.split()[0]}")
print(f"    python {sys.version.split()[0]} OK")
PY

# --- 2. Virtualenv + runtime dependencies -----------------------------------
step "Preparing the virtual environment"
if [ -d "$ROOT/.venv" ]; then
    note ".venv already exists"
else
    "$PY" -m venv "$ROOT/.venv"
    note "created .venv"
fi
# shellcheck disable=SC1091
source "$ROOT/.venv/bin/activate"
PYBIN="$ROOT/.venv/bin/python"

note "installing requirements.txt ..."
"$PYBIN" -m pip install -q --upgrade pip
"$PYBIN" -m pip install -q -r requirements.txt
note "runtime dependencies OK"

# `conso` console script; main.py works without this, so failure is not fatal.
"$PYBIN" -m pip install -q -e . 2>/dev/null || warn "pip install -e . failed (main.py still works)"

# --- 3. Solver packages -----------------------------------------------------
if [ "$SKIP_BROWSER" -eq 0 ]; then
    step "Installing the solver (camoufox)"
    "$PYBIN" -m pip install -q 'camoufox[geoip]>=0.4.0'
    # Must be installed AFTER camoufox: camoufox's own dependency range would
    # otherwise pull a newer playwright that cannot drive the pinned build.
    "$PYBIN" -m pip install -q "$PLAYWRIGHT_PIN"
    note "camoufox + $PLAYWRIGHT_PIN OK"

    if [ "$WITH_CLOAK" -eq 1 ]; then
        note "installing the cloak provider (cloakbrowser) ..."
        "$PYBIN" -m pip install -q cloakbrowser
        "$PYBIN" -m playwright install chromium
        note "cloakbrowser + chromium OK"
    fi
else
    step "Skipping the solver (--skip-browser)"
    warn "only the browser-free providers will be usable until you re-run without it"
fi

# --- 4. Browser build (pinned) ----------------------------------------------
if [ "$SKIP_BROWSER" -eq 0 ]; then
    step "Fetching the Camoufox browser build"
    BROWSER_DIR="$("$PYBIN" -m camoufox path 2>/dev/null || true)"
    if [ -n "$BROWSER_DIR" ] && \
       ls "$BROWSER_DIR/browsers/official" 2>/dev/null | grep -qF "$CAMOUFOX_BUILD_ID"; then
        note "build $CAMOUFOX_BUILD_ID already present"
        if ! "$PYBIN" -m camoufox active 2>/dev/null | grep -qF "$CAMOUFOX_BUILD_ID"; then
            "$PYBIN" -m camoufox set "$CAMOUFOX_BUILD"
        fi
    else
        note "downloading $CAMOUFOX_BUILD (~1.3 GB, one time) ..."
        "$PYBIN" -m camoufox set "$CAMOUFOX_BUILD"
    fi
    note "active build: $("$PYBIN" -m camoufox active 2>/dev/null | head -1)"
fi

# --- 5. Configuration -------------------------------------------------------
step "Preparing the configuration"
if [ -f .env ]; then
    note ".env already exists (left untouched)"
else
    cp .env.example .env
    note "created .env from .env.example"
    warn "review .env before registering accounts:"
    warn "  EMAIL_DOMAIN / VERIFIER  -> the mailbox that receives the OTP"
    warn "  DEFAULT_REFERRAL_CODE    -> optional referral applied at signup"
fi

# --- 6. Pre-flight ----------------------------------------------------------
step "Pre-flight (doctor)"
"$PYBIN" main.py doctor || warn "doctor reported a problem; see the lines above"

step "Pre-flight (test)"
if ! "$PYBIN" main.py test; then
    warn "test failed — usually the backend is unreachable or the captcha"
    warn "provider is not ready; check the output above and .env"
fi

# --- Done -------------------------------------------------------------------
step "Ready"
cat <<EOF
    Start the CLI with:

        cd $ROOT
        .venv/bin/python main.py

    That opens the dashboard (the tabbed menu). From there:
      1  daily run (loop --once)     earn on every account once
      9  doctor (readiness)          re-check the environment
     17  pipeline (register -> earn -> loop)

    Non-interactive equivalents:
        .venv/bin/python main.py loop --once     # one daily cycle
        .venv/bin/python main.py register 1 --earn
EOF
