"""Chaos module (04-06 §6.6): seeded, reproducible fault injection on outbound callbacks. Off by default."""

from __future__ import annotations

import hashlib
import random
from dataclasses import asdict, dataclass

PRESETS: dict[str, dict[str, float | int | bool]] = {
    "off": {},
    "flaky": {"drop_rate": 0.2, "duplicate_rate": 0.2, "out_of_order": True, "reorder_window": 3},
    "slow": {"delay_ms": 800, "jitter_ms": 400},
    "hostile": {"drop_rate": 0.35, "duplicate_rate": 0.35, "out_of_order": True, "reorder_window": 5, "delay_ms": 200, "jitter_ms": 200},
}


@dataclass
class Chaos:
    drop_rate: float = 0.0
    duplicate_rate: float = 0.0
    delay_ms: int = 0
    jitter_ms: int = 0
    out_of_order: bool = False
    reorder_window: int = 1
    fail_status: int = 0  # sim's own insurer endpoints answer this status ...
    fail_count: int = 0  # ... for the next N calls
    seed: int = 42

    def __post_init__(self) -> None:
        self._rngs: dict[str, random.Random] = {}

    def rng(self, claim_ref: str) -> random.Random:
        if claim_ref not in self._rngs:
            h = int(hashlib.sha256(f"{self.seed}|{claim_ref}".encode()).hexdigest()[:12], 16)
            self._rngs[claim_ref] = random.Random(h)
        return self._rngs[claim_ref]

    @property
    def active(self) -> bool:
        return bool(self.drop_rate or self.duplicate_rate or self.delay_ms or self.out_of_order or self.fail_count)

    def update(self, **kw: float | int | bool | str) -> None:
        if "preset" in kw:
            kw = {**PRESETS[str(kw.pop("preset"))], **kw}
            self.clear()
        for k, v in kw.items():
            if not hasattr(self, k) or k.startswith("_"):
                raise ValueError(f"unknown chaos setting {k}")
            setattr(self, k, v)
        self._rngs.clear()

    def clear(self) -> None:
        seed = self.seed
        self.__init__()  # type: ignore[misc]
        self.seed = seed

    def snapshot(self) -> dict[str, float | int | bool]:
        return asdict(self)

    def should_drop(self, claim_ref: str, attempt: int, max_attempts: int) -> bool:
        if not self.drop_rate:
            return False
        if self.drop_rate >= 1.0:
            return True
        # never drop the final attempt of a claim unless drop_rate == 1.0 (so flaky presets always converge)
        return attempt < max_attempts - 1 and self.rng(claim_ref).random() < self.drop_rate

    def should_duplicate(self, claim_ref: str) -> bool:
        return bool(self.duplicate_rate) and self.rng(claim_ref).random() < self.duplicate_rate

    def take_failure(self) -> int:
        if self.fail_count > 0 and self.fail_status:
            self.fail_count -= 1
            return self.fail_status
        return 0
