#!/usr/bin/env python3
"""Benchmark the Turnstile solvers against each other.

Runs N serial solves per provider with the same pacing, so the only variable is
the solver itself. Prints per-solve latency plus a success rate, then a summary
table.

Usage:
    python scripts/bench_solver.py                 # 3 solves per provider
    python scripts/bench_solver.py --runs 5        # more samples
    python scripts/bench_solver.py --only internal,camoufox

`internal` is the in-process port of waguriagentic/captcha-solver (the cloak
provider); `camoufox` is the stealth-Firefox solver. Both need their extra
installed — see scripts/setup_cloak.sh and scripts/setup_camoufox.sh.
"""

from __future__ import annotations

import argparse
import os
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from conso.core.config import Settings  # noqa: E402
from conso.net.transport import Transport  # noqa: E402
from conso.solver import build_solver  # noqa: E402

DEFAULT_PROVIDERS = ["internal", "camoufox"]


def bench(provider: str, runs: int, timeout: float, delay: float) -> dict:
    """Run `runs` solves through one provider; return timings and failures."""
    os.environ["CAPTCHA_PROVIDER"] = provider
    # Equal pacing for every provider so only the solver differs.
    os.environ["SOLVER_SOLVE_DELAY"] = str(delay)
    settings = Settings.from_env()
    solver = build_solver(Transport(settings))

    name = type(solver).__name__
    timings: list[float] = []
    failures: list[str] = []
    lengths: list[int] = []

    for i in range(1, runs + 1):
        t0 = time.monotonic()
        try:
            token = solver.solve_turnstile(timeout=timeout)
        except Exception as exc:  # noqa: BLE001 - benchmark records failures
            timings.append(time.monotonic() - t0)
            failures.append(f"{type(exc).__name__}: {str(exc)[:80]}")
            print(f"  {provider} #{i}: FAIL  {time.monotonic() - t0:6.1f}s")
            continue
        elapsed = time.monotonic() - t0
        timings.append(elapsed)
        lengths.append(len(token))
        print(f"  {provider} #{i}: OK    {elapsed:6.1f}s  token_len={len(token)}")

    ok = runs - len(failures)
    return {
        "provider": provider,
        "solver": name,
        "runs": runs,
        "ok": ok,
        "failed": len(failures),
        "success_rate": (ok / runs) if runs else 0.0,
        "avg": statistics.mean(timings) if timings else 0.0,
        "median": statistics.median(timings) if timings else 0.0,
        "min": min(timings) if timings else 0.0,
        "max": max(timings) if timings else 0.0,
        "token_len": statistics.mean(lengths) if lengths else 0.0,
        "errors": failures,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--runs", type=int, default=3, help="Solves per provider.")
    ap.add_argument("--timeout", type=float, default=120.0, help="Per-solve timeout.")
    ap.add_argument("--delay", type=float, default=20.0,
                    help="SOLVER_SOLVE_DELAY applied to every provider (fair pacing).")
    ap.add_argument("--only", default="", help="Comma-separated providers to test.")
    args = ap.parse_args()

    providers = [p.strip() for p in args.only.split(",") if p.strip()] or DEFAULT_PROVIDERS
    print(f"benchmark: {args.runs} solve(s)/provider, timeout={args.timeout}s, "
          f"delay={args.delay}s\n")

    results = []
    for provider in providers:
        print(f"--- {provider} ---")
        try:
            results.append(bench(provider, args.runs, args.timeout, args.delay))
        except Exception as exc:  # noqa: BLE001
            print(f"  {provider}: SETUP ERROR {type(exc).__name__}: {str(exc)[:100]}")
            results.append({
                "provider": provider, "solver": "-", "runs": args.runs, "ok": 0,
                "failed": args.runs, "success_rate": 0.0, "avg": 0.0, "median": 0.0,
                "min": 0.0, "max": 0.0, "token_len": 0.0,
                "errors": [f"setup: {type(exc).__name__}: {exc}"],
            })
        print()

    print("=" * 78)
    header = f"{'provider':10} {'solver':28} {'ok':>5} {'rate':>7} {'avg':>8} {'med':>8} {'min':>8} {'max':>8}"
    print(header)
    print("-" * 78)
    for r in results:
        print(f"{r['provider']:10} {r['solver'][:28]:28} "
              f"{r['ok']:>2}/{r['runs']:<2} {r['success_rate'] * 100:6.0f}% "
              f"{r['avg']:7.1f}s {r['median']:7.1f}s {r['min']:7.1f}s {r['max']:7.1f}s")
    print("=" * 78)

    usable = [r for r in results if r["ok"] > 0]
    if usable:
        fastest = min(usable, key=lambda r: r["median"])
        most_reliable = max(usable, key=lambda r: (r["success_rate"], -r["median"]))
        print(f"fastest (median)   : {fastest['provider']} "
              f"({fastest['median']:.1f}s)")
        print(f"most reliable      : {most_reliable['provider']} "
              f"({most_reliable['success_rate'] * 100:.0f}% ok)")
    else:
        print("no provider produced a token - see errors above")
        for r in results:
            for err in r["errors"]:
                print(f"  {r['provider']}: {err}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
