"""Build one case: entities, bill, documents (clean + degraded), labels and the derived hospital expectations."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from decimal import Decimal
from typing import Any

from synth import bill, degrade, ids, render
from synth.archetypes import RECIPES, Recipe
from synth.entities import ANCHOR, CATALOG, Case, make_doctor, make_member
from synth.rng import case_rng, case_seed

GENERATOR_VERSION = "1.0.0"
REQUIRED = [
    "prescription",
    "pharmacy_bill",
    "final_bill",
]  # default required set (user decision); surgery/implant add more


def _s(v: Any) -> Any:
    if isinstance(v, Decimal):
        return format(v, "f")
    if isinstance(v, dt.date):
        return v.isoformat()
    if isinstance(v, dict):
        return {k: _s(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_s(x) for x in v]
    return v


def build_case(global_seed: int, idx: int, recipe: Recipe) -> dict[str, Any]:
    rng = case_rng(global_seed, idx)
    seed = case_seed(global_seed, idx)
    procs = CATALOG["procedures"]
    if recipe.procedure:
        proc = next(p for p in procs if p["code"] == recipe.procedure)
    else:
        proc = rng.choice([p for p in procs if not p["implant"]])
    hosp = rng.choice(CATALOG["hospitals"])
    discharged = ANCHOR - dt.timedelta(days=rng.randrange(20, 300))
    admitted = discharged - dt.timedelta(days=max(1, proc["los"] + rng.randrange(-1, 2)))
    member = make_member(rng, seed, idx)
    case = Case(
        f"SYN-{idx:06d}",
        seed,
        recipe.id,
        recipe.claim_type,
        recipe.admission_type,
        member,
        hosp,
        proc,
        admitted,
        discharged,
        make_doctor(rng, seed),
    )
    case.bill_lines = bill.hospital_lines(rng, proc, admitted, discharged)
    discount = Decimal(rng.choice([0, 0, 250, 500, 1000]))
    case.totals = {k: v for k, v in bill.totals(case.bill_lines, discount).items()}  # type: ignore[misc]
    ph_lines = bill.pharmacy_lines(rng, recipe.pharmacy_lines, admitted)
    ph_total = bill.totals(ph_lines)["gross"]

    present: dict[str, tuple[bytes, dict[str, Any]]] = {}
    free = ""
    if (
        recipe.pii_text
    ):  # identifiers in free text: masking must catch all of them; all are reserved fake ranges
        free = f"Patient phone {ids.fake_phone(rng)}  Aadhaar {member.aadhaar[:4]} {member.aadhaar[4:8]} {member.aadhaar[8:]}\nPAN {member.pan}  email {member.full_name.split()[0].lower()}@example.test"
    present["prescription"] = render.prescription(case, rng.sample(CATALOG["medicines"], 3), free)
    present["pharmacy_bill"] = render.pharmacy_bill(
        case,
        ph_lines,
        ph_total,
        stamp="pharmacy_bill" not in recipe.no_stamp,
        printed_total=ph_total + Decimal("150.00")
        if "pharmacy_bill" in recipe.total_mismatch
        else None,
    )
    present["final_bill"] = render.final_bill(
        case,
        case.bill_lines,
        case.totals,
        stamp="final_bill" not in recipe.no_stamp,
        printed_total=case.totals["gross"] + Decimal("500.00")
        if "final_bill" in recipe.total_mismatch
        else None,
    )
    present["discharge_summary"] = render.discharge_summary(case, stamp=True)
    if proc["implant"]:
        present["implant_sticker"] = render.implant_sticker(case)
    for t in recipe.drop:
        present.pop(t, None)

    docs: list[dict[str, Any]] = []
    files: dict[str, bytes] = {}
    for n, (dtype, (pdf, meta)) in enumerate(present.items(), 1):
        base = f"docs/{n:02d}_{dtype}"
        params = recipe.degrade.get(dtype)
        qc = "good"
        if params:
            imgs = [degrade.apply(i, **params) for i in degrade.rasterise(pdf)]
            files[f"{base}.degraded.pdf"] = degrade.to_pdf(imgs)
            qc = degrade.quality_class(**params)
            main = f"{base}.degraded.pdf"
        else:
            main = f"{base}.pdf"
        files[f"{base}.pdf"] = pdf
        files[f"{base}.labels.json"] = json.dumps(_s(meta), indent=1, sort_keys=True).encode()
        stamped = any(s["kind"] == "hospital_stamp" for s in meta["stamps"])
        docs.append(
            {
                "doc_type": dtype,
                "file": main,
                "clean_file": f"{base}.pdf",
                "labels": f"{base}.labels.json",
                "pages": meta["pages"],
                "quality_class": qc,
                "degrade": params or {},
                "stamp_present": stamped,
            }
        )

    required = (
        list(REQUIRED)
        + (["procedure_bill"] if proc["surgery"] and False else [])
        + (["implant_sticker"] if proc["implant"] else [])
    )
    have = {x["doc_type"] for x in docs}
    usable = {x["doc_type"] for x in docs if x["quality_class"] in ("good", "acceptable")}
    missing = [t for t in required if t not in have]
    unusable = [t for t in required if t in have and t not in usable]
    stamp_missing = [
        x["doc_type"]
        for x in docs
        if x["doc_type"] in ("final_bill", "pharmacy_bill") and not x["stamp_present"]
    ]
    defects = [
        d
        for d in [
            *(f"missing_doc:{t}" for t in recipe.drop),
            *(f"degrade:{t}" for t in recipe.degrade),
            *(f"no_stamp:{t}" for t in recipe.no_stamp),
            *(f"total_mismatch:{t}" for t in recipe.total_mismatch),
            "pii_text" if recipe.pii_text else "",
        ]
        if d
    ]
    case_json = {
        "schema_version": "1.0", "generator_version": GENERATOR_VERSION, "case_id": case.case_id, "seed": seed, "archetype": recipe.id, "archetype_name": recipe.name,
        "claim_type": case.claim_type, "admission_type": case.admission_type,
        "member": {"full_name": member.full_name, "dob": member.dob, "gender": member.gender, "member_id": member.member_id, "policy_number": member.policy_number, "uhid": member.uhid, "phone": member.phone},
        "hospital": {"id": hosp["id"], "name": hosp["name"], "reg_no": hosp["reg_no"]},
        "admission": {"admitted_on": admitted, "discharged_on": discharged, "diagnosis_codes": proc["dx"], "procedure": proc["code"], "treating_doctor": case.doctor},
        "bill_lines": [{k: v for k, v in ln.items()} for ln in case.bill_lines], "totals": case.totals,
        "pii": {"aadhaar": member.aadhaar, "pan": member.pan} if recipe.pii_text else {},
        "documents": docs, "defects_applied": defects,
        "expected": {"hospital": {
            "classification": [{"doc_type": x["doc_type"], "file": x["file"]} for x in docs],
            "completeness": {"complete": not missing and not unusable and not stamp_missing, "missing": missing, "unusable": unusable, "missing_stamp": stamp_missing},
            "validation": {"table_total_mismatch": [t for t in recipe.total_mismatch]},
            "router": {"claim_type": case.claim_type, "admission_type": case.admission_type},
            "claim_build_fields": {"patient.full_name": member.full_name, "totals.gross": case.totals["gross"], "admission.admitted_on": admitted, "admission.discharged_on": discharged},
        }},
    }  # fmt: skip
    blob = json.dumps(_s(case_json), indent=1, sort_keys=True).encode()
    files["case.json"] = blob
    files["manifest.json"] = json.dumps(
        {
            "generator_version": GENERATOR_VERSION,
            "seed": seed,
            "sha256": {k: hashlib.sha256(v).hexdigest() for k, v in sorted(files.items())},
        },
        indent=1,
    ).encode()
    return {"case_id": case.case_id, "recipe": recipe.id, "files": files, "case": _s(case_json)}


def pick_recipe(rng_seed: int, idx: int) -> Recipe:
    rs = list(RECIPES.values())
    r = case_rng(rng_seed, 10_000_000 + idx)
    return r.choices(rs, weights=[x.weight for x in rs])[0]
