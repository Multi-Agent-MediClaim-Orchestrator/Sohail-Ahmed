from __future__ import annotations

import datetime as dt
import random
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from faker import Faker

from synth import ids

CATALOG = yaml.safe_load(
    (Path(__file__).resolve().parents[1] / "catalog" / "hospital.yaml").read_text()
)
ANCHOR = dt.date(2026, 9, 30)  # all dates are relative to this, never the wall clock (determinism)


@dataclass
class Member:
    full_name: str
    dob: dt.date
    gender: str
    phone: str
    member_id: str
    policy_number: str
    aadhaar: str
    pan: str
    uhid: str


@dataclass
class Case:
    case_id: str
    seed: int
    archetype: str
    claim_type: str
    admission_type: str
    member: Member
    hospital: dict[str, Any]
    procedure: dict[str, Any]
    admitted_on: dt.date
    discharged_on: dt.date
    doctor: str
    bill_lines: list[dict[str, Any]] = field(default_factory=list)
    totals: dict[str, str] = field(default_factory=dict)


def make_member(rng: random.Random, seed: int, n: int) -> Member:
    fk = Faker("en_IN")
    fk.seed_instance(seed)
    g = rng.choice(["M", "F"])
    name = fk.name_male() if g == "M" else fk.name_female()
    name = " ".join(
        w
        for w in name.replace("Dr.", "")
        .replace("Mr.", "")
        .replace("Mrs.", "")
        .replace("Ms.", "")
        .split()
        if len(w) > 1
    )[:40]
    return Member(
        full_name=name, dob=dt.date(rng.randrange(1950, 2005), rng.randrange(1, 13), rng.randrange(1, 28)), gender=g,
        phone=ids.fake_phone(rng), member_id=f"MB{rng.randrange(10**9):09d}", policy_number=f"POL{n:06d}{rng.randrange(100):02d}",
        aadhaar=ids.fake_aadhaar(rng), pan=ids.fake_pan(rng), uhid=f"UH-{n:06d}",
    )  # fmt: skip


def make_doctor(rng: random.Random, seed: int) -> str:
    fk = Faker("en_IN")
    fk.seed_instance(seed + 1)
    return "Dr. " + " ".join(fk.name().replace("Dr.", "").split()[:2])
