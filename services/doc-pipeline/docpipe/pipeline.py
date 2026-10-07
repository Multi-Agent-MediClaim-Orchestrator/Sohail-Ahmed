"""Orchestration (doc 02 §6.1): render -> classify -> mask -> guard -> pass A (local) -> pass B (cloud, masked,
when the gate says) -> merge -> validate -> gate. Output is the list of passes hospital-api stores."""

from __future__ import annotations

import hashlib
import time
from typing import Any

from docpipe.llm import LLM, LLMUnavailable
from docpipe.schemas.fields import FIELDS, LINE_DOCS, LOCAL_ONLY
from docpipe.settings import PIPELINE_VERSION, Settings
from docpipe.stages import classify, extract, guard, mask, render, tables, validate
from docpipe.stages.numbers import norm, parse_amount, parse_date

CRITICAL = {t: {k for k, (_, c) in f.items() if c} for t, f in FIELDS.items()}


def _same(kind: str, a: Any, b: Any) -> bool:
    if a is None or b is None:
        return a is None and b is None
    if kind == "money":
        return parse_amount(a) == parse_amount(b)
    if kind == "date":
        return parse_date(a) == parse_date(b)
    if kind == "list":
        return {norm(str(x)) for x in a} == {norm(str(x)) for x in b}
    if kind == "id":
        return norm(str(a)).replace(" ", "").replace("-", "") == norm(str(b)).replace(
            " ", ""
        ).replace("-", "")
    return norm(str(a)) == norm(str(b))


async def run(
    raw: bytes,
    s: Settings,
    llm: LLM,
    *,
    doc_type_hint: str | None = None,
    force_second_pass: bool = False,
) -> dict[str, Any]:
    t0 = time.monotonic()
    timings: dict[str, int] = {}
    sha = hashlib.sha256(raw).hexdigest()
    if len(raw) > s.max_bytes:
        raise render.ParseError("file_too_large", "over the size limit")
    rend = render.render(raw, s.parser, s.max_pages, s.textlayer_min_chars)
    timings["parse"] = int((time.monotonic() - t0) * 1000)
    raw_text = "\n\f\n".join(p.text for p in rend.pages)
    parser_conf = round(min((p.conf for p in rend.pages), default=0.0), 3)
    reasons: list[str] = []
    if not raw_text.strip():
        return _result(
            sha,
            rend,
            None,
            0.0,
            [],
            reasons + ["empty_document", "low_classification"],
            timings,
            mask.Masked("", {}, {}),
            parser_conf,
            [],
        )

    dt, dconf, decisive = classify.classify(raw_text, doc_type_hint, s.class_min_conf)
    if not decisive and dt is not None and not (doc_type_hint and dt == doc_type_hint):
        reasons.append("low_classification")
    if dt is None:
        dt, dconf = "other", 0.0
        reasons.append("low_classification")

    id_issues = validate.id_checks(dt, raw_text)  # before masking, locally
    m = mask.mask(raw_text)
    lines, printed_total, printed_discount = (
        tables.extract_lines(raw_text) if dt in LINE_DOCS else ([], None, None)
    )

    passes: list[dict[str, Any]] = []
    a: extract.Extracted | None = None
    t1 = time.monotonic()
    try:
        a = await extract.run(llm, s.model_local, dt, m.text)
    except LLMUnavailable:
        reasons.append("llm_unavailable")
    timings["llm_a"] = int((time.monotonic() - t1) * 1000)

    b: extract.Extracted | None = None
    crit = CRITICAL.get(dt, set())
    # hospital-api needs two passes to compute agreement (and leaves a document "processing" with one), so a second
    # pass always runs when the first produced values: the cloud model on masked text when allowed and safe, else the
    # LOCAL model again with an independent re-reading prompt (weaker independence, recorded in the engine name).
    want_b = bool(crit) and a is not None
    model_b = s.model_local
    cloud_ok = s.allow_cloud and dt not in LOCAL_ONLY
    if want_b and cloud_ok:
        try:
            guard.assert_safe(m.text, m.pii_map)
        except guard.GuardTripped:
            cloud_ok = False
            reasons.append("masking_guard_tripped")
    if want_b:
        model_b = s.model_cloud if cloud_ok else s.model_local
        if not cloud_ok:
            reasons.append("second_pass_local")  # informational
        try:
            t2 = time.monotonic()
            b = await extract.run(llm, model_b, dt, m.text, variant=True)
            timings["llm_b"] = int((time.monotonic() - t2) * 1000)
        except LLMUnavailable:
            reasons.append("llm_unavailable")

    # merge: agreement only on critical fields B produced
    issues = list(a.issues if a else []) + id_issues
    typed_a, typed_b = dict(a.values) if a else {}, dict(b.values) if b else {}
    disagreements = []
    if a and b:
        for k in crit:
            if not _same(FIELDS[dt][k][0], typed_a.get(k), typed_b.get(k)):
                disagreements.append(k)
        if disagreements:
            reasons.append("critical_disagreement")
    if a is None:
        typed_a = {}
    if lines:
        typed_a["lines"] = lines
        typed_b = {**typed_b, "lines": lines} if b else typed_b
        if printed_total is not None and typed_a.get("total") is None:
            typed_a["total"] = format(printed_total, "f")  # printed on the page, found by code
        if printed_discount is not None and typed_a.get("discounts") is None:
            typed_a["discounts"] = format(printed_discount, "f")
    v = validate.validate(dt, typed_a, lines, printed_total)
    issues += v
    if any(
        i["code"]
        in (
            "table_total_mismatch",
            "date_order",
            "amount_format",
            "invalid_ifsc",
            "invalid_id_checksum",
        )
        for i in issues
    ):
        reasons.append("validation_error")
    if parser_conf < s.parser_min_conf:
        reasons.append("low_parser_conf")
    missing = [k for k in crit if typed_a.get(k) is None] if a else []
    if missing and a:
        reasons.append("missing_required")
    # identity documents: the number itself never leaves; only its hash
    if dt == "id_proof":
        nums = [x for x in m.pii_map.items() if x[0].startswith(("<AADHAAR", "<PAN"))]
        if nums:
            typed_a["id_number_sha256"] = hashlib.sha256(
                nums[0][1].replace(" ", "").encode()
            ).hexdigest()
    # unmask locally: values go to the hospital API (same trust zone), never to a cloud model
    typed_a = _unmask(typed_a, m.pii_map)
    typed_b = _unmask(typed_b, m.pii_map)
    ent = {"counts": m.counts}
    passes.append(
        {
            "pass_no": 1,
            "engine": f"{rend.engine}+{s.model_local}",
            "typed_json": typed_a,
            "confidence": parser_conf,
            "entities": ent,
            "duration_ms": timings.get("llm_a", 0),
        }
    )
    if b is not None:
        passes.append(
            {
                "pass_no": 2,
                "engine": f"{rend.engine}+{model_b}",
                "typed_json": {**typed_b},
                "confidence": parser_conf,
                "entities": ent,
                "duration_ms": timings.get("llm_b", 0),
            }
        )
    timings["total"] = int((time.monotonic() - t0) * 1000)
    return _result(
        sha, rend, dt, dconf, passes, sorted(set(reasons)), timings, m, parser_conf, issues
    )


def _unmask(d: dict[str, Any], pii: dict[str, str]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in d.items():
        out[k] = mask.unmask(v, pii) if isinstance(v, str) else v
    return out


def _result(
    sha: str,
    rend: render.Rendered,
    dt: str | None,
    dconf: float,
    passes: list[dict[str, Any]],
    reasons: list[str],
    timings: dict[str, int],
    m: mask.Masked,
    parser_conf: float,
    issues: list[dict[str, str]],
) -> dict[str, Any]:
    return {
        "doc_type": dt, "doc_type_conf": dconf, "pages": len(rend.pages), "parser": rend.engine, "overall_conf": parser_conf,
        "needs_review": bool(set(reasons) - {"masking_guard_tripped", "second_pass_local"}), "review_reasons": reasons, "issues": issues,
        "passes": passes, "entities_masked": [{"type": t.strip("<>").split("_")[0], "count": 1} for t in m.pii_map],
        "pipeline_version": PIPELINE_VERSION, "timings_ms": timings, "source_sha256": sha,
    }  # fmt: skip
