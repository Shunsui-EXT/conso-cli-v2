#!/usr/bin/env bash
# Push the local account store to a remote host so it can run the daily earn
# cycle there.
#
# The store holds rotated refresh tokens: whoever runs last owns them. Syncing
# copies the local state over the remote one, so run this BEFORE letting the
# remote run, and never let both sides farm the same accounts at once.
#
# Usage:
#   scripts/sync_accounts.sh                 # uses SSH_HOST / SSH_KEY from env or .env
#   SSH_HOST=ubuntu@1.2.3.4 scripts/sync_accounts.sh
#   SSH_KEY=~/.ssh/id_ed25519 scripts/sync_accounts.sh
#
# Optional:
#   REMOTE_DIR   remote project directory   (default: ~/conso)
#   SSH_PORT     ssh port                   (default: 22)

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

# Read SSH_HOST / SSH_KEY from .env when they are not already exported.
if [[ -z "${SSH_HOST:-}" && -f .env ]]; then
  SSH_HOST="$(grep -E '^SSH_HOST=' .env | tail -1 | cut -d= -f2- || true)"
fi
if [[ -z "${SSH_KEY:-}" && -f .env ]]; then
  SSH_KEY="$(grep -E '^SSH_KEY=' .env | tail -1 | cut -d= -f2- || true)"
fi

SSH_HOST="${SSH_HOST:-}"
SSH_KEY="${SSH_KEY:-}"
REMOTE_DIR="${REMOTE_DIR:-~/conso}"
SSH_PORT="${SSH_PORT:-22}"

if [[ -z "$SSH_HOST" ]]; then
  echo "sync: SSH_HOST is not set (export it or add SSH_HOST= to .env)" >&2
  exit 1
fi

# ssh uses -p for the port, scp uses -P. Mixing them up silently turns the
# port into scp's "preserve times" flag and the upload fails.
SSH_OPTS=(-p "$SSH_PORT" -o StrictHostKeyChecking=accept-new -o BatchMode=yes)
SCP_OPTS=(-P "$SSH_PORT" -o StrictHostKeyChecking=accept-new -o BatchMode=yes)
[[ -n "$SSH_KEY" ]] && SSH_OPTS+=(-i "${SSH_KEY/#\~/$HOME}") && SCP_OPTS+=(-i "${SSH_KEY/#\~/$HOME}")

SRC="data/accounts.json"
if [[ ! -f "$SRC" ]]; then
  echo "sync: $SRC not found — run from the project root" >&2
  exit 1
fi

COUNT="$(python3 -c "import json;print(len(json.load(open('$SRC'))))" 2>/dev/null || echo '?')"
echo "sync: local accounts=$COUNT -> $SSH_HOST:$REMOTE_DIR"

# Back up the remote store first so a bad push is recoverable.
ssh "${SSH_OPTS[@]}" "$SSH_HOST" \
  "mkdir -p $REMOTE_DIR/data && \
   if [ -f $REMOTE_DIR/data/accounts.json ]; then \
     cp $REMOTE_DIR/data/accounts.json $REMOTE_DIR/data/accounts.json.bak; \
     echo 'sync: remote backed up'; \
   fi"

scp "${SCP_OPTS[@]}" -q "$SRC" "$SSH_HOST:$REMOTE_DIR/data/accounts.json"
echo "sync: pushed"

# state.json is the "already ran today" ledger. It is NOT synced by default:
# pushing the local ledger would mark every account done on the remote and the
# remote cycle would skip everything. Each side keeps its own ledger, so only
# ever let one side farm. Set SYNC_STATE=1 to push it deliberately (e.g. when
# migrating ownership to the remote and wanting today's run skipped).
if [[ "${SYNC_STATE:-0}" == "1" && -f data/state.json ]]; then
  scp "${SCP_OPTS[@]}" -q data/state.json "$SSH_HOST:$REMOTE_DIR/data/state.json" || true
  echo "sync: state.json pushed"
fi

ssh "${SSH_OPTS[@]}" "$SSH_HOST" \
  "cd $REMOTE_DIR && \
   python3 -c \"import json;print('sync: remote accounts=',len(json.load(open('data/accounts.json'))))\""
echo "sync: done"
