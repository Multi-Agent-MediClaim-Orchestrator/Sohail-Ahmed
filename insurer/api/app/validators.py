"""Door validators V1-V14 (03-02 §4.6) beyond the shared Pydantic/``claim_contract.validation`` rules."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from claim_contract.errors import FieldError, ProblemError
from claim_contract.models import ClaimSubmission
from claim_contract.validation import ValidationReport, check_submission
from pydantic import ValidationError

from .models.core import NetworkHospital
from .security.ssrf import UrlNotAllowed, assert_allowed_host, parse_allow_list

MAX_STAY_DAYS = 180
MAX_DOCS = 200
MAX_TOTAL_BYTES = 500 * 1024 * 1024
MAX_DOC_BYTES = 50 * 1024 * 1024
MAX_LINES = 2000


def parse_submission(raw: bytes) -> ClaimSubmission:
    """Pydantic parse of the raw body. Unknown fields -> 422 listing them; major version != 1 -> 400 (V1)."""
    try:
        data = json.loads(raw)
    except (ValueError, UnicodeDecodeError, RecursionError) as exc:  # RecursionError: absurdly nested JSON
        raise ProblemError("bad_request", "malformed JSON") from exc
    ver = str(data.get("contract_version", "")) if isinstance(data, dict) else ""
    if ver and ver.split(".")[0] != "1":
        raise ProblemError("unsupported_version", f"contract_version {ver} is not supported")
    try:
        return ClaimSubmission.model_validate_json(raw)
    except ValidationError as exc:
        errs = [FieldError(field=".".join(str(p) for p in e["loc"]), message=e["msg"]) for e in exc.errors()]
        code = "validation_error"
        raise ProblemError(code, "request body failed validation", errors=errs) from exc


def check_hospital(sub: ClaimSubmission, hospital: NetworkHospital | None, key_id: str) -> NetworkHospital:
    """V9 blacklist/active (403) and V10 hospital_id equals the hospital bound to the key (403)."""
    if hospital is None:
        raise ProblemError("invalid_signature", "signature verification failed")
    if not hospital.active or hospital.network_status == "blacklisted":
        raise ProblemError("hospital_blacklisted", f"Hospital {hospital.hospital_code} is not currently permitted to submit claims")
    if sub.admission.hospital_id != hospital.hospital_code:
        raise ProblemError("hospital_mismatch", "admission.hospital_id does not match the hospital bound to this key")
    return hospital


def check_door_rules(sub: ClaimSubmission, allowed_hosts: str, now: datetime | None = None) -> ValidationReport:
    now = now or datetime.now(UTC)
    report = check_submission(sub, preauth_is_warning=True)  # V2-V4, V6-V9(model-level), V11 (warning at the door)
    errors: list[FieldError] = []
    adm = sub.admission
    if (adm.discharged_on - adm.admitted_on).days > MAX_STAY_DAYS:  # V5
        errors.append(FieldError(field="admission.discharged_on", message=f"stay exceeds {MAX_STAY_DAYS} days"))
    if len(sub.documents) > MAX_DOCS or sum(d.size_bytes for d in sub.documents) > MAX_TOTAL_BYTES:  # V7
        errors.append(FieldError(field="documents", message="too many documents or total size above 500 MB"))
    for i, d in enumerate(sub.documents):
        if d.size_bytes > MAX_DOC_BYTES:
            errors.append(FieldError(field=f"documents[{i}].size_bytes", message="document above 50 MB"))
    if len(sub.bill_lines) > MAX_LINES:  # V12
        errors.append(FieldError(field="bill_lines", message="more than 2000 lines"))
    allow = parse_allow_list(allowed_hosts)
    for i, d in enumerate(sub.documents):  # V13 SSRF guard
        try:
            assert_allowed_host(str(d.download_url), allow)
        except UrlNotAllowed as exc:
            errors.append(FieldError(field=f"documents[{i}].download_url", message=f"url not allowed: {exc}"))
    if errors:
        raise ProblemError("validation_error", errors[0].message, errors=errors)
    return report


def assign_line_ids(sub: ClaimSubmission) -> None:
    for i, bl in enumerate(sub.bill_lines, start=1):
        if bl.line_id is None:
            bl.line_id = f"L{i:03d}"


def priority_for(sub: ClaimSubmission, t_auto: Any) -> int:
    base = 1 if sub.admission.admission_type.value == "emergency" else (2 if sub.claim_type.value == "cashless" else 3)
    if sub.totals.claimed.amount > t_auto:
        base = max(1, base - 1)
    return base
