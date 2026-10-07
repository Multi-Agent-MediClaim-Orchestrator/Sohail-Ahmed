"""Hospital-side evaluation harness (doc 05-02, skeleton). Runs the shared services' libraries over the synthetic corpus
and scores them against the generator's labels. `--llm none` measures the deterministic parts only (parser, rules,
code-extracted bill lines, stamps, quality); `--llm ollama` adds the model passes. Report: JSON with targets.

    uv run python -m evalh.run --corpus data/synthetic/out --out data/eval/report.json [--llm none|ollama] [--limit N]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import statistics
from collections import defaultdict
from decimal import Decimal
from pathlib import Path
from typing import Any

from docpipe.llm import Fake, Ollama
from docpipe.pipeline import run as parse_doc
from docpipe.settings import Settings as DocSettings
from vision.analyze import analyze as vision_analyze
from vision.imageio import load_pages
from vision.settings import Settings as VisionSettings

TARGETS = {
    "classify_acc": 0.95, "line_count_exact": 0.90, "line_amount_exact": 0.95, "total_exact": 0.90, "stamp_precision": 0.90,
    "stamp_recall": 0.90, "legible_acc": 0.90, "poor_flagged": 0.95,
}  # fmt: skip


def norm_amount(s: str) -> Decimal | None:
    try:
        return Decimal(re.sub(r"[,\s₹]|Rs\.?", "", str(s)))
    except Exception:  # noqa: BLE001
        return None


def ratio(num: int, den: int) -> float | None:
    return round(num / den, 4) if den else None


async def eval_case(
    case_dir: Path, ds: DocSettings, vs: VisionSettings, llm: Any
) -> dict[str, Any]:
    case = json.loads((case_dir / "case.json").read_text())
    out: dict[str, Any] = {"case_id": case["case_id"], "archetype": case["archetype"], "docs": []}
    for d in case["documents"]:
        raw = (case_dir / d["file"]).read_bytes()
        labels = json.loads((case_dir / d["labels"]).read_text())["labels"]
        row: dict[str, Any] = {
            "doc_type": d["doc_type"],
            "quality_class": d["quality_class"],
            "stamp_expected": d["stamp_present"],
        }
        parsed = await parse_doc(raw, ds, llm)
        row["doc_type_pred"] = parsed["doc_type"]
        row["parser"] = parsed["parser"]
        row["review_reasons"] = parsed["review_reasons"]
        t = parsed["passes"][0]["typed_json"] if parsed["passes"] else {}
        lab_lines = [x["value"].split("|") for x in labels if x["field"] == "bill_line"]
        pred_lines = t.get("lines") or []
        if lab_lines:
            row["lines_expected"], row["lines_pred"] = len(lab_lines), len(pred_lines)
            want = sorted(norm_amount(a) for _, a in lab_lines)
            got = sorted(norm_amount(x["amount"]) for x in pred_lines)
            row["line_amounts_match"] = want == got
        tl = next((x["value"] for x in labels if x["field"] == "total"), None)
        if tl is not None:
            row["total_expected"], row["total_pred"] = (
                str(norm_amount(tl)),
                str(norm_amount(t["total"])) if t.get("total") else None,
            )
            row["total_match"] = row["total_expected"] == row["total_pred"]
        pages = load_pages(raw)
        rep = await vision_analyze(
            pages,
            vs,
            [],
            expect=["hospital_stamp"] if d["doc_type"] in ("final_bill", "pharmacy_bill") else [],
        )
        row["legible_pred"] = rep["all_pages_legible"]
        row["legible_expected"] = d["quality_class"] in ("good", "acceptable")
        if d["doc_type"] in ("final_bill", "pharmacy_bill") and rep["all_pages_legible"]:
            row["stamp_pred"] = rep["required_stamp_present"]
        out["docs"].append(row)
    return out


def score(rows: list[dict[str, Any]]) -> dict[str, Any]:
    docs = [x for r in rows for x in r["docs"]]
    m: dict[str, Any] = {"cases": len(rows), "documents": len(docs)}
    m["classify_acc"] = ratio(sum(d["doc_type_pred"] == d["doc_type"] for d in docs), len(docs))
    good = [
        d for d in docs if d["quality_class"] in ("good", "acceptable")
    ]  # extraction is scored on readable documents
    ld = [d for d in good if "lines_expected" in d]
    m["line_count_exact"] = ratio(sum(d["lines_expected"] == d["lines_pred"] for d in ld), len(ld))
    m["line_amount_exact"] = ratio(sum(d["line_amounts_match"] for d in ld), len(ld))
    td = [d for d in good if "total_match" in d]
    m["total_exact"] = ratio(sum(d["total_match"] for d in td), len(td))
    sd = [d for d in docs if "stamp_pred" in d]
    tp = sum(d["stamp_pred"] is True and d["stamp_expected"] for d in sd)
    fp = sum(d["stamp_pred"] is True and not d["stamp_expected"] for d in sd)
    fn = sum(d["stamp_pred"] is False and d["stamp_expected"] for d in sd)
    m["stamp_precision"], m["stamp_recall"] = ratio(tp, tp + fp), ratio(tp, tp + fn)
    poor = [d for d in docs if d["quality_class"] in ("poor", "unreadable")]
    m["poor_flagged"] = ratio(
        sum(bool(d["review_reasons"]) or not d["legible_pred"] for d in poor), len(poor)
    )  # damaged pages must not pass silently
    m["legible_acc"] = ratio(
        sum(d["legible_pred"] == d["legible_expected"] for d in docs), len(docs)
    )
    per: dict[str, list[bool]] = defaultdict(list)
    for d in docs:
        per[d["doc_type"]].append(d["doc_type_pred"] == d["doc_type"])
    m["classify_by_type"] = {k: ratio(sum(v), len(v)) for k, v in sorted(per.items())}
    m["pass"] = {k: (m[k] is not None and m[k] >= t) for k, t in TARGETS.items()}
    m["targets"] = TARGETS
    return m


async def main_async(a: argparse.Namespace) -> dict[str, Any]:
    ds = DocSettings(parser="auto", allow_cloud=False)
    vs = VisionSettings()
    llm = (
        Ollama(ds.llm_base_url, 240) if a.llm == "ollama" else Fake({})
    )  # `none`: every model call is unavailable
    root = Path(a.corpus)  # noqa: ASYNC240
    dirs = sorted(p for p in root.iterdir() if p.is_dir())[: a.limit or None]  # noqa: ASYNC240
    rows = [await eval_case(d, ds, vs, llm) for d in dirs]
    rep = {"mode": a.llm, "metrics": score(rows), "cases": rows}
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)  # noqa: ASYNC240
    Path(a.out).write_text(json.dumps(rep, indent=1, default=str))  # noqa: ASYNC240
    return rep


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--corpus", default="data/synthetic/out")
    ap.add_argument("--out", default="data/eval/report.json")
    ap.add_argument("--llm", choices=["none", "ollama"], default="none")
    ap.add_argument("--limit", type=int, default=0)
    a = ap.parse_args()
    rep = asyncio.run(main_async(a))
    m = rep["metrics"]
    print(
        json.dumps(
            {k: v for k, v in m.items() if k not in ("targets", "classify_by_type")}, indent=1
        )
    )
    print("median ok" if statistics.median([1]) else "")


if __name__ == "__main__":
    main()
