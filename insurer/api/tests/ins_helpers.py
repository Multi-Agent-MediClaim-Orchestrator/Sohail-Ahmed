"""Helpers shared by insurer-api tests (kept out of conftest.py so the name does not clash with other packages)."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

API_DIR = Path(__file__).resolve().parents[1]
APP_PW, RO_PW = "apppw", "ropw"


def alembic_cfg(url: str):
    from alembic.config import Config

    cfg = Config(str(API_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(API_DIR / "alembic"))
    cfg.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    return cfg


def migrate(url: str, target: str = "head") -> None:
    from alembic import command

    os.environ["INS_APP_PASSWORD"] = APP_PW
    os.environ["INS_RO_PASSWORD"] = RO_PW
    command.upgrade(alembic_cfg(url), target)


def app_url(owner_url: str) -> str:
    base, _, db = owner_url.rpartition("/")
    host = base.split("@", 1)[1]
    return f"postgresql+asyncpg://ins_app:{APP_PW}@{host}/{db}"


def register_claim_docs(e: Any, claim: dict[str, Any]) -> None:
    """Serve the bytes whose sha256/size the claim declares (the sample builder derives them from a known string)."""
    for d in claim["documents"]:
        from claim_contract.samples import doc_content

        e.docs[d["doc_id"]] = doc_content(d["doc_id"], d["doc_type"])


_stay = {"n": 0}


def unique_stay(days: int = 2) -> dict[str, str]:
    """Distinct, non-overlapping admission windows so tests never trip duplicate-claim detection by accident."""
    from datetime import date, timedelta

    _stay["n"] += 1
    start = date(2026, 1, 20) + timedelta(days=_stay["n"] * 5)
    assert start + timedelta(days=days) < date(2026, 10, 6), "ran out of unique stays"
    return {"admitted_on": start.isoformat(), "discharged_on": (start + timedelta(days=days)).isoformat()}


_used_members: set[str] = set()


def unique_member(min_si: int = 500_000) -> dict[str, Any]:
    """A seeded, active, never-used member (its own sum insured) + a fixed stay inside its policy period.
    Gives each test an isolated policy so utilisation and duplicate-claim detection never leak between tests."""
    from seeds.seed import build_master

    data = build_master()
    gold = next(pr["id"] for pr in data["products"] if pr["code"] == "HEALTH-PLUS-GOLD")  # no co-pay: payable == claimed for clean claims
    pols = {p["policy_number"]: p for p in data["policies"] if p["status"] == "active" and p["product_id"] == gold and p["sum_insured"] >= min_si and p["policy_number"] != "POL-NIV-2025-004411"}
    for m in data["members"]:
        p = pols.get(next((x["policy_number"] for x in data["policies"] if x["id"] == m["policy_id"]), ""))
        if p is None or m["member_id"] in _used_members or m["pre_existing"] or m["cover_start"] != p["start_date"] and False:
            continue
        _used_members.add(m["member_id"])
        return {"full_name": m["full_name"], "dob": m["dob"], "gender": m["gender"], "member_id": m["member_id"], "policy_number": p["policy_number"],
                "admitted_on": "2026-09-01", "discharged_on": "2026-09-03"}
    raise RuntimeError("no free seeded member left")


def reset_members() -> None:
    _used_members.clear()
