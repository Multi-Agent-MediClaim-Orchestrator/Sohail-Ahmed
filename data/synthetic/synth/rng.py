from __future__ import annotations

import hashlib
import random


def case_seed(global_seed: int, idx: int) -> int:
    return int(hashlib.sha256(f"{global_seed}|{idx}".encode()).hexdigest()[:8], 16)


def case_rng(global_seed: int, idx: int) -> random.Random:
    return random.Random(case_seed(global_seed, idx))  # noqa: S311  (synthetic data, not security)
