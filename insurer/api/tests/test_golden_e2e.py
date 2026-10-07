"""Golden synthetic cases (05-01 §5.1, §6) run end to end through insurer-api: hospital-sim -> receipt -> fetch -> verification -> gate.

Ground truth comes from data/synthetic (independent reference calculator + archetype rules), never from observing the system.
Archetypes that depend on image analysis (stamps, tamper, blur, handwriting, PII masking) are scored by doc-pipeline/vision; they are listed here as
explicitly skipped so the coverage gap is visible, not silent."""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from app.services import jobs
from app.services.utilisation import policy_year
from sqlalchemy import text

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[3]
GOLDEN = ROOT / "data" / "synthetic" / "golden"

spec = importlib.util.spec_from_file_location("synth_generate_e2e", ROOT / "data" / "synthetic" / "synth" / "generate.py")
gen = importlib.util.module_from_spec(spec)
sys.modules["synth_generate_e2e"] = gen
spec.loader.exec_module(gen)

CASES = {c["archetype"]: c for c in (json.loads(p.read_text(encoding="utf-8")) for p in sorted(GOLDEN.glob("SYN-*/case.json")))}
IMAGE_BOUND = {"S04": "hospital late-filing rule", "S07": "doc-pipeline quality class", "S08": "vision stamp detection", "S17": "vision tamper detection", "S23": "doc-pipeline handwriting", "S25": "privacy suite",
               "S20": "covered by test_query_flow (round-3 escalation under a fake clock)"}
RUN = sorted(a for a in CASES if a not in IMAGE_BOUND)


async def q(env, sql, **p):
    async with env.sm() as s:
        return (await s.execute(text(sql), p)).all()


def docs_of(case) -> dict[str, bytes]:
    d = GOLDEN / case["case_id"] / "docs"
    out = {}
    for doc in case["submission"]["documents"]:
        out[doc["doc_id"]] = next(d.glob(f"{doc['doc_type']}_{doc['doc_id'][-4:]}.pdf")).read_bytes()
    return out


async def submit_as_hospital(env, sub):
    """Each hospital signs with its own key (hosp-00N <-> HOSP-000N); the API rejects a claim for a different hospital."""
    return await env.sim.submit(sub, key_id="hosp-" + sub["admission"]["hospital_id"].split("-")[1][1:])


async def run_case(env, case):
    sub = case["submission"]
    env.docs.update(docs_of(case))
    if case["utilised_prior"] != "0.00":
        pol = (await q(env, "SELECT id, start_date FROM core.policy WHERE policy_number = :n", n=case["policy"]["policy_number"]))[0]
        yr = policy_year(pol.start_date, date.fromisoformat(sub["admission"]["admitted_on"]))
        async with env.sm() as s:
            await s.execute(text("INSERT INTO core.policy_claim_utilisation (id, policy_id, policy_year, utilised_amount) VALUES (gen_random_uuid(), :p, :y, :a) "
                                 "ON CONFLICT (policy_id, policy_year) DO UPDATE SET utilised_amount = :a"), {"p": pol.id, "y": yr, "a": Decimal(case["utilised_prior"])})
            await s.commit()
    r = await submit_as_hospital(env, sub)
    if case["expected"]["insurer"]["rejected_at_door"]:
        return r, None
    assert r.status_code == 202, r.text
    await jobs.drain({"fetch_documents", "start_verification"})
    row = (await q(env, "SELECT id, status::text AS st, approved_amount FROM core.claim_case WHERE hospital_claim_ref = :r", r=sub["claim_ref"]))[0]
    return r, row


@pytest.mark.parametrize("arch", RUN)
async def test_archetype_matches_ground_truth(env, arch):
    case = CASES[arch]
    exp = case["expected"]["insurer"]
    if arch == "S18":  # the earlier claim of the pair must exist first
        base = gen.build_case("S01", 9018 - 1000, 42, gen.build_master())
        gen.write_case(base, GOLDEN.parent / "tmp_base")
        env.docs.update({d["doc_id"]: next((GOLDEN.parent / "tmp_base" / base["case_id"] / "docs").glob(f"{d['doc_type']}_{d['doc_id'][-4:]}.pdf")).read_bytes() for d in base["submission"]["documents"]})
        assert (await submit_as_hospital(env, base["submission"])).status_code == 202
        await jobs.drain({"fetch_documents", "start_verification"})
    r, row = await run_case(env, case)
    if exp["rejected_at_door"]:
        assert r.status_code == 422 and r.json()["code"] == "totals_mismatch"
        return
    assert row is not None
    codes = {f["code"] for step in await q(env, "SELECT s.findings FROM core.verification_step s JOIN core.verification_run r ON r.id = s.run_id WHERE r.case_id = :c", c=row.id) for f in step.findings}
    for need in exp["finding_codes_include"]:
        assert need in codes, f"{arch}: expected finding {need}, got {sorted(codes)}"
    route = exp["route"]
    if route == "auto":
        assert row.st == "approved", f"{arch}: expected auto-approval, got {row.st} with findings {sorted(codes)}"
        assert Decimal(str(row.approved_amount)) == Decimal(exp["payable"])
    elif route == "none":
        assert row.st == exp["status"], f"{arch}: {row.st} != {exp['status']} ({sorted(codes)})"
    else:
        assert row.st in ("ready_for_decision", "needs_info", "escalated", "awaiting_approval"), f"{arch}: {row.st}"
        if row.st == "ready_for_decision" and not exp["finding_codes_include"]:
            rec = (await q(env, "SELECT approved_amount, gate_tier FROM core.decision WHERE case_id = :c AND kind = 'recommendation' ORDER BY created_at DESC LIMIT 1", c=row.id))
            assert rec, f"{arch}: no recommendation"
            assert rec[0].gate_tier == route, f"{arch}: tier {rec[0].gate_tier} != {route}"
            assert abs(Decimal(str(rec[0].approved_amount)) - Decimal(exp["payable"])) <= Decimal("0.05"), f"{arch}: payable {rec[0].approved_amount} != {exp['payable']}"


def test_image_bound_archetypes_are_listed_not_forgotten():
    assert set(IMAGE_BOUND) <= set(CASES) and not (set(IMAGE_BOUND) & set(RUN))
