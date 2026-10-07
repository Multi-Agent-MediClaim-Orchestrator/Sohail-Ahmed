"""Tokens and latency per claim from Langfuse generations (04-04 task 10).

    LANGFUSE_HOST=http://localhost:3200 LANGFUSE_PUBLIC_KEY=... LANGFUSE_SECRET_KEY=... python cost_by_claim.py [--csv out.csv]

``aggregate`` is pure so it can be unit-tested with canned generations.
"""

from __future__ import annotations

import argparse
import csv
import os
import statistics
import sys
from collections import defaultdict
from typing import Any

import httpx


def pct(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    return s[min(len(s) - 1, int(round(p * (len(s) - 1))))]


def aggregate(generations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Group by (session_id, agent): calls, tokens, p50/p95 latency (seconds), fallback count."""
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for g in generations:
        groups[(g.get("sessionId") or g.get("session_id") or "-", g.get("name") or "-")].append(g)
    rows = []
    for (session, agent), items in sorted(groups.items()):
        lat = [float(i.get("latency", 0) or 0) for i in items]
        usage = [i.get("usage") or {} for i in items]
        rows.append({
            "claim_ref": session, "agent": agent, "calls": len(items),
            "tokens": sum(int(u.get("totalTokens") or u.get("total") or 0) for u in usage),
            "p50_s": round(statistics.median(lat), 3) if lat else 0.0, "p95_s": round(pct(lat, 0.95), 3),
            "fallbacks": sum(1 for i in items if any(str(t).startswith("fallback:") for t in (i.get("tags") or []))),
        })
    return rows


def to_markdown(rows: list[dict[str, Any]]) -> str:
    cols = ["claim_ref", "agent", "calls", "tokens", "p50_s", "p95_s", "fallbacks"]
    out = ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    out += ["| " + " | ".join(str(r[c]) for c in cols) + " |" for r in rows]
    return "\n".join(out)


def fetch(host: str, pk: str, sk: str, limit: int = 100) -> list[dict[str, Any]]:
    out, page = [], 1
    with httpx.Client(base_url=host, auth=(pk, sk), timeout=30) as c:
        while True:
            r = c.get("/api/public/observations", params={"type": "GENERATION", "limit": limit, "page": page})
            r.raise_for_status()
            data = r.json().get("data", [])
            out += data
            if len(data) < limit:
                return out
            page += 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv")
    a = ap.parse_args()
    host, pk, sk = os.environ.get("LANGFUSE_HOST"), os.environ.get("LANGFUSE_PUBLIC_KEY"), os.environ.get("LANGFUSE_SECRET_KEY")
    if not (host and pk and sk):
        print("set LANGFUSE_HOST, LANGFUSE_PUBLIC_KEY, LANGFUSE_SECRET_KEY", file=sys.stderr)
        return 2
    rows = aggregate(fetch(host, pk, sk))
    print(to_markdown(rows))
    if a.csv:
        with open(a.csv, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0]) if rows else ["claim_ref"])
            w.writeheader()
            w.writerows(rows)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
