#!/usr/bin/env bash
# Run N parallel register processes, each with its own DATA_DIR + solver browser,
# then merge their account stores into data/.
#
# Why separate processes: the Store is thread-safe but not multi-process safe
# (accounts.json would race), and one Camoufox browser saturates around
# SOLVER_MAX_CONCURRENT=8. Separate processes = separate browsers + separate
# stores, merged at the end.
#
# Usage: scripts/parallel_register.sh <processes> <per_process> [turns] [mc]
#
# Set MC (solver concurrency per process) so that PROCS * MC ~= CPU cores.
# One Camoufox browser saturates around mc=8 alone; running several processes
# at mc=8 each overloads the CPU (measured: 2x8 -> load 36 on 8 cores).
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PROCS="${1:-2}"
PER="${2:-8}"
TURNS="${3:-10}"
MC="${4:-4}"
RUN_DIR="$ROOT/data/runs/$(date +%s)"

mkdir -p "$RUN_DIR"
echo "[parallel] $PROCS processes x $PER accounts (turns=$TURNS, mc=$MC) -> $RUN_DIR"

# shellcheck disable=SC1091
source "$ROOT/.venv/bin/activate"

pids=()
for i in $(seq 1 "$PROCS"); do
    d="$RUN_DIR/p$i"
    mkdir -p "$d"
    DATA_DIR="$d" SOLVER_MAX_CONCURRENT="$MC" PYTHONPATH="$ROOT/src" \
        python main.py register "$PER" --earn --turns "$TURNS" \
        > "$d/run.log" 2>&1 &
    pids+=($!)
    echo "[parallel] started p$i (pid ${pids[-1]})"
done

fail=0
for i in "${!pids[@]}"; do
    if ! wait "${pids[$i]}"; then
        echo "[parallel] p$((i + 1)) exited non-zero"
        fail=1
    fi
done

# Merge all per-process accounts.json into the main store.
PYTHONPATH="$ROOT/src" python - "$RUN_DIR" <<'PY'
import json, sys, os
from conso.store.storage import Store, AccountRecord, SCHEMA_FIELDS
run_dir = sys.argv[1]
main = Store("data")
existing = {a.email for a in main.all()}
added = 0
for entry in sorted(os.listdir(run_dir)):
    p = os.path.join(run_dir, entry, "accounts.json")
    if not os.path.exists(p):
        continue
    for row in json.load(open(p)):
        rec = AccountRecord(**{k: v for k, v in row.items() if k in SCHEMA_FIELDS})
        if rec.email and rec.email not in existing:
            main.add(rec)
            existing.add(rec.email)
            added += 1
print(f"[parallel] merged {added} new accounts into data/accounts.json")
PY

echo "[parallel] done (fail=$fail). Per-process logs in $RUN_DIR"
exit "$fail"
