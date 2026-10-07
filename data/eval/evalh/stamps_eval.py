"""Stamp detector evaluation on a held-out hard-stamp set: box precision/recall and page-level 'has a stamp', by difficulty,
for the classical rule and the learned forest.   uv run python -m evalh.stamps_eval [--model path] [--n 90] [--seed 99]"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from typing import Any

import numpy as np
from synth.stamps_hard import build_set
from vision import stamp_model, stamps

STAMP_KINDS = ("hospital_stamp", "seal")


def iou(a: list[int] | tuple[int, ...], b: list[int] | tuple[int, ...]) -> float:
    ix = max(0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0, min(a[3], b[3]) - max(a[1], b[1]))
    i = ix * iy
    u = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - i
    return i / u if u else 0.0


def score(detect: Any, data: list[Any], thr: float = 0.5) -> dict[str, Any]:
    st: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for page, labels, diff in data:
        gt = [x["bbox"] for x in labels if x["kind"] == "hospital_stamp"]
        pred = [
            s.bbox
            for s in detect(np.asarray(page), 1)
            if s.kind in STAMP_KINDS and s.det_conf >= thr
        ]
        s = st[diff]
        used: set[int] = set()
        for p in pred:
            j = next((k for k, g in enumerate(gt) if k not in used and iou(p, g) >= 0.4), None)
            if j is None:
                s["fp"] += 1
            else:
                used.add(j)
                s["tp"] += 1
        s["fn"] += len(gt) - len(used)
        has, got = bool(gt), bool(pred)
        s["pt"] += has and got
        s["pf"] += (not has) and got
        s["pm"] += has and not got
    out: dict[str, Any] = {}
    tot: dict[str, int] = defaultdict(int)
    for d, s in list(st.items()) + [("all", tot)]:
        if d == "all":
            for v in st.values():
                for k, x in v.items():
                    s[k] += x
        out[d] = {
            "box_precision": round(s["tp"] / max(1, s["tp"] + s["fp"]), 3), "box_recall": round(s["tp"] / max(1, s["tp"] + s["fn"]), 3),
            "page_precision": round(s["pt"] / max(1, s["pt"] + s["pf"]), 3), "page_recall": round(s["pt"] / max(1, s["pt"] + s["pm"]), 3),
        }  # fmt: skip
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=90)
    ap.add_argument("--seed", type=int, default=99)
    ap.add_argument("--model", default=str(stamp_model.MODEL_PATH))
    a = ap.parse_args()
    data = build_set(a.n, a.seed)
    res = {"classical": score(stamps.detect_classical, data)}
    fo = stamp_model.load(__import__("pathlib").Path(a.model))
    if fo is not None:
        res["learned"] = score(lambda im, pg: stamps.detect_learned(im, pg, fo), data, fo.threshold)
    print(json.dumps(res, indent=1))


if __name__ == "__main__":
    main()
