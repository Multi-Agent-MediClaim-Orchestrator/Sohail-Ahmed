"""``make doctor``: one-screen health table for the insurer side (05-04 §8.1). Works without Prometheus.

    python scripts/doctor.py            # exit 1 if a required service is down
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import urllib.error
import urllib.request

SERVICES = [  # name, base url, path, required
    ("insurer-api", os.environ.get("INS_API_URL", "http://localhost:8100"), "/v1/ready", True),
    ("calc-engine", os.environ.get("INS_CALC_URL", "http://localhost:8120"), "/v1/health", False),
    ("insurer-crew", os.environ.get("INS_CREW_URL", "http://localhost:8110"), "/v1/health", False),
    ("rag-service", os.environ.get("INS_RAG_URL", "http://localhost:8400"), "/health", False),
    ("llm-gateway", os.environ.get("LLM_GATEWAY_URL", "http://localhost:4000"), "/health/liveliness", False),
    ("tpa-sim", os.environ.get("TPA_SIM_URL", "http://localhost:8500"), "/sim/health", False),
    ("insurer-ui", os.environ.get("INS_UI_URL", "http://localhost:3100"), "/", False),
]


def probe(url: str) -> tuple[str, dict]:
    try:
        with urllib.request.urlopen(url, timeout=2) as r:
            body = r.read(4096).decode("utf-8", "replace")
            try:
                j = json.loads(body)
            except ValueError:
                j = {}
            return ("ok", j)
    except urllib.error.HTTPError as e:
        return (f"http {e.code}", {})
    except Exception:  # noqa: BLE001
        return ("down", {})


def main() -> int:
    print(f"{'SERVICE':<14} {'UP':<8} {'STATUS':<10} NOTE")
    bad = False
    for name, base, path, required in SERVICES:
        up, j = probe(base.rstrip("/") + path)
        status = j.get("status", "-") if isinstance(j, dict) else "-"
        note = ""
        if isinstance(j, dict) and j.get("deps"):
            note = " ".join(f"{k}={v}" for k, v in j["deps"].items())
        print(f"{name:<14} {up:<8} {status:<10} {note}")
        if required and up != "ok":
            bad = True
    total, used, free = shutil.disk_usage(".")
    print(f"\nDISK free {free * 100 // total}%")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
