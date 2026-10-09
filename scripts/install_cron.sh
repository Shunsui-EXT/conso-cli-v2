#!/usr/bin/env bash
# Install (or remove) the daily earn cycle as a cron job on this machine.
#
# Run it ON the machine that should do the earning (typically the VPS):
#   bash scripts/install_cron.sh            # install, daily at 07:13 UTC
#   CRON_HOUR=3 bash scripts/install_cron.sh
#   bash scripts/install_cron.sh --remove
#
# The job runs `python main.py loop --once` inside the project venv, appends to
# logs/daily.log, and is idempotent: re-running replaces the previous entry.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

CRON_HOUR="${CRON_HOUR:-7}"
# Avoid :00 — every operator schedules on the hour.
CRON_MINUTE="${CRON_MINUTE:-13}"
WORKERS="${WORKERS:-2}"
TURNS="${TURNS:-10}"
MIN_REMAINING="${MIN_REMAINING:-1.0}"
MARKER="# conso-daily"

remove() {
  if crontab -l 2>/dev/null | grep -qF "$MARKER"; then
    crontab -l 2>/dev/null | grep -vF "$MARKER" | crontab -
    echo "cron: removed"
  else
    echo "cron: nothing to remove"
  fi
  exit 0
}
[[ "${1:-}" == "--remove" ]] && remove

PYTHON="$REPO_ROOT/.venv/bin/python"
if [[ ! -x "$PYTHON" ]]; then
  echo "cron: $PYTHON not found — run scripts/setup first" >&2
  exit 1
fi

mkdir -p logs

# One line, fully quoted: the command runs from the repo root with the venv
# interpreter so imports resolve without activating anything.
CMD="cd $REPO_ROOT && $PYTHON main.py loop --once --workers $WORKERS --turns $TURNS --min-remaining $MIN_REMAINING >> $REPO_ROOT/logs/daily.log 2>&1 $MARKER"
LINE="$CRON_MINUTE $CRON_HOUR * * * $CMD"

TMP="$(mktemp)"
crontab -l 2>/dev/null | grep -vF "$MARKER" > "$TMP" || true
printf '%s\n' "$LINE" >> "$TMP"
crontab "$TMP"
rm -f "$TMP"

echo "cron: installed"
echo "cron: $LINE"
echo "cron: logs -> $REPO_ROOT/logs/daily.log"

# Show the resulting schedule.
crontab -l | grep -F "$MARKER"
