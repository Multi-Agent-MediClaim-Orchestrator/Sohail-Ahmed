"""Train the stamp classifier on synthetic hard-stamp pages and export it as JSON (numpy inference at runtime).
Needs scikit-learn (dev dependency of this package only).   uv run python -m evalh.train_stamps [--n 600] [--seed 7]"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
from sklearn.ensemble import RandomForestClassifier
from sklearn.metrics import precision_recall_curve
from sklearn.model_selection import train_test_split
from synth.stamps_hard import build_set
from vision import stamp_candidates, stamp_model

from evalh.stamps_eval import iou


def dataset(n: int, seed: int) -> tuple[np.ndarray, np.ndarray]:
    X, y = [], []
    for page, labels, _ in build_set(n, seed):
        gt = [x["bbox"] for x in labels if x["kind"] == "hospital_stamp"]
        for bbox, feat in stamp_candidates.candidates(np.asarray(page)):
            X.append(feat)
            y.append(int(any(iou(bbox, g) >= 0.4 for g in gt)))
    return np.array(X), np.array(y)


def export(rf: RandomForestClassifier, threshold: float, meta: dict) -> dict:  # type: ignore[type-arg]
    trees = []
    for est in rf.estimators_:
        t = est.tree_
        v = t.value[:, 0, :]
        trees.append({"left": t.children_left.tolist(), "right": t.children_right.tolist(), "feature": t.feature.tolist(),
                      "threshold": t.threshold.tolist(), "value": (v[:, 1] / v.sum(axis=1)).tolist()})  # fmt: skip
    return {
        "features": stamp_candidates.FEATURES,
        "trees": trees,
        "threshold": threshold,
        "meta": meta,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=600)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--out", default=str(stamp_model.MODEL_PATH))
    a = ap.parse_args()
    X, y = dataset(a.n, a.seed)
    print(f"{len(y)} candidates, {int(y.sum())} positive")
    Xtr, Xva, ytr, yva = train_test_split(X, y, test_size=0.25, random_state=0, stratify=y)
    rf = RandomForestClassifier(
        n_estimators=40,
        max_depth=8,
        min_samples_leaf=3,
        class_weight="balanced",
        random_state=0,
        n_jobs=-1,
    ).fit(Xtr, ytr)
    p, r, th = precision_recall_curve(yva, rf.predict_proba(Xva)[:, 1])
    f1 = 2 * p[:-1] * r[:-1] / np.maximum(1e-9, p[:-1] + r[:-1])
    thr = float(th[int(np.argmax(f1))])
    rf = RandomForestClassifier(
        n_estimators=40,
        max_depth=8,
        min_samples_leaf=3,
        class_weight="balanced",
        random_state=0,
        n_jobs=-1,
    ).fit(X, y)
    spec = export(
        rf,
        thr,
        {
            "trained_on": f"{a.n} synthetic pages, seed {a.seed}",
            "candidates": int(len(y)),
            "positives": int(y.sum()),
            "note": "synthetic data only",
        },
    )
    Path(a.out).write_text(json.dumps(spec))
    print(f"wrote {a.out}  threshold {thr:.2f}  validation F1 {f1.max():.3f}")


if __name__ == "__main__":
    main()
