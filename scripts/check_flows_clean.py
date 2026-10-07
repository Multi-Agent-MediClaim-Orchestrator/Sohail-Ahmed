"""Hygiene check for exported n8n flows (03-09 task 18): no credential values, tokens in URLs, JWTs or foreign hostnames.

    python scripts/check_flows_clean.py [dir ...]        # default: insurer/n8n/flows
Exit 1 on any violation.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

JWT = re.compile(r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}")
URL_SECRET = re.compile(r"[?&](token|key|api_key|apikey|secret|password)=[^&{\s]+", re.IGNORECASE)
HOST = re.compile(r"https?://([A-Za-z0-9.-]+)")
ALLOWED_HOSTS = {"localhost", "127.0.0.1"}


def strings(o: Any) -> list[str]:
    if isinstance(o, str):
        return [o]
    if isinstance(o, dict):
        return [s for v in o.values() for s in strings(v)]
    if isinstance(o, list):
        return [s for v in o for s in strings(v)]
    return []


def check(flow: dict[str, Any], name: str) -> list[str]:
    bad: list[str] = []
    if flow.get("pinData"):
        bad.append(f"{name}: pinData present")
    for n in flow.get("nodes", []):
        for cname, cval in (n.get("credentials") or {}).items():
            if isinstance(cval, dict) and (cval.get("data") or cval.get("value")):
                bad.append(f"{name}:{n.get('name')}: credential {cname} carries data")
    for s in strings(flow.get("nodes", [])):
        if JWT.search(s):
            bad.append(f"{name}: JWT-looking string")
        if URL_SECRET.search(s):
            bad.append(f"{name}: secret in URL query")
        for h in HOST.findall(s):
            if h not in ALLOWED_HOSTS:
                bad.append(f"{name}: hard-coded host {h}")
    return bad


def main(argv: list[str]) -> int:
    dirs = [Path(a) for a in argv] or [Path(__file__).resolve().parents[1] / "insurer" / "n8n" / "flows"]
    problems: list[str] = []
    n = 0
    for d in dirs:
        for f in sorted(d.glob("*.json")):
            n += 1
            problems += check(json.loads(f.read_text(encoding="utf-8")), f.name)
    for p in problems:
        print(p, file=sys.stderr)
    print(f"checked {n} flows, {len(problems)} problems")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
