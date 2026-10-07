"""Pick the auto-approval threshold T_auto from decision rows.

Input JSON: a list of {"payable": number, "gates_pass": bool, "correct": bool}, where `correct` says the automatic
decision would have matched the human/reference outcome. A claim is auto-approved at threshold T when gates pass and
payable <= T. False-approve rate = auto-approved claims that were not correct / auto-approved claims. The recommendation
is the largest candidate threshold whose rate is below the target (default 1%). Results from the reference calculator
are provisional: only the insurer's real engine and real outcomes can set the production value."""

from __future__ import annotations

import argparse
import json
from typing import Any

CANDIDATES = [10_000, 20_000, 30_000, 50_000, 75_000, 100_000, 150_000, 200_000, 300_000, 500_000]


def sweep(rows: list[dict[str, Any]], candidates: list[int] = CANDIDATES) -> list[dict[str, Any]]:
    out = []
    for t in candidates:
        auto = [r for r in rows if r["gates_pass"] and r["payable"] <= t]
        bad = sum(1 for r in auto if not r["correct"])
        out.append(
            {
                "t_auto": t,
                "auto_share": len(auto) / len(rows) if rows else 0.0,
                "false_approve_rate": bad / len(auto) if auto else 0.0,
                "auto_count": len(auto),
            }
        )
    return out


def recommend(table: list[dict[str, Any]], target: float = 0.01) -> int | None:
    ok = [r["t_auto"] for r in table if r["false_approve_rate"] < target]
    return max(ok) if ok else None


def simulate(
    n: int, seed: int = 7, tamper_rate: float = 0.10, miss_rate: float = 0.05
) -> list[dict[str, Any]]:
    """Stand-in for the insurer's engine: claim amounts are log-uniform; a share carry tampering that the checks
    miss with probability `miss_rate` (a missed one is auto-approvable and wrong). Provisional placeholder only."""
    import math
    import random

    rng = random.Random(seed)
    rows = []
    for _ in range(n):
        payable = round(math.exp(rng.uniform(math.log(2_000), math.log(800_000))), 2)
        tampered = rng.random() < tamper_rate
        caught = tampered and rng.random() >= miss_rate
        rows.append({"payable": payable, "gates_pass": not caught, "correct": not tampered})
    return rows


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("decisions", help="decisions JSON, or 'simulate'")
    ap.add_argument("--target", type=float, default=0.01)
    a = ap.parse_args()
    if a.decisions == "simulate":
        rows = simulate(20_000)
    else:
        with open(a.decisions) as f:
            rows = json.load(f)
    table = sweep(rows)
    for r in table:
        print(
            f"T={r['t_auto']:>8}  auto={r['auto_share']:.1%}  false-approve={r['false_approve_rate']:.2%}"
        )
    print(
        "recommended:",
        recommend(table, a.target),
        "(provisional unless rows come from the real engine)",
    )


if __name__ == "__main__":
    main()
