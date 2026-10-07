"""Static checks on the committed n8n workflow JSON (doc 08 task 9). Exit 1 on any violation."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

FLOWS = Path(__file__).resolve().parent.parent / "hospital" / "n8n" / "flows"
SECRETISH = re.compile(
    r"(Bearer [A-Za-z0-9._-]{12,}|password\"\s*:\s*\"[^=\"][^\"]*\"|client_secret\"\s*:\s*\"[^={\"][^\"]*\")",
    re.I,
)
REQUIRED_WEBHOOKS = {
    "intake/document-uploaded", "completeness/changed", "claim/submitted", "query/intake", "query/draft",
}  # fmt: skip


def lint() -> list[str]:
    errs: list[str] = []
    ids = set()
    paths = set()
    for f in sorted(FLOWS.glob("*.json")):
        d = json.loads(f.read_text())
        raw = f.read_text()
        ids.add(d["id"])
        if SECRETISH.search(raw):
            errs.append(f"{f.name}: looks like it contains a secret value")
        names = [n["name"] for n in d["nodes"]]
        if len(names) != len(set(names)):
            errs.append(f"{f.name}: duplicate node names")
        if not d["id"].startswith("hosp_") or f.stem != d["id"]:
            errs.append(f"{f.name}: id must match the file name and start with hosp_")
        is_util = d["id"].startswith("hosp_u") or d["id"] == "hosp_f8_error"
        if not is_util and d["settings"].get("errorWorkflow") != "hosp_f8_error":
            errs.append(f"{f.name}: no error workflow")
        for n in d["nodes"]:
            p = n["parameters"]
            if n["type"].endswith("httpRequest"):
                if not re.match(r"^=\{\{ \$env\.", p["url"]):
                    errs.append(f"{f.name}/{n['name']}: URL must start with an $env variable")
                if "http" in p["url"].replace("$env", "").split("'")[0][:8]:
                    errs.append(f"{f.name}/{n['name']}: hard-coded host")
            if n["type"].endswith(".wait"):
                secs = p["amount"] * {"seconds": 1, "minutes": 60, "hours": 3600}[p["unit"]]
                if secs > 86400:
                    errs.append(f"{f.name}/{n['name']}: Wait node over 24 h")
            if n["type"].endswith(".webhook"):
                paths.add(p["path"])
        # no cycles: the only back-edge allowed is the Split In Batches loop in the reminders flow
        graph = {
            a: [c["node"] for outs in v["main"] for c in outs] for a, v in d["connections"].items()
        }
        state: dict[str, int] = {}

        def visit(
            x: str,
            trail: tuple[str, ...] = (),
            state: dict[str, int] = state,
            graph: dict[str, list[str]] = graph,
            d: dict = d,
            f: Path = f,
        ) -> None:  # noqa: PLR0913
            if state.get(x) == 1:
                if "Batches" not in " ".join(trail) and d["id"] != "hosp_f6_reminders":
                    errs.append(f"{f.name}: loop through {x}")
                return
            if state.get(x) == 2:
                return
            state[x] = 1
            for y in graph.get(x, []):
                visit(y, (*trail, x), state, graph, d, f)
            state[x] = 2

        for start in list(graph):
            visit(start)
    missing = REQUIRED_WEBHOOKS - paths
    if missing:
        errs.append(f"missing webhooks: {sorted(missing)}")
    for need in ("hosp_u1_auth", "hosp_u2_idem", "hosp_u3_poll", "hosp_f8_error"):
        if need not in ids:
            errs.append(f"missing workflow {need}")
    return errs


if __name__ == "__main__":
    problems = lint()
    print("\n".join(problems) or "flows OK")
    sys.exit(1 if problems else 0)
