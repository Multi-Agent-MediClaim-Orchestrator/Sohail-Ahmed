"""Deterministic sample payloads shared by tests, tpa-sim, hospital-sim and the eval harness.

Builders return JSON-shaped dicts (strings for money/dates/UUIDs) so they can be signed as-is and parsed with
``ClaimSubmission.model_validate_json``."""

from __future__ import annotations

import copy
import hashlib
import uuid
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

CENT = Decimal("0.01")


def _uuid(n: int) -> str:
    return str(uuid.UUID(int=(0x0199A1B2_0000_7000_8000_000000000000 | n)))


def money(x: Decimal | str | int) -> dict[str, str]:
    return {
        "amount": f"{Decimal(str(x)).quantize(CENT, rounding=ROUND_HALF_UP):.2f}",
        "currency": "INR",
    }


def make_line(
    i: int,
    *,
    category: str = "room",
    desc: str | None = None,
    qty: str = "1",
    unit: str = "1000.00",
    doc_id: str | None = None,
) -> dict[str, Any]:
    amount = (Decimal(qty) * Decimal(unit)).quantize(CENT, rounding=ROUND_HALF_UP)
    return {
        "line_id": f"L{i:03d}",
        "code": f"ITM-{i:04d}",
        "description": desc or f"{category.title()} item {i}",
        "category": category,
        "qty": qty,
        "unit_price": money(unit),
        "amount": money(amount),
        "source_doc_id": doc_id or _uuid(0x0A),
        "source_page": 1 + (i % 3),
    }


def doc_content(doc_id: str, doc_type: str) -> bytes:
    """Deterministic, per-document bytes: the sha256/size a sample claims is derived from these (unique per doc id)."""
    return f"synthetic-doc-{doc_id}-{doc_type}".encode()


def make_document(
    n: int,
    doc_type: str = "final_bill",
    *,
    host: str = "minio:9000",
    content: bytes | None = None,
    base: int = 0,
) -> dict[str, Any]:
    content = content if content is not None else doc_content(_uuid(base + 0x0A + n), doc_type)
    return {
        "doc_id": _uuid(base + 0x0A + n),
        "doc_type": doc_type,
        "filename": f"{doc_type}.pdf",
        "sha256": hashlib.sha256(content).hexdigest(),
        "size_bytes": len(content),
        "mime_type": "application/pdf",
        "download_url": f"http://{host}/hospital-docs/{_uuid(base + 0x0A + n)}/{doc_type}.pdf?X-Amz-Expires=86400&X-Amz-Signature=abc123",
        "parse_confidence": 0.93,
        "pages": 3,
        "received_via": "upload",
    }


DEFAULT_DOC_TYPES = [
    "discharge_summary",
    "final_bill",
    "itemised_bill",
    "claim_form",
    "id_proof",
    "policy_card",
]


def make_submission(
    *,
    claim_ref: str = "HC-2026-000001",
    claim_type: str = "cashless",
    admission_type: str = "planned",
    hospital_id: str = "HOSP-0001",
    member_id: str = "MEM-77120345",
    policy_number: str = "POL-NIV-2025-004411",
    full_name: str = "Asha Verma",
    dob: str = "1984-03-12",
    gender: str = "F",
    admitted_on: str = "2026-09-28",
    discharged_on: str = "2026-10-01",
    diagnosis_codes: list[str] | None = None,
    lines: list[dict[str, Any]] | None = None,
    doc_types: list[str] | None = None,
    discounts: str = "0.00",
    journey_id: str | None = None,
    preauth_ref: str | None = "PA-2026-33121",
    submitted_at: str = "2026-10-01T16:42:10Z",
    doc_base: int = 0,
    procedure_codes: list[str] | None = None,
) -> dict[str, Any]:
    doc_types = doc_types or DEFAULT_DOC_TYPES
    docs = [make_document(i, t, base=doc_base) for i, t in enumerate(doc_types)]
    src = docs[1]["doc_id"] if len(docs) > 1 else docs[0]["doc_id"]
    if lines is None:
        lines = [
            make_line(
                1,
                category="room",
                desc="Room rent (semi-private) x3",
                qty="3",
                unit="4000.00",
                doc_id=src,
            ),
            make_line(
                2, category="surgery", desc="Surgeon fee", qty="1", unit="40000.00", doc_id=src
            ),
            make_line(
                3, category="medicine", desc="Medicines", qty="1", unit="18000.00", doc_id=src
            ),
            make_line(
                4,
                category="investigation",
                desc="Investigations",
                qty="1",
                unit="6500.00",
                doc_id=src,
            ),
        ]
    gross = sum((Decimal(ln["amount"]["amount"]) for ln in lines), Decimal("0.00"))
    claimed = gross - Decimal(discounts)
    adm: dict[str, Any] = {
        "admission_type": admission_type,
        "admitted_on": admitted_on,
        "discharged_on": discharged_on,
        "diagnosis_codes": diagnosis_codes or ["K80.2"],
        "procedure_codes": procedure_codes
        if procedure_codes is not None
        else (["0FT44ZZ"] if diagnosis_codes is None else []),
        "treating_doctor": "Dr. R. Menon",
        "hospital_id": hospital_id,
    }
    if preauth_ref:
        adm["preauth_ref"] = preauth_ref
    sub: dict[str, Any] = {
        "contract_version": "1.0",
        "claim_ref": claim_ref,
        "claim_type": claim_type,
        "patient": {
            "full_name": full_name,
            "dob": dob,
            "gender": gender,
            "member_id": member_id,
            "policy_number": policy_number,
            "id_proof_hash": hashlib.sha256(f"synthetic-salt|ID-{member_id}".encode()).hexdigest(),
        },
        "admission": adm,
        "bill_lines": lines,
        "totals": {"gross": money(gross), "discounts": money(discounts), "claimed": money(claimed)},
        "documents": docs,
        "config_versions": {
            "doc_requirements": 4,
            "deadlines": 2,
            "router_rules": 3,
            "confidence_gates": 1,
        },
        "submitted_at": submitted_at,
    }
    if journey_id:
        sub["journey_id"] = journey_id
    return sub


def clone(d: dict[str, Any]) -> dict[str, Any]:
    return copy.deepcopy(d)
