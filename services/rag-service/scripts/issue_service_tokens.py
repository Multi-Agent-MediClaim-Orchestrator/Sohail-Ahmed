"""Issue HS256 service tokens (04-05 task 14). Prints NAME=token lines; with --file writes a JSON file (SERVICE_TOKENS_FILE).

    JWT_SECRET=... python scripts/issue_service_tokens.py [--file rag_tokens.json]
"""

from __future__ import annotations

import argparse
import json
import os

from rag_service import security


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--file")
    a = ap.parse_args()
    secret = os.environ.get("JWT_SECRET")
    if not secret:
        raise SystemExit("JWT_SECRET not set")
    tokens = {svc: security.issue_token(secret, svc, role="admin" if svc.endswith("-api") else "service") for svc in security.CALLERS}
    if a.file:
        with open(a.file, "w", encoding="utf-8") as f:
            json.dump(tokens, f, indent=1)
    else:
        for svc, t in tokens.items():
            name = svc.upper().replace("-", "_")
            print(f"{name}_RAG_TOKEN={t}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
