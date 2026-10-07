"""Synthetic claims corpus + ground truth (05-01). Everything is fictional and derived from insurer_app.seeds.

    python data/synthetic/synth/generate.py --n 500 --seed 42 --out data/synthetic/out
    python data/synthetic/synth/generate.py --golden            # the 20 committed golden cases

Scope note (deviation, see PROGRESS-DEV-B.md): documents are deterministic text-layer PDFs written by a tiny stdlib writer; raster degradation
(blur/skew/photo) and image tampering need Pillow/ReportLab and belong with doc-pipeline (Dev A). Those archetypes carry *labels* (what would be
applied and what the expected detection is) so the evaluation harness can score them once the images exist. Insurer-side ground truth
(identity, coverage, calculation, route) is complete and independent of the engine: payables come from an independent reference calculator."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import random
import sys
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "insurer" / "api"))
from claim_contract.insurer_side.samples import make_line, make_submission, money  # noqa: E402
from insurer_app.seeds.seed import build_master  # noqa: E402

GENERATOR_VERSION = "1.0.0"
CENT = Decimal("0.01")
T_AUTO, T_FOUR = Decimal("50000"), Decimal("500000")


def _load_reference():
    spec = importlib.util.spec_from_file_location("reference_calc", ROOT / "insurer" / "calc_engine" / "tests" / "reference_calc.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)  # imports nothing from calc_engine: an independent implementation
    return m.reference


reference = _load_reference()


def _load_base_rules():
    spec = importlib.util.spec_from_file_location("calc_builders", ROOT / "insurer" / "calc_engine" / "tests" / "builders.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m.rules


base_rules = _load_base_rules()  # full rule dict with every key the reference expects; the product rules override it

# category -> calc-engine mapped_group, written independently here (not imported)
GROUP = {"room": "room_rent", "icu": "icu", "surgery": "surgeon_fees", "anaesthesia": "anaesthesia", "medicine": "medicine", "consumable": "consumable", "implant": "implant",
         "investigation": "investigation", "consultation": "doctor_fees", "ot": "ot_charges", "nonmedical": "non_medical"}

PROCEDURES = [  # (name, icd10, procedure code, typical LOS, implant, procedure group)
    ("Appendectomy", "K35.8", "0DTJ4ZZ", 3, False, "abdominal_surgery"), ("Cholecystectomy", "K80.2", "0FT44ZZ", 3, False, "abdominal_surgery"), ("Hernia repair", "K40.9", "0YQ50ZZ", 2, False, "hernia"),
    ("Total knee replacement", "M17.1", "0SRC0JZ", 5, True, "knee_replacement"), ("Coronary angioplasty", "I25.1", "02703ZZ", 3, True, "cardiac"), ("Dengue fever", "A90", "", 4, False, None),
    ("Pneumonia", "J18.9", "", 5, False, None), ("Cataract surgery", "H25.9", "08RJ3JZ", 1, True, "cataract"), ("Kidney stone removal", "N20.0", "0TC30ZZ", 2, False, "urology"),
]
PROC_ATTACHED = {"surgeon_fees", "ot_charges", "anaesthesia", "implant", "procedure_package"}  # line groups that belong to the procedure (07 §5)


# --------------------------------------------------------------------------------------------------
# minimal deterministic PDF writer (text layer, A4, one page per chunk of lines)
# --------------------------------------------------------------------------------------------------
def pdf_bytes(title: str, lines: list[str], *, producer: str = "synthetic-gen") -> bytes:
    def esc(s: str) -> str:
        return s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")

    pages = [lines[i : i + 48] for i in range(0, max(len(lines), 1), 48)] or [[]]
    objs: list[bytes] = []
    kids = " ".join(f"{5 + 2 * i} 0 R" for i in range(len(pages)))
    objs.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    objs.append(f"<< /Type /Pages /Kids [{kids}] /Count {len(pages)} >>".encode())
    objs.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    objs.append(f"<< /Producer ({esc(producer)}) /Title ({esc(title)}) /CreationDate (D:20260101000000Z) >>".encode())
    for i, pg in enumerate(pages):
        body = "BT /F1 10 Tf 40 800 Td 12 TL\n" + "\n".join(f"({esc(t)}) Tj T*" for t in ([title, ""] + pg if i == 0 else pg)) + "\nET"
        objs.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Resources << /Font << /F1 3 0 R >> >> /Contents {6 + 2 * i} 0 R >>".encode())
        objs.append(f"<< /Length {len(body.encode('latin-1', 'replace'))} >>\nstream\n".encode() + body.encode("latin-1", "replace") + b"\nendstream")
    out = bytearray(b"%PDF-1.4\n")
    offs = []
    for n, o in enumerate(objs, start=1):
        offs.append(len(out))
        out += f"{n} 0 obj\n".encode() + o + b"\nendobj\n"
    x = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode() + b"".join(f"{o:010d} 00000 n \n".encode() for o in offs)
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R /Info 4 0 R >>\nstartxref\n{x}\n%%EOF\n".encode()
    return bytes(out)


def doc_lines(doc_type: str, case: dict[str, Any]) -> list[str]:
    p, a, t = case["submission"]["patient"], case["submission"]["admission"], case["submission"]["totals"]
    base = [f"Patient: {p['full_name']}", f"DOB: {p['dob']}", f"Member: {p['member_id']}", f"Policy: {p['policy_number']}", f"Admitted: {a['admitted_on']}  Discharged: {a['discharged_on']}"]
    if doc_type in ("final_bill", "itemised_bill"):
        for ln in case["submission"]["bill_lines"]:
            base.append(f"{ln['line_id']} {ln['description']} qty {ln['qty']} x {ln['unit_price']['amount']} = {ln['amount']['amount']}")
        base += [f"Gross: {t['gross']['amount']}", f"Net claimed: {t['claimed']['amount']}", "Hospital stamp: " + ("PRESENT" if case["labels"]["stamp_present"] else "ABSENT")]
    elif doc_type == "discharge_summary":
        base += [f"Diagnosis: {', '.join(a['diagnosis_codes'])}", "Course: uneventful recovery.", "Signed: Dr. R. Menon"]
    return base


# --------------------------------------------------------------------------------------------------
# archetypes (05-01 §6). ``applies`` lists which component the ground truth is meant to score.
# --------------------------------------------------------------------------------------------------
ARCH: dict[str, dict[str, Any]] = {
    "S01": dict(name="clean_cashless_planned", profile="small", route="auto"),
    "S02": dict(name="clean_cashless_emergency", profile="small", admission="emergency", route="auto"),
    "S03": dict(name="clean_reimbursement_planned", profile="small", claim_type="reimbursement", extra_docs=["payment_receipt", "cancelled_cheque"], route="auto"),
    "S04": dict(name="reimbursement_late_filing", profile="small", claim_type="reimbursement", extra_docs=["payment_receipt", "cancelled_cheque"], late_filing=True, route="reviewer", applies=["hospital"]),
    "S05": dict(name="missing_required_doc", profile="small", drop_docs=["discharge_summary"], route="none", codes=["completeness.missing_required"], status="needs_info"),
    "S06": dict(name="missing_conditional_doc", profile="implant", route="none", codes=["completeness.missing_required"], status="needs_info", proc=3),
    "S07": dict(name="blurry_discharge_summary", profile="small", degrade={"blur_sigma": 2.5}, quality="poor", route="auto", applies=["doc-pipeline"]),
    "S08": dict(name="missing_stamp", profile="small", stamp=False, route="none", labels_only=True, applies=["hospital", "vision"]),
    "S09": dict(name="wrong_patient_doc", profile="small", name_swap=True, route="reviewer", codes=["identity.name_mismatch"]),
    "S10": dict(name="bill_total_mismatch", profile="small", total_mismatch=True, route="none", rejected_at_door=True),
    "S11": dict(name="room_rent_over_cap", profile="roomcap", route="reviewer"),
    "S12": dict(name="copay_and_deductible", profile="standard", product="SENIOR-SHIELD", route="reviewer"),
    "S13": dict(name="waiting_period_violation", profile="small", waiting=True, route="reviewer", codes=["coverage.waiting_period"]),
    "S14": dict(name="exclusion_hit", profile="small", dx="Z41.1", route="reviewer", codes=["coverage.exclusion"]),
    "S15": dict(name="policy_expired", profile="small", expired=True, route="reviewer", codes=["coverage.outside_period"]),
    "S16": dict(name="sum_insured_exhausted", profile="standard", utilised_frac="0.97", route="reviewer"),
    "S17": dict(name="tampered_amount", profile="standard", tamper="amount_edit", route="reviewer", applies=["vision", "insurer-authenticity"], labels_only=True),
    "S18": dict(name="duplicate_claim", profile="small", duplicate_of="prev", route="reviewer", codes=["auth.duplicate_claim"]),
    "S19": dict(name="query_loop_resolved_r2", profile="small", drop_docs=["id_proof"], route="none", scenario="query_twice_then_approve", codes=["completeness.missing_required"], status="needs_info"),
    "S20": dict(name="query_loop_escalates_r3", profile="small", drop_docs=["id_proof"], route="none", scenario="query_three_rounds_escalate", codes=["completeness.missing_required"], status="needs_info"),
    "S21": dict(name="high_value_over_T_four", profile="large", route="dual_approver"),
    "S22": dict(name="mid_value_over_T_auto", profile="standard", route="single_approver"),
    "S23": dict(name="handwritten_notes", profile="small", handwritten=True, route="auto", applies=["doc-pipeline"]),
    "S24": dict(name="multi_page_bill_50_lines", profile="fifty", route="single_approver"),
    "S25": dict(name="pii_stress", profile="small", pii_free_text=True, route="auto", applies=["doc-pipeline", "privacy"]),
}

PROFILES: dict[str, list[tuple[str, str, str, str]]] = {  # (category, description, qty, unit)
    "small": [("room", "Room rent (semi-private)", "2", "3000.00"), ("medicine", "Medicines", "1", "8000.00"), ("investigation", "Investigations", "1", "4000.00"), ("consultation", "Consultation", "1", "2000.00")],
    "standard": [("room", "Room rent (semi-private)", "3", "4000.00"), ("surgery", "Surgeon fee", "1", "40000.00"), ("medicine", "Medicines", "1", "18000.00"), ("investigation", "Investigations", "1", "6500.00"), ("other", "OT charges", "1", "12000.00")],
    "roomcap": [("room", "Room rent (suite)", "4", "7000.00"), ("surgery", "Surgeon fee", "1", "40000.00"), ("anaesthesia", "Anaesthetist fee", "1", "9000.00"), ("medicine", "Medicines", "1", "15000.00"), ("investigation", "Investigations", "1", "6500.00")],
    "implant": [("room", "Room rent (semi-private)", "4", "4000.00"), ("surgery", "Surgeon fee", "1", "60000.00"), ("implant", "Knee prosthesis", "1", "95000.00"), ("medicine", "Medicines", "1", "12000.00")],
    "large": [("room", "Room rent (semi-private)", "6", "4500.00"), ("surgery", "Surgeon fee", "1", "180000.00"), ("implant", "Cardiac stent set", "1", "260000.00"), ("medicine", "Medicines", "1", "45000.00"), ("investigation", "Investigations", "1", "30000.00")],
}


def lines_for(profile: str, rng: random.Random, si: int = 500000) -> list[dict[str, Any]]:
    if profile == "fifty":
        rows = [("room", "Room rent (semi-private)", "3", "3000.00")] + [("medicine", f"Medicine item {i}", str(1 + i % 3), f"{300 + (i * 37) % 900}.00") for i in range(40)] + \
               [("consumable", f"Consumable {i}", "2", f"{150 + (i * 11) % 200}.00") for i in range(9)]
        rows += [("surgery", "Surgeon fee", "1", "30000.00")]
    else:
        rows = list(PROFILES[profile])
        if profile == "roomcap":  # suite rate 40% above the 1%-of-SI daily cap, whatever the sum insured
            rows[0] = ("room", "Room rent (suite)", "4", f"{int(si * 0.01 * 1.4)}.00")
    out = []
    for i, (cat, desc, qty, unit) in enumerate(rows, start=1):
        jitter = Decimal(rng.randint(0, 4)) * Decimal("100")  # small variation per case, never changes the shape
        u = (Decimal(unit) + (jitter if cat in ("medicine", "investigation") else Decimal(0))).quantize(CENT)
        out.append(make_line(i, category=cat, desc=desc, qty=qty, unit=str(u)))
    return out


def pick_member(master: dict[str, Any], idx: int, product: str, *, fresh_cover: bool = False) -> tuple[dict[str, Any], dict[str, Any]]:
    pid = next(p["id"] for p in master["products"] if p["code"] == product)
    pols = {p["id"]: p for p in master["policies"] if p["product_id"] == pid and p["status"] == "active" and p["sum_insured"] >= 500000 and p["policy_number"] != "POL-NIV-2025-004411"}
    mems = [m for m in master["members"] if m["policy_id"] in pols and not m["pre_existing"]]
    if fresh_cover:  # cover_start == policy start (no seniority) so an early admission is inside the waiting period
        mems = [m for m in mems if m["cover_start"] == pols[m["policy_id"]]["start_date"]]
    m = mems[idx % len(mems)]
    return m, pols[m["policy_id"]]


def calc_input(case: dict[str, Any], member: dict[str, Any], policy: dict[str, Any], product: str, utilised: Decimal) -> dict[str, Any]:
    sys.path.insert(0, str(ROOT / "insurer" / "api"))
    from insurer_app.config.defaults import policy_rules

    sub = case["submission"]
    a = sub["admission"]
    lines = [{"line_ref": ln["line_id"], "category": ln["category"], "mapped_group": (g := "ot_charges" if ln["description"].startswith("OT charges") else GROUP.get(ln["category"], "other")), "procedure_group": case.get("procedure_group") if g in PROC_ATTACHED else None, "days": int(Decimal(ln["qty"])) if g in ("room_rent", "icu") else None, "description": ln["description"], "qty": ln["qty"],
              "unit_price": ln["unit_price"]["amount"], "claimed_amount": ln["amount"]["amount"], "mapping_source": "rule", "source_doc_id": ln["source_doc_id"]} for ln in sub["bill_lines"]]
    import uuid as _uuid

    return {"case_id": str(_uuid.uuid5(_uuid.NAMESPACE_URL, case["case_id"])), "claim_type": sub["claim_type"], "admission_type": a["admission_type"],
            "policy": {"policy_number": policy["policy_number"], "product_code": product, "status": "active", "start_date": policy["start_date"], "end_date": policy["end_date"],
                       "premium_paid_until": policy["premium_paid_until"], "grace_days": policy["grace_days"], "sum_insured": f"{policy['sum_insured']}.00", "bonus_sum": "0.00",
                       "utilised_this_year": f"{utilised:.2f}", "sum_insured_basis": "floater"},
            "member": {"member_id": member["member_id"], "relationship": member["relationship"], "dob": member["dob"], "cover_start": member["cover_start"], "pre_existing": []},
            "admission": {"admitted_on": a["admitted_on"], "discharged_on": a["discharged_on"], "admission_type": a["admission_type"], "diagnosis_codes": a["diagnosis_codes"],
                          "procedure_codes": a["procedure_codes"], "procedure_group": case.get("procedure_group"), "day_care": False, "hospital": {"hospital_id": a["hospital_id"], "network_status": "network", "room_rent_tier": None}},
            "lines": lines, "rules": base_rules(**policy_rules(product)["rules"]), "rules_version": 1}


def build_case(arch_id: str, idx: int, seed: int, master: dict[str, Any], prev: dict[str, Any] | None = None) -> dict[str, Any]:
    spec = ARCH[arch_id]
    rng = random.Random(int(hashlib.sha256(f"{seed}|{idx}".encode()).hexdigest()[:8], 16))
    product = spec.get("product", "HEALTH-PLUS-GOLD")
    member, policy = pick_member(master, idx, product, fresh_cover=bool(spec.get("waiting")))
    pstart = date.fromisoformat(policy["start_date"])
    proc = PROCEDURES[spec.get("proc", rng.randrange(len(PROCEDURES)) if spec["profile"] not in ("implant",) else 3)]
    los = 2 if spec["profile"] == "small" else proc[3]
    admitted = date(2026, 9, 1) + timedelta(days=(idx % 24) * 1)
    if spec.get("waiting"):
        admitted = pstart + timedelta(days=10)
    if spec.get("expired"):
        admitted = pstart - timedelta(days=20)  # before the cover period (a past admission outside the policy window)
    if spec.get("late_filing"):
        admitted = date(2026, 6, 1)
    discharged = admitted + timedelta(days=los)
    name = member["full_name"] if not spec.get("name_swap") else "Sunita Deshpande"
    dx = [spec["dx"]] if spec.get("dx") else [proc[1]]
    lines = lines_for(spec["profile"], rng, policy["sum_insured"])
    room_days = sum(int(Decimal(x["qty"])) for x in lines if x["category"] == "room")
    if room_days:  # the stay is as long as the billed room days, so day counts agree everywhere
        discharged = admitted + timedelta(days=room_days)
    docs = ["discharge_summary", "final_bill", "itemised_bill", "claim_form", "id_proof", "policy_card"] + spec.get("extra_docs", [])
    if spec["profile"] == "implant" or any(x["category"] == "implant" for x in lines):
        docs.append("implant_sticker")
    if arch_id == "S06":
        docs.remove("implant_sticker")
    docs = [d for d in docs if d not in spec.get("drop_docs", [])]
    discounts = "0.00"
    hospital = f"HOSP-{(idx % 8) + 1:04d}"
    sub = make_submission(claim_ref=f"HC-2026-{idx:06d}", claim_type=spec.get("claim_type", "cashless"), admission_type=spec.get("admission", "planned"), hospital_id=hospital,
                          member_id=member["member_id"], policy_number=policy["policy_number"], full_name=name, dob=member["dob"], gender=member["gender"],
                          admitted_on=admitted.isoformat(), discharged_on=discharged.isoformat(), diagnosis_codes=dx, lines=lines, doc_types=docs, discounts=discounts,
                          submitted_at=f"{discharged.isoformat()}T16:42:10Z", doc_base=idx * 64, procedure_codes=[proc[2]] if proc[2] else [], preauth_ref=None if spec.get("claim_type") == "reimbursement" else f"PA-2026-{idx:05d}")
    if spec.get("duplicate_of") and prev is not None:  # same member + hospital + overlapping stay as the previous case
        pa = prev["submission"]
        sub = make_submission(claim_ref=f"HC-2026-{idx:06d}", hospital_id=pa["admission"]["hospital_id"], member_id=pa["patient"]["member_id"], policy_number=pa["patient"]["policy_number"],
                              full_name=pa["patient"]["full_name"], dob=pa["patient"]["dob"], gender=pa["patient"]["gender"], admitted_on=pa["admission"]["admitted_on"], discharged_on=pa["admission"]["discharged_on"],
                              diagnosis_codes=pa["admission"]["diagnosis_codes"], lines=lines, doc_types=docs, doc_base=idx * 64, submitted_at=pa["submitted_at"])
    bill_doc = next((d["doc_id"] for d in sub["documents"] if d["doc_type"] in ("itemised_bill", "final_bill")), sub["documents"][0]["doc_id"])
    for ln in sub["bill_lines"]:
        ln["source_doc_id"] = bill_doc
    utilised = Decimal(0)
    if spec.get("utilised_frac"):
        utilised = (Decimal(policy["sum_insured"]) * Decimal(spec["utilised_frac"])).quantize(CENT)
    if spec.get("total_mismatch"):
        sub["totals"]["claimed"] = money(Decimal(sub["totals"]["claimed"]["amount"]) + Decimal("1000"))
    case: dict[str, Any] = {"schema_version": "1.0", "case_id": f"SYN-{idx:06d}", "seed": seed, "archetype": arch_id, "archetype_name": spec["name"], "submission": sub,
                            "member": {k: member[k] for k in ("member_id", "full_name", "dob", "gender", "relationship", "cover_start")}, "policy": {k: policy[k] for k in ("policy_number", "start_date", "end_date", "sum_insured", "premium_paid_until")},
                            "product": product, "utilised_prior": f"{utilised:.2f}", "applies_to": spec.get("applies", ["insurer"]),
                            "labels": {"stamp_present": spec.get("stamp", True), "handwritten": bool(spec.get("handwritten")), "degrade": spec.get("degrade"), "expected_quality_class": spec.get("quality", "good"),
                                       "tamper": spec.get("tamper"), "pii_in_free_text": bool(spec.get("pii_free_text")), "late_filing": bool(spec.get("late_filing"))},
                            "scenario": spec.get("scenario", "happy_path"), "procedure_group": proc[5] if not spec.get("dx") else None}
    ref = reference(calc_input(case, member, policy, product, utilised))
    payable = (Decimal(ref["payable"].numerator) / Decimal(ref["payable"].denominator)).quantize(CENT, ROUND_HALF_UP)
    gate_amount = max(Decimal(sub["totals"]["claimed"]["amount"]), payable)
    clean = not spec.get("codes") and spec.get("route") != "none"
    route = spec.get("route")
    if route in ("auto", "reviewer") and clean and arch_id not in ("S04", "S11", "S12", "S16", "S17"):
        route = "auto" if gate_amount <= T_AUTO else ("single_approver" if gate_amount <= T_FOUR else "dual_approver")
    if arch_id in ("S11", "S12", "S16"):
        route = "single_approver" if gate_amount <= T_FOUR else "dual_approver"
    if arch_id in ("S22", "S24") and gate_amount <= T_AUTO:
        route = "auto"  # keep the label honest if the jittered amount ever lands under the auto limit
    case["expected"] = {"insurer": {"status": spec.get("status", "ready_for_decision"), "finding_codes_include": spec.get("codes", []), "route": route if not spec.get("rejected_at_door") else "none",
                                     "rejected_at_door": bool(spec.get("rejected_at_door")), "payable": f"{payable:.2f}", "blocked": ref.get("blocked"),
                                     "gate_amount": f"{gate_amount:.2f}", "reference_flags": sorted(str(f) for f in ref.get("flags", []))},
                        "hospital": {"complete": not spec.get("drop_docs") and arch_id not in ("S06", "S08"), "missing": sorted(set(spec.get("drop_docs", [])) | ({"implant_sticker"} if arch_id == "S06" else set()))}}
    case["documents"] = [{"doc_id": d["doc_id"], "doc_type": d["doc_type"], "sha256": d["sha256"], "size_bytes": d["size_bytes"]} for d in sub["documents"]]
    return case


def write_case(case: dict[str, Any], out: Path) -> None:
    d = out / case["case_id"]
    (d / "docs").mkdir(parents=True, exist_ok=True)
    for doc in case["submission"]["documents"]:
        data = pdf_bytes(doc["doc_type"], doc_lines(doc["doc_type"], case) + (["Call me on 98xxxx1234"] if case["labels"]["pii_in_free_text"] else []))
        (d / "docs" / f"{doc['doc_type']}_{doc['doc_id'][-4:]}.pdf").write_bytes(data)
        doc["sha256"], doc["size_bytes"] = hashlib.sha256(data).hexdigest(), len(data)
    for dd in case["documents"]:
        s = next(x for x in case["submission"]["documents"] if x["doc_id"] == dd["doc_id"])
        dd["sha256"], dd["size_bytes"] = s["sha256"], s["size_bytes"]
    (d / "case.json").write_text(json.dumps(case, indent=1, sort_keys=True), encoding="utf-8")
    manifest = {"generator_version": GENERATOR_VERSION, "files": {str(p.relative_to(d)).replace("\\", "/"): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(d.rglob("*")) if p.is_file() and p.name != "manifest.json"}}
    (d / "manifest.json").write_text(json.dumps(manifest, indent=1, sort_keys=True), encoding="utf-8")


MIX = [("S01", 0.14), ("S02", 0.13), ("S03", 0.13)]  # 40% clean
SINGLE = ["S04", "S05", "S06", "S07", "S08", "S09", "S10", "S11", "S12", "S13", "S14", "S15", "S16", "S21", "S22", "S23", "S24"]  # ~35% single defect
ADVERSARIAL = ["S17", "S18", "S25"]
QUERY = ["S19", "S20"]


def archetype_for(i: int, rng: random.Random) -> str:
    r = rng.random()
    if r < 0.40:
        return rng.choice(["S01", "S02", "S03"])
    if r < 0.75:
        return rng.choice(SINGLE)
    if r < 0.90:
        return rng.choice(QUERY + ["S09", "S11", "S13"])
    return rng.choice(ADVERSARIAL)


def build_corpus(n: int, seed: int) -> list[dict[str, Any]]:
    master = build_master()
    rng = random.Random(seed)
    cases: list[dict[str, Any]] = []
    for i in range(1, n + 1):
        a = archetype_for(i, rng)
        prev = cases[-1] if a == "S18" and cases else None
        if a == "S18" and prev is None:
            a = "S01"
        cases.append(build_case(a, i, seed, master, prev))
    return cases


def build_golden(seed: int = 42) -> list[dict[str, Any]]:
    """One case per archetype S01-S25 (S18 follows a base case), indexes 9001+: stable regardless of corpus size."""
    master = build_master()
    out: list[dict[str, Any]] = []
    for k, a in enumerate(sorted(ARCH), start=0):
        idx = 9001 + k
        prev = out[-1] if a == "S18" else None
        if a == "S18":
            prev = build_case("S01", idx - 1000, seed, master)
        out.append(build_case(a, idx, seed, master, prev))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=500)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--out", default=str(ROOT / "data" / "synthetic" / "out"))
    ap.add_argument("--golden", action="store_true")
    a = ap.parse_args()
    out = Path(a.out)
    if a.golden:
        out = ROOT / "data" / "synthetic" / "golden"
        cases = build_golden(a.seed)
    else:
        cases = build_corpus(a.n, a.seed)
    for c in cases:
        write_case(c, out)
    summary = {"generator_version": GENERATOR_VERSION, "seed": a.seed, "count": len(cases), "by_archetype": {}}
    for c in cases:
        summary["by_archetype"][c["archetype"]] = summary["by_archetype"].get(c["archetype"], 0) + 1
    (out / "corpus_manifest.json").write_text(json.dumps(summary, indent=1, sort_keys=True), encoding="utf-8")
    print(json.dumps(summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
