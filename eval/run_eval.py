"""Evaluation harness (05-02), insurer-side suites that run in-process, offline and deterministically:

  calc        calc_engine vs the independent reference calculator on the synthetic corpus (disagreements are listed, never hidden)
  gate        routing correctness (auto / single / dual) and the "no wrongful auto path" invariant
  mapper      crew keyword mapper vs the generator's category -> group truth
  identity    name-similarity decisions (variation vs mismatch) on generated variants
  rag         retrieval/answer metrics from services/rag-service (offline mode)

    python eval/run_eval.py --n 300 --seed 42 --out eval/runs

Writes run.json, metrics.json, report.md and failures/*.json under eval/runs/<timestamp>/. Suites needing a live stack (parse accuracy, vision,
verification pipeline via insurer-api) are reported as ``not_run`` with the reason, so a green report never implies they passed."""

from __future__ import annotations

import argparse
import importlib.util
import json
import platform
import random
import sys
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
for p in ("insurer/api", "insurer/calc_engine", "insurer/crew", "services/rag-service", "contract/python"):
    sys.path.insert(0, str(ROOT / p))


def load(path: str, name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    assert spec is not None and spec.loader is not None
    m = importlib.util.module_from_spec(spec)
    sys.modules[name] = m
    spec.loader.exec_module(m)
    return m


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (0.0, 0.0)
    p = k / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    r = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5)
    return (round((c - r) / d, 4), round((c + r) / d, 4))


def metric(name: str, k: int, n: int, target: float | None = None, higher_is_better: bool = True) -> dict[str, Any]:
    v = k / n if n else None
    ok = None if target is None or v is None else (v >= target if higher_is_better else v <= target)
    return {"name": name, "value": None if v is None else round(v, 4), "k": k, "n": n, "ci95": wilson(k, n), "target": target, "pass": ok}


# ------------------------------------------------------------------------------------------------ suites
def suite_calc(cases: list[dict[str, Any]], gen: Any) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    from calc_engine.engine import run as calc_run
    from calc_engine.models import CalcInput

    master = gen.build_master()
    agree = total = 0
    fails: list[dict[str, Any]] = []
    for c in cases:
        if c["archetype"] in ("S10",) or c["expected"]["insurer"]["rejected_at_door"]:
            continue
        member = next(m for m in master["members"] if m["member_id"] == c["member"]["member_id"])
        policy = next(p for p in master["policies"] if p["policy_number"] == c["policy"]["policy_number"])
        inp = gen.calc_input(c, member, policy, c["product"], Decimal(c["utilised_prior"]))
        out = calc_run(CalcInput.model_validate(inp))
        got = Decimal(str(out.payable_total))
        want = Decimal(c["expected"]["insurer"]["payable"])
        total += 1
        tol = Decimal("0.01") * len(inp["lines"])
        if abs(got - want) <= tol:
            agree += 1
        else:
            fails.append({"case": c["case_id"], "archetype": c["archetype"], "engine": str(got), "reference": str(want), "flags": [str(f) for f in out.flags]})
    return [metric("calc_agreement_vs_reference", agree, total, 0.995)], fails


def suite_gate(cases: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    from insurer_app.services.gate import AutoFacts, Thresholds, compute_gate

    th = Thresholds(t_auto=Decimal("50000"), t_four=Decimal("500000"))
    ok = n = wrongful = 0
    fails = []
    for c in cases:
        e = c["expected"]["insurer"]
        if e["route"] not in ("auto", "single_approver", "dual_approver") or e["finding_codes_include"]:
            continue
        claimed = Decimal(c["submission"]["totals"]["claimed"]["amount"])
        payable = Decimal(e["payable"])
        facts = AutoFacts(outcome="approve" if payable == claimed else "partial", active_blockers=0, active_warnings=0, identity_score=1.0, manual_verification=False, degraded=False)
        g = compute_gate(claimed, payable, facts.outcome, th, [], facts)
        n += 1
        if g.tier == e["route"]:
            ok += 1
        else:
            fails.append({"case": c["case_id"], "archetype": c["archetype"], "got": g.tier, "want": e["route"], "claimed": str(claimed), "payable": str(payable)})
        if g.tier == "auto" and max(claimed, payable) > th.t_auto:
            wrongful += 1
    # invariant sweep: whatever the amounts, auto is never returned above T_auto and never with a blocker/warning/low identity/degraded run
    import itertools

    sweep_bad = 0
    for amt, blk, wrn, ident, deg in itertools.product(["1", "49999.99", "50000", "50000.01", "500000", "500000.01"], [0, 1], [0, 1], [1.0, 0.5], [False, True]):
        f = AutoFacts("approve", blk, wrn, ident, False, deg)
        g = compute_gate(Decimal(amt), Decimal(amt), "approve", th, [], f)
        if g.tier == "auto" and (Decimal(amt) > th.t_auto or blk or wrn or ident < th.auto_min_identity or deg):
            sweep_bad += 1
        wrongful += 0
    return [metric("decision_route_correctness", ok, n, 1.0), metric("wrongful_auto_path", wrongful + sweep_bad, max(n, 1), 0.0, higher_is_better=False)], fails


def suite_mapper() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    from insurer_crew import tools

    truth = [("Private room 5 days", "room", "room_rent"), ("Surgeon fee", "surgery", "surgeon_fees"), ("Anaesthetist fee", "anaesthesia", "anaesthesia"), ("Knee prosthesis", "other", "implant"),
             ("Coronary stent", "other", "implant"), ("Medicines", "medicine", "medicine"), ("Injection ceftriaxone", "other", "medicine"), ("Gloves pack", "other", "consumable"),
             ("MRI brain", "other", "investigation"), ("Registration fee", "other", "non_medical"), ("Ambulance charges", "other", "ambulance"), ("ICU charges", "other", "icu"),
             ("Nursing care", "other", "nursing"), ("OT charges", "other", "ot_charges"), ("Consultation", "consultation", "doctor_fees")]
    rows = [(d + (f" {i}" if i else ""), c, g) for i in range(0, 14) for d, c, g in truth]
    ok = sum(1 for d, c, g in rows if (m := tools.keyword_map(c, d)) and m["mapped_group"] == g)
    fails = [{"desc": d, "category": c, "want": g} for d, c, g in rows if not ((m := tools.keyword_map(c, d)) and m["mapped_group"] == g)]
    return [metric("mapper_rule_accuracy", ok, len(rows), 0.97)], fails


def suite_identity(seed: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    from insurer_crew import tools

    rng = random.Random(seed)
    first = ["Ravi", "Asha", "Karthik", "Meera", "Suresh", "Priya"]
    last = ["Kumar", "Verma", "Sharma", "Iyer", "Nair", "Reddy"]
    same, diff, fails = 0, 0, []
    n_same = n_diff = 0
    for _ in range(200):
        f, la = rng.choice(first), rng.choice(last)
        member = f"{f} {la}"
        variant = rng.choice([member, f"{f[0]}. {la}", f"{la} {f}", f"Mr. {member}", member.upper(), f"{f} {rng.choice('ABCDEFGHKMNPRSTV')}. {la}"])
        n_same += 1
        if tools.compare_names(variant, member)["score"] >= 0.85:
            same += 1
        else:
            fails.append({"member": member, "variant": variant, "kind": "should_match"})
        other = f"{rng.choice([x for x in first if x != f])} {rng.choice([x for x in last if x != la])}"
        n_diff += 1
        if tools.compare_names(other, member)["score"] < 0.85:
            diff += 1
        else:
            fails.append({"member": member, "variant": other, "kind": "should_mismatch"})
    return [metric("identity_match_recall", same, n_same, 0.98), metric("identity_mismatch_recall", diff, n_diff, 0.95)], fails


def suite_rag() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    from rag_service import corpus
    from rag_service import evaluation as ev

    store, _, emb = ev.build_index()
    rows = ev.resolve_gold(store, corpus.build_qa())
    m = ev.evaluate(store, emb, rows)
    t = {"recall@5": 0.85, "mrr": 0.6, "ndcg@5": 0.7, "citation_precision": 0.9, "insufficient_evidence_accuracy": 0.9, "temporal_trap_accuracy": 0.95, "numeric_faithfulness": 1.0}
    out = [{"name": f"rag_{k}", "value": v, "target": t[k], "pass": v >= t[k] if v is not None else None, "k": None, "n": m["rows"], "ci95": None} for k, v in m.items() if k in t]
    return out, []


NOT_RUN = {
    "doc_parse_accuracy": "needs doc-pipeline (Dev A) and rendered images",
    "vision_stamp_signature": "needs vision-service",
    "pii_masking_recall": "needs Presidio in doc-pipeline",
    "verification_pipeline_e2e": "run `pytest insurer/api/tests/test_golden_e2e.py` (needs Docker/Postgres)",
    "llm_agent_quality": "needs the live gateway (Gemini/Ollama); offline stubs only here",
    "calibration": "needs parser/vision confidences from the live pipeline",
}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=300)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--out", default=str(ROOT / "eval" / "runs"))
    a = ap.parse_args()
    gen = load("data/synthetic/synth/generate.py", "synth_generate_eval")
    cases = gen.build_corpus(a.n, a.seed)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    out = Path(a.out) / stamp
    (out / "failures").mkdir(parents=True, exist_ok=True)
    metrics: list[dict[str, Any]] = []
    for name, fn in [("calc", lambda: suite_calc(cases, gen)), ("gate", lambda: suite_gate(cases)), ("mapper", suite_mapper), ("identity", lambda: suite_identity(a.seed)), ("rag", suite_rag)]:
        try:
            m, f = fn()
            for x in m:
                x["suite"] = name
            metrics += m
            if f:
                (out / "failures" / f"{name}.json").write_text(json.dumps(f, indent=1), encoding="utf-8")
        except Exception as e:  # noqa: BLE001 - a broken suite must show up in the report, not abort the run
            metrics.append({"suite": name, "name": f"{name}_suite_error", "value": None, "pass": False, "error": f"{type(e).__name__}: {e}"})
    (out / "run.json").write_text(json.dumps({"stamp": stamp, "seed": a.seed, "cases": len(cases), "python": platform.python_version(), "generator": gen.GENERATOR_VERSION,
                                              "llm_mode": "offline-stub", "not_run": NOT_RUN}, indent=1), encoding="utf-8")
    (out / "metrics.json").write_text(json.dumps(metrics, indent=1), encoding="utf-8")
    lines = [f"# Evaluation {stamp}", "", f"cases={len(cases)} seed={a.seed} llm_mode=offline-stub", "", "| suite | metric | value | n | 95% CI | target | pass |", "|---|---|---|---|---|---|---|"]
    for row in metrics:
        lines.append(f"| {row.get('suite')} | {row['name']} | {row.get('value')} | {row.get('n')} | {row.get('ci95')} | {row.get('target')} | {row.get('pass')} |")
    lines += ["", "## Not run (and why)", ""] + [f"- **{k}**: {v}" for k, v in NOT_RUN.items()]
    (out / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    return 0 if all(m.get("pass") is not False for m in metrics) else 1


if __name__ == "__main__":
    raise SystemExit(main())
