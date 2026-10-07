"""Config dry-run (01-03 §7 step 3): replay up to N historical cases against a draft and report what would change.

* ``thresholds``  -> recompute the gate tier of each case's latest recommendation
* ``policy_rules`` -> re-run the calculation engine on each stored ``CalcInput`` with the draft rules
Nothing is written except the summary row in ``config.config_dry_run``."""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any
from uuid import UUID

from calc_engine.engine import run as run_engine
from calc_engine.models import CalcInput
from claim_contract.errors import ProblemError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ..config.schemas import PolicyRulesPayload, Thresholds
from ..ids import uuid7
from .gate import Thresholds as GateTh
from .gate import compute_gate


async def run(s: AsyncSession, domain: str, name: str, version: int, limit: int = 200) -> dict[str, Any]:
    row = (await s.execute(text(
        "SELECT cv.id, cv.payload FROM config.config_version cv JOIN config.config_set cs ON cs.id = cv.config_set_id WHERE cs.domain=:d AND cs.name=:n AND cv.version=:v"),
        {"d": domain, "n": name, "v": version})).one_or_none()
    if row is None:
        raise ProblemError("not_found", "unknown config version", status=404)
    payload = row.payload if isinstance(row.payload, dict) else json.loads(row.payload)
    changed: list[dict[str, Any]] = []
    n = 0
    if domain == "thresholds":
        th = Thresholds.model_validate(payload)
        new = GateTh(Decimal(th.t_auto_inr), Decimal(th.t_four_inr), allow_auto=False)
        rows = (await s.execute(text(
            "SELECT c.id, c.insurer_claim_no, c.claimed_amount, d.approved_amount, d.outcome, d.gate_tier FROM core.claim_case c JOIN LATERAL "
            "(SELECT * FROM core.decision WHERE case_id = c.id AND kind = 'recommendation' ORDER BY created_at DESC LIMIT 1) d ON true ORDER BY c.received_at DESC LIMIT :l"), {"l": limit})).all()
        for r in rows:
            n += 1
            g = compute_gate(r.claimed_amount, r.approved_amount or Decimal("0"), r.outcome, new, [])
            old = r.gate_tier or "unknown"
            if old not in (g.tier, "auto") and old != "unknown":
                changed.append({"claim_no": r.insurer_claim_no, "before": old, "after": g.tier})
    elif domain == "policy_rules":
        pr = PolicyRulesPayload.model_validate(payload)
        rows = (await s.execute(text(
            "SELECT cr.case_id, c.insurer_claim_no, cr.input, cr.payable_amount FROM core.calculation_result cr JOIN core.claim_case c ON c.id = cr.case_id "
            "WHERE cr.created_at = (SELECT max(created_at) FROM core.calculation_result WHERE case_id = cr.case_id) AND cr.input #>> '{policy,product_code}' = :p LIMIT :l"),
            {"p": pr.product_code, "l": limit})).all()
        for r in rows:
            n += 1
            inp = dict(r.input)
            inp["rules"] = json.loads(pr.rules.model_dump_json())
            try:
                res = run_engine(CalcInput.model_validate(inp))
            except Exception as exc:  # a draft that breaks the engine is itself a finding
                changed.append({"claim_no": r.insurer_claim_no, "error": str(exc)[:200]})
                continue
            if res.payable_total != r.payable_amount:
                changed.append({"claim_no": r.insurer_claim_no, "before": str(r.payable_amount), "after": str(res.payable_total)})
    else:
        raise ProblemError("validation_error", f"dry-run is not available for domain {domain!r}", status=422)
    summary = {"cases_replayed": n, "changed": len(changed), "diff": changed[:200]}
    await s.execute(text("INSERT INTO config.config_dry_run (id, version_id, summary) VALUES (:i, :v, CAST(:s AS JSONB))"), {"i": uuid7(), "v": row.id, "s": json.dumps(summary)})
    return summary


_ = UUID
