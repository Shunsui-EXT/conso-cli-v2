#!/usr/bin/env bash
# Set up the Camoufox Turnstile solver (the production-proven browser provider).
#
# Installs camoufox + a matching playwright, fetches the stealth browser, and
# verifies it can launch. Run once after `pip install -r requirements.txt`.
#
#   bash scripts/setup_camoufox.sh
#
# Then make sure CAPTCHA_PROVIDER can reach it: either add `camoufox` to the
# chain (e.g. CAPTCHA_PROVIDER=internal,camoufox) or keep the default chain
# (`cloak,camoufox`), which already tries Camoufox after the cloak provider.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PY="${PYTHON:-python3}"
if [ -d "$ROOT/.venv" ]; then
    # shellcheck disable=SC1091
    source "$ROOT/.venv/bin/activate"
    PY="python"
fi

echo "[camoufox] installing camoufox + playwright==1.60 ..."
"$PY" -m pip install -q 'camoufox[geoip]>=0.4.0'
# Playwright must match the Camoufox browser build; 1.61+ re-downloads and hangs.
"$PY" -m pip install -q 'playwright==1.60'

echo "[camoufox] fetching the stealth browser (~1.5 GB, one time) ..."
"$PY" -m camoufox fetch

echo "[camoufox] verifying launch ..."
"$PY" - <<'PY'
import asyncio
try:
    from camoufox.async_api import AsyncCamoufox
except ImportError as exc:
    raise SystemExit(f"camoufox import failed: {exc}")

async def main():
    async with AsyncCamoufox(headless=True, args=["--no-sandbox"]) as b:
        p = await b.new_page()
        await p.goto("https://example.com", wait_until="domcontentloaded", timeout=30000)
        print("[camoufox] launch OK:", await p.title())

asyncio.run(main())
PY

echo "[camoufox] done. Set CAPTCHA_PROVIDER=camoufox (or keep the default"
echo "[camoufox] chain `internal,camoufox` so Camoufox is tried as a fallback)."
echo "[camoufox] sanity check: python main.py solve"
