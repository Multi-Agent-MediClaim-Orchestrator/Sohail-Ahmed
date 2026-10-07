"""Random-forest inference in numpy from a JSON export (no scikit-learn at runtime, no pickle)."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np

MODEL_PATH = Path(__file__).parent / "stamp_model.json"


class Forest:
    def __init__(self, spec: dict) -> None:  # type: ignore[type-arg]
        self.features: list[str] = spec["features"]
        self.trees = [{k: np.array(v) for k, v in t.items()} for t in spec["trees"]]
        self.threshold: float = spec.get("threshold", 0.5)
        self.meta: dict = spec.get("meta", {})  # type: ignore[type-arg]

    def proba(self, x: np.ndarray) -> float:
        p = 0.0
        for t in self.trees:
            node = 0
            while t["left"][node] != -1:
                node = (
                    t["left"][node]
                    if x[t["feature"][node]] <= t["threshold"][node]
                    else t["right"][node]
                )
            p += float(t["value"][node])
        return p / len(self.trees)


def load(path: Path = MODEL_PATH) -> Forest | None:
    return Forest(json.loads(path.read_text())) if path.exists() else None
