"""CLI: ``python -m calc_engine explain <input.json|golden.json>`` | ``run`` | ``golden-update <dir>``."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from .engine import run
from .explain import explain
from .models import CalcInput


def _load(path: str) -> CalcInput:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    return CalcInput.model_validate(raw.get("input", raw))


def main(argv: list[str]) -> int:
    if len(argv) < 2 or argv[0] not in ("explain", "run", "golden-update"):
        print(__doc__)
        return 2
    cmd, target = argv[0], argv[1]
    if cmd == "explain":
        inp = _load(target)
        print(explain(run(inp), str(inp.case_id)))
    elif cmd == "run":
        print(run(_load(target)).model_dump_json(indent=1))
    else:  # golden-update: print the engine's numbers for review; expected values are never auto-overwritten
        for p in sorted(Path(target).glob("*.json")):
            res = run(_load(str(p)))
            print(f"{p.stem}: payable={res.payable_total} pays={res.patient_pays_total} blocked={res.blocked}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main(sys.argv[1:]))
