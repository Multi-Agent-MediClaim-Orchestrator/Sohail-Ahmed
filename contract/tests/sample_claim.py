from typing import Any

DOC = "0199a1b2-1111-7000-8000-00000000000a"


def valid_submission() -> dict[str, Any]:
    return {
        "contract_version": "1.0",
        "claim_ref": "HC-2026-000001",
        "claim_type": "cashless",
        "patient": {
            "full_name": "Asha Verma",
            "dob": "1984-03-12",
            "gender": "F",
            "member_id": "MEM-77120345",
            "policy_number": "POL-NIV-2025-004411",
            "id_proof_hash": "9f2c0e6c0a8e3b1d5f4a7c2e9b6d1a0f3c5e7b9d2f4a6c8e0b1d3f5a7c9e2b4d",
        },
        "admission": {
            "admission_type": "planned",
            "admitted_on": "2026-09-28",
            "discharged_on": "2026-10-01",
            "diagnosis_codes": ["K80.2"],
            "procedure_codes": ["0FT44ZZ"],
            "treating_doctor": "Dr. R. Menon",
            "hospital_id": "HOSP-0007",
            "preauth_ref": "PA-2026-33121",
        },
        "bill_lines": [
            {
                "line_id": "L001",
                "code": "RM-GEN",
                "description": "Room rent (semi-private) x3",
                "category": "room",
                "qty": "3",
                "unit_price": {"amount": "4000.00"},
                "amount": {"amount": "12000.00"},
                "service_date": "2026-09-29",
                "source_doc_id": DOC,
                "source_page": 2,
            }
        ],
        "totals": {
            "gross": {"amount": "12000.00"},
            "discounts": {"amount": "0.00"},
            "claimed": {"amount": "12000.00"},
        },
        "documents": [
            {
                "doc_id": DOC,
                "doc_type": "final_bill",
                "filename": "final_bill.pdf",
                "sha256": "ab" * 32,
                "size_bytes": 184233,
                "mime_type": "application/pdf",
                "download_url": "https://minio.local:9000/hospital-docs/x?X-Amz-Expires=86400",
                "url_expires_at": "2026-10-02T16:42:10Z",
                "parse_confidence": 0.93,
                "pages": 3,
            }
        ],
        "config_versions": {
            "doc_requirements": 4,
            "deadlines": 2,
            "router_rules": 3,
            "confidence_gates": 1,
        },
        "submitted_at": "2026-10-01T16:42:10Z",
    }
