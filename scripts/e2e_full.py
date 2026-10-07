"""Full-system end-to-end run: hospital stack + REAL insurer stack (no simulator on the insurer side).

    make e2e-full            # deterministic extractors (rules mode), about 1-2 minutes

A synthetic case is generated for a member who exists in the insurer's seeded data, pushed through the hospital pipeline
(upload, parse, completeness, claim build, sign-off, submit), received and verified by insurer-api, decided (auto-approved
when the gate allows it), calculated by calc-engine, reported back to the hospital by signed callbacks, settled through
the bank simulator, and both audit chains are verified. Scenarios chosen with E2E_SCENARIO (default `auto`)."""

from __future__ import annotations

import asyncio
import dataclasses
import datetime as dt
import json
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
import check_insurer_crew as CC  # noqa: E402
import e2e_hospital as H  # noqa: E402

ROOT, E, LOGS = H.ROOT, H.E, H.LOGS
INS = "http://localhost:8600"
TPA = "http://localhost:8500"
SCENARIO = os.environ.get("E2E_SCENARIO", "auto")
CREW = os.environ.get(
    "E2E_CREW", "off"
)  # off: no insurer crew (rules only); ollama: real agents on the local model
RAG_URL = CC.RAG_URL
ORCH = os.environ.get(
    "E2E_ORCH", "inline"
)  # inline: pipeline runs inside insurer-api; n8n: the insurer n8n flows sequence it


def ins_env() -> dict[str, str]:
    port = E.get("INS_DB_PORT", "5453")
    return {
        "INS_DATABASE_URL": f"postgresql+asyncpg://ins_app:{E['INS_APP_PASSWORD']}@localhost:{port}/insurer",
        "INS_OWNER_DATABASE_URL": f"postgresql://postgres:{E['INS_PG_SUPERUSER_PASSWORD']}@localhost:{port}/insurer",
        "INS_REDIS_URL": f"redis://ins_app:{E['INS_REDIS_PW']}@localhost:{E.get('SHARED_REDIS_PORT', '6379')}/0",
        "INS_EVENTS_STREAM": "sse:insurer:events",
        "INS_HMAC_SECRETS": '{"hosp-001": ["%s"], "bank-sim": ["dev-bank-sim-callback-secret-00000000000"]}'
        % E["HOSP_TO_INS_HMAC_SECRET"],
        "INS_INS_TO_HOSP_HMAC_SECRET": E["INS_TO_HOSP_HMAC_SECRET"],
        "INS_HOSPITAL_CALLBACK_BASE": H.API,
        "INS_ALLOWED_DOC_HOSTS": f"localhost:{E.get('SHARED_MINIO_PORT', '9000')}",
        "INS_TPA_SIM_URL": TPA,
        "INS_SETTLEMENT_MODE": "sim",
        "INS_SETTLEMENT_RETRY_BASE_MINUTES": "0",  # retry a failed payout on the next dispatcher pass instead of after 5 minutes
        "INS_ORCHESTRATOR": ORCH,
        "INS_N8N_URL": f"http://localhost:{E.get('INS_N8N_PORT', '5689')}",
        "INS_N8N_WEBHOOK_SECRET": E["INS_N8N_WEBHOOK_SECRET"],
        "INS_KEYCLOAK_JWKS_URL": "http://localhost:8080/realms/insurer/protocol/openid-connect/certs",
        "INS_KEYCLOAK_ISSUER": "http://localhost:8080/realms/insurer",
        "INS_KEYCLOAK_AUDIENCE": "insurer-api",
        "INS_ALLOW_DEV_TOKENS": "true",  # the script signs its own reviewer tokens; n8n and the UI use real Keycloak tokens
        "INS_CREW_URL": "http://localhost:8610" if CREW == "ollama" else "",
        "INS_N8N_SERVICE_TOKEN": E["INS_CREW_SERVICE_TOKEN"],
        "INS_CALC_ENGINE_URL": "inprocess",
    }


def db() -> Any:
    import psycopg2

    return psycopg2.connect(ins_env()["INS_OWNER_DATABASE_URL"])


def q(sql: str, *args: Any) -> list[tuple[Any, ...]]:
    with db() as conn, conn.cursor() as cur:
        cur.execute(sql, args)
        return list(cur.fetchall()) if cur.description else []


def pick_member() -> dict[str, Any]:
    """An active, long-standing member whose policy has the most cover left (waiting periods are not the story of this test)."""
    rows = q(
        "SELECT m.member_id, m.full_name, m.dob, m.gender, p.policy_number FROM core.policy_member m "
        "JOIN core.policy p ON p.id = m.policy_id WHERE p.start_date <= DATE '2026-01-15' AND p.end_date >= DATE '2026-12-31' AND p.status = 'active' "
        "AND coalesce(p.premium_paid_until, p.end_date) >= DATE '2026-12-31' AND cardinality(m.pre_existing) = 0 "
        "AND m.cover_start <= DATE '2026-01-15' AND m.relationship = 'self' "
        # most remaining sum insured first (earlier runs consume it, and a claim near the limit correctly needs a human), then fewest claims
        "ORDER BY p.sum_insured - coalesce((SELECT sum(u.utilised_amount) FROM core.policy_claim_utilisation u WHERE u.policy_id = p.id), 0) DESC, "
        "(SELECT count(*) FROM core.claim_case c WHERE c.member_id = m.id), m.member_id LIMIT 1"
    )
    if not rows:
        raise SystemExit("no suitable member in the insurer seed; run `make seed-insurer`")
    mid, name, dob, gender, pol = rows[0]
    used = q(
        "SELECT count(*) FROM core.claim_case c JOIN core.policy_member m ON m.id = c.member_id WHERE m.member_id = %s",
        mid,
    )[0][0]
    return {
        "member_id": mid,
        "full_name": name,
        "dob": dob.isoformat(),
        "gender": gender,
        "policy_number": pol,
        "_used": int(used),
    }


def ins_token(*roles: str) -> dict[str, str]:
    from insurer_app.security.auth import make_dev_token

    return {"Authorization": "Bearer " + make_dev_token(f"dev-{roles[0]}", list(roles))}


def case_row(ref: str) -> dict[str, Any] | None:
    r = q(
        "SELECT id, insurer_claim_no, status::text, claimed_amount, approved_amount, procedure_group "
        "FROM core.claim_case WHERE hospital_claim_ref = %s",
        ref,
    )
    if not r:
        return None
    return dict(
        zip(("id", "claim_no", "status", "claimed", "approved", "group"), r[0], strict=True)
    )


def tail(name: str, n: int = 25) -> str:
    p = LOGS / f"{name}.log"
    return "\n".join(p.read_text(errors="ignore").splitlines()[-n:]) if p.exists() else "(no log)"


@dataclasses.dataclass(frozen=True)
class Scn:
    name: str
    procedure: str
    scale: float = 1.0  # non-surgical stays only: a surgery needs a procedure bill the synthetic generator does not produce
    kind: str = "approve"  # auto | approve | dual | reject | queries | callbacks | bank_retry
    note: str = ""


SCENARIOS = {
    s.name: s
    for s in (
        Scn(
            "auto",
            "viral_fever",
            1.0,
            "auto",
            "clean small claim: all gates pass and payable <= T_auto, approved without a human",
        ),
        Scn("reviewer", "pneumonia", 2.0, "approve", "above T_auto: one human approves"),
        Scn("dual", "pneumonia", 36.0, "dual", "above T_four: two approvers, one of them senior"),
        Scn(
            "reject",
            "pneumonia",
            2.0,
            "reject",
            "reviewer rejects with a reason code; nothing is settled",
        ),
        Scn(
            "queries",
            "pneumonia",
            2.0,
            "queries",
            "two answered rounds, an unanswered round 3 escalates, a senior decides",
        ),
        Scn(
            "callbacks",
            "viral_fever",
            1.0,
            "callbacks",
            "duplicate and out-of-order insurer callbacks must not change a settled claim",
        ),
        Scn(
            "bank_retry",
            "viral_fever",
            1.0,
            "bank_retry",
            "bank fails the first payout (tpa-sim), the insurer retries and the hospital still ends settled",
        ),
    )
}
IDEM = lambda: {"Idempotency-Key": str(uuid.uuid4())}  # noqa: E731


class Run:
    """One claim travelling through both systems; the helpers raise AssertionError with context on any surprise."""

    def __init__(self, scn: Scn, case: dict[str, Any], out: Path) -> None:
        self.scn, self.case, self.out = scn, case, out
        self.desk, self.officer = H.Live("desk1"), H.Live("officer1")
        self.c = httpx.Client(base_url=H.API, timeout=60)
        self.ic = httpx.Client(base_url=INS, timeout=60)
        self.s: dict[str, Any] = {}

    # -- hospital side
    def status(self) -> str:
        return str(self.c.get(f"/v1/cases/{self.s['id']}", headers=self.officer).json()["status"])

    def hospital_pipeline(self) -> None:
        c, s, case = self.c, self.s, self.case
        with H.step("hospital: create case for an insurer member"):
            m, a = case["member"], case["admission"]
            r = c.post("/v1/cases", headers={**self.desk, **IDEM()}, json={
                "patient": {"uhid": m["uhid"], "full_name": m["full_name"], "dob": m["dob"], "gender": m["gender"], "phone": "+91" + m["phone"]},
                "policy": {"insurer_name": "Acme Health", "policy_number": m["policy_number"], "member_id": m["member_id"]},
                "claim_type": "cashless", "admission_type": "planned", "admitted_on": a["admitted_on"], "discharged_on": a["discharged_on"],
                "preauth_ref": "PA-2026-33001", "treating_doctor": a["treating_doctor"]})  # fmt: skip
            assert r.status_code == 201, r.text
            s["id"], s["ref"] = r.json()["id"], r.json()["claim_ref"]
        with H.step("hospital: upload and parse documents"):
            files = [
                (
                    "files",
                    (Path(d["file"]).name, (self.out / d["file"]).read_bytes(), "application/pdf"),
                )
                for d in case["documents"]
            ]
            r = c.post(
                f"/v1/cases/{s['id']}/documents", headers={**self.desk, **IDEM()}, files=files
            )
            assert r.status_code == 202, r.text

            def parsed() -> bool:
                docs = c.get(f"/v1/cases/{s['id']}/documents", headers=self.desk).json()[
                    "documents"
                ]
                s["docs"] = docs
                return bool(docs) and all(
                    d["parse_status"] in ("parsed", "needs_review", "failed") and d["doc_type"]
                    for d in docs
                )

            H.wait(parsed, H.PARSE_TIMEOUT, "all documents parsed", 3)
            bad = [d["filename"] for d in s["docs"] if d["parse_status"] == "failed"]
            assert not bad, f"parse failed: {bad}"
        with H.step("hospital: checklist complete, officer builds and signs off the claim"):
            H.wait(
                lambda: c.get(f"/v1/cases/{s['id']}/completeness", headers=self.desk).json()[
                    "complete"
                ],
                120,
                "completeness",
                5,
            )
            r = c.post(f"/v1/cases/{s['id']}/claim/build", headers={**self.officer, **IDEM()})
            assert r.status_code == 202, r.text
            H.wait(lambda: self.status() == "ready_for_review", 300, "ready_for_review")
            claim = c.get(f"/v1/cases/{s['id']}/claim", headers=self.officer).json()
            assert not claim["has_errors"], claim["validation"]
            s["claimed"] = claim["payload"]["totals"]["claimed"]
            rc = c.get(f"/v1/cases/{s['id']}", headers=self.officer).json()
            ack = [w["code"] for w in (rc["route"] or {}).get("warnings", []) if w.get("needs_ack")]
            ack += [w["code"] for w in claim["validation"].get("warnings", [])]
            r = c.post(
                f"/v1/cases/{s['id']}/claim/signoff",
                headers={**self.officer, **IDEM()},
                json={
                    "decision": "approved",
                    "comment": "e2e-full",
                    "acknowledged_warnings": sorted(set(ack)),
                },
            )
            assert r.status_code == 200, r.text

    def submit_and_verify(self) -> None:
        c, s = self.c, self.s
        with H.step("hospital submits; insurer receives, acknowledges and verifies"):
            r = c.post(
                f"/v1/cases/{s['id']}/claim/submit", headers={**self.officer, **IDEM()}, json={}
            )
            assert r.status_code == 202, r.text
            H.wait(lambda: case_row(s["ref"]) is not None, 60, "claim received by insurer-api")
            H.wait(
                lambda: (
                    self.status()
                    in ("acknowledged", "approved", "partially_approved", "under_query")
                ),
                60,
                "hospital acknowledged",
            )
            s["ins"] = H.wait(
                lambda: (
                    (cr := case_row(s["ref"]))
                    and cr["status"] not in ("received", "verifying")
                    and cr
                ),
                180,
                "insurer verification finished",
                3,
            )
            print(
                f"    insurer status {s['ins']['status']}, claimed {s['ins']['claimed']}, approved {s['ins']['approved']}"
            )

    # -- insurer side
    def tier(self) -> dict[str, Any]:
        return self.ic.post(
            f"/v1/cases/{self.s['ins']['id']}/decision/recommend",
            headers=ins_token("reviewer", "admin"),
        ).json()  # type: ignore[no-any-return]

    def decide(
        self,
        *,
        outcome: str | None = None,
        reasons: list[str] | None = None,
        expect_tier: str | None = None,
        votes_expected: int | None = None,
    ) -> str:
        cid = self.s["ins"]["id"]
        prev = self.tier()
        rec, gate = prev["recommendation"], prev["gate"]
        assert rec, f"no recommendation: {prev}"
        print(
            f"    recommendation {rec['outcome']} {rec['approved_amount']}; gate {gate['tier']} ({gate['required_approvals']} approval(s))"
        )
        if expect_tier:
            assert gate["tier"] == expect_tier, f"expected tier {expect_tier}, got {gate}"
        body = {"outcome": outcome or rec["outcome"]}
        if body["outcome"] != "reject":
            body["approved_amount"] = rec["approved_amount"]
        if reasons:
            body["reason_codes"] = reasons
        r = self.ic.post(
            f"/v1/cases/{cid}/decision/submit", headers=ins_token("reviewer"), json=body
        )
        assert r.status_code in (200, 202), f"submit {r.status_code} {r.text}"
        voted = 0
        for who in (("approver",), ("senior_reviewer", "approver"), ("approver2", "approver")):
            cur = self.ic.get(
                f"/v1/cases/{cid}/decision", headers=ins_token("reviewer", "admin")
            ).json()
            if not cur.get("task"):
                break
            r = self.ic.post(
                f"/v1/decisions/{cur['task']['decision_id']}/approvals",
                headers=ins_token(*who),
                json={"verdict": "approve"},
            )
            assert r.status_code == 200, f"vote {r.status_code} {r.text}"
            voted += 1
            if votes_expected and voted == 1 and votes_expected > 1:
                still = self.ic.get(
                    f"/v1/cases/{cid}/decision", headers=ins_token("reviewer", "admin")
                ).json()
                assert still.get("task"), "one vote must not finish a dual-approval decision"
        if votes_expected is not None:
            assert voted == votes_expected, f"expected {votes_expected} vote(s), cast {voted}"
        return str(gate["tier"])

    def settled(self) -> None:
        with H.step("settlement via the bank simulator reaches the hospital"):
            H.wait(lambda: self.status() == "settled", 150, "settled at hospital")

    def audit(self) -> None:
        with H.step("audit chains verify on both sides"):
            r = self.ic.get(
                f"/v1/cases/{self.s['ins']['id']}/audit/verify",
                headers=ins_token("reviewer", "admin", "auditor"),
            )
            assert r.status_code == 200 and r.json().get("ok"), r.text
            r = self.c.post(f"/v1/audit/{self.s['id']}/verify", headers=self.officer)
            assert r.status_code == 200 and r.json()["ok"], r.text

    # -- queries (hospital answers through the crew drafting path, like a real desk would)
    def hospital_answers(self, round_no: int) -> None:
        c, s = self.c, self.s
        H.wait(lambda: self.status() == "under_query", 30, "hospital under_query")
        row = H.wait(
            lambda: next(
                (
                    x
                    for st in ("open", "draft_ready")
                    for x in c.get(
                        f"/v1/queries?status={st}&limit=50", headers=self.officer
                    ).json()["items"]
                    if x["case_id"] == s["id"]
                ),
                None,
            ),
            30,
            f"round {round_no} query in the hospital inbox",
        )
        qid = row["id"]
        r = c.post(f"/v1/queries/{qid}/draft", headers={**self.officer, **IDEM()}, json={})
        assert r.status_code == 202, r.text
        H.wait(
            lambda: c.get(f"/v1/queries/{qid}", headers=self.officer).json()["responses"],
            60 if H.FAST else 480,
            "draft from the hospital crew",
            3,
        )
        d = c.get(f"/v1/queries/{qid}", headers=self.officer).json()
        latest = d["responses"][-1]
        body = (
            {"override_note": "Reviewed by hand against the final bill in the e2e run."}
            if latest["status"] == "needs_attention"
            else {}
        )
        r = c.post(f"/v1/queries/{qid}/approve", headers={**self.officer, **IDEM()}, json=body)
        assert r.status_code == 200, r.text
        if not r.json()["approved"]:
            r = c.post(
                f"/v1/queries/{qid}/approve", headers={**H.token("officer2"), **IDEM()}, json={}
            )
            assert r.status_code == 200 and r.json()["approved"], r.text
        r = c.post(f"/v1/queries/{qid}/send", headers={**self.officer, **IDEM()})
        assert r.status_code == 200, r.text
        s.setdefault("answered_queries", []).append(qid)

    def insurer_queries(self) -> list[dict[str, Any]]:
        return self.ic.get(
            f"/v1/cases/{self.s['ins']['id']}/queries", headers=ins_token("reviewer")
        ).json()["items"]  # type: ignore[no-any-return]

    def raise_query(self, text: str) -> str:
        r = self.ic.post(
            f"/v1/cases/{self.s['ins']['id']}/queries",
            headers=ins_token("reviewer"),
            json={"category": "billing_discrepancy", "text": text, "send": True},
        )
        assert r.status_code == 201, f"{r.status_code} {r.text}"
        return str(r.json()["id"])


def scenario_body(run: Run) -> None:  # noqa: C901
    scn, s = run.scn, run.s
    if scn.kind == "auto":
        with H.step("insurer approves by itself (no human); hospital sees the decision"):
            assert s["ins"]["status"] in ("approved", "settled"), (
                f"expected auto-approval, insurer is {s['ins']['status']}"
            )
            humans = q(
                "SELECT count(*) FROM core.approval a JOIN core.decision d ON d.id = a.decision_id WHERE d.case_id = %s",
                s["ins"]["id"],
            )[0][0]
            assert humans == 0, f"auto-approved claims need no human vote, found {humans}"
            H.wait(
                lambda: run.status() in ("approved", "partially_approved", "settled"),
                90,
                "decision at hospital",
            )
        run.settled()
    elif scn.kind in ("approve", "dual"):
        with H.step(f"reviewer path ({scn.kind}): decision and approvals"):
            assert s["ins"]["status"] in ("ready_for_decision", "awaiting_approval"), s["ins"]
            run.decide(
                expect_tier="dual_approver" if scn.kind == "dual" else None,
                votes_expected=2 if scn.kind == "dual" else None,
            )
            H.wait(
                lambda: run.status() in ("approved", "partially_approved", "settled"),
                90,
                "decision at hospital",
            )
        run.settled()
    elif scn.kind == "reject":
        with H.step("reviewer rejects; hospital sees rejected and nothing is paid"):
            run.decide(outcome="reject", reasons=["EXCL_COSMETIC"])
            H.wait(lambda: run.status() == "rejected", 90, "rejected at hospital")
            assert not q("SELECT 1 FROM core.settlement WHERE case_id = %s", s["ins"]["id"]), (
                "a rejected claim must not be settled"
            )
    elif scn.kind == "queries":
        queries_scenario(run)
    elif scn.kind == "callbacks":
        with H.step("claim approved and settled first"):
            H.wait(lambda: run.status() == "settled", 150, "settled at hospital")
        callbacks_scenario(run)
    elif scn.kind == "bank_retry":
        with H.step("bank failed the first payout; the insurer retried; the hospital is settled"):
            H.wait(
                lambda: run.status() == "settled", 180, "settled at hospital after the bank retry"
            )
            attempts = q(
                "SELECT max(attempt_count), count(*) FROM core.settlement s JOIN core.claim_case c ON c.id = s.case_id WHERE c.hospital_claim_ref = %s",
                s["ref"],
            )[0]
            assert attempts[0] and attempts[0] >= 1, (
                f"expected at least one failed attempt, got {attempts}"
            )
    run.audit()


def close_round(run: Run, qid: str) -> None:
    """The round closes when re-verification finds nothing fixable left. If the triage (rules or the crew) did not say the reply
    is sufficient, the reviewer overrules it by hand, exactly as the UI allows (`triage/override`), and the round then closes."""
    item = next(x for x in run.insurer_queries() if x["id"] == qid)
    verdict = ((item.get("response") or {}).get("triage") or {}).get("verdict")
    if item["status"] != "closed" and verdict not in ("sufficient", "partial"):
        print(f"    triage said {verdict!r}; reviewer overrules: the reply explains the charge")
        r = run.ic.post(
            f"/v1/queries/{qid}/triage/override",
            headers=ins_token("reviewer"),
            json={
                "verdict": "sufficient",
                "note": "reviewed the hospital reply by hand: it explains the charge",
            },
        )
        assert r.status_code == 200, f"override {r.status_code} {r.text}"
    H.wait(
        lambda: next(x for x in run.insurer_queries() if x["id"] == qid)["status"] == "closed",
        90,
        "round closed after re-verification",
    )


def queries_scenario(run: Run) -> None:
    s = run.s
    cid = s["ins"]["id"]
    with H.step("round 1: reviewer asks, hospital answers, insurer triages"):
        assert s["ins"]["status"] in ("ready_for_decision", "awaiting_approval"), s["ins"]
        q1 = run.raise_query("Please explain the room rent charge on the final bill.")
        run.hospital_answers(1)
        # the insurer answers, triages (rules) and closes a fully answered round by itself; the claim returns to ready_for_decision
        H.wait(
            lambda: (
                next((x for x in run.insurer_queries() if x["id"] == q1), {}).get("status")
                in ("answered", "closed")
                and next((x for x in run.insurer_queries() if x["id"] == q1), {})
                .get("response", {})
                .get("triage")
            ),
            60,
            "round 1 answered and triaged at the insurer",
        )
        close_round(run, q1)
    with H.step("round 2: second question, answered"):
        q2 = run.raise_query("Please confirm the surgeon fee matches the procedure estimate.")
        run.hospital_answers(2)
        # the insurer answers, triages (rules) and closes a fully answered round by itself; the claim returns to ready_for_decision
        H.wait(
            lambda: (
                next((x for x in run.insurer_queries() if x["id"] == q2), {}).get("status")
                in ("answered", "closed")
                and next((x for x in run.insurer_queries() if x["id"] == q2), {})
                .get("response", {})
                .get("triage")
            ),
            60,
            "round 2 answered and triaged at the insurer",
        )
        close_round(run, q2)
    with H.step("round 3 is not answered in time: the claim escalates to a senior reviewer"):
        q3 = run.raise_query("Please provide the anaesthesia chart for the procedure.")
        H.wait(lambda: run.status() == "under_query", 30, "hospital under_query for round 3")
        force_timeout(cid, 3)
        esc = H.wait(
            lambda: (
                run.ic.get(
                    f"/v1/cases/{cid}/escalation", headers=ins_token("senior_reviewer")
                ).json()
                if run.ic.get(
                    f"/v1/cases/{cid}/escalation", headers=ins_token("senior_reviewer")
                ).status_code
                == 200
                else None
            ),
            30,
            "escalation raised",
        )
        assert esc["status"] == "open" and esc["reason"] == "no_response_round_3", esc
        assert case_row(s["ref"])["status"] == "escalated"  # type: ignore[index]
        r = run.ic.post(
            f"/v1/cases/{cid}/escalation/resolve",
            headers=ins_token("senior_reviewer"),
            json={"action": "decide_now", "note": "decided on the information already supplied"},
        )
        assert r.status_code == 200, f"{r.status_code} {r.text}"
        _ = q3
    with H.step("senior decision, approvals, settlement"):
        H.wait(
            lambda: case_row(s["ref"])["status"] in ("ready_for_decision", "awaiting_approval"),
            30,
            "back to ready_for_decision",
        )  # type: ignore[index]
        run.decide()
        H.wait(
            lambda: run.status() in ("approved", "partially_approved", "settled"),
            90,
            "decision at hospital",
        )
    run.settled()


def force_timeout(case_id: str, round_no: int) -> None:
    """Fire the round deadline now (the insurer's timers run on wall-clock days): same function the SLA sweep calls."""
    os.environ.update(ins_env())
    from insurer_app import db
    from insurer_app.services import queries as ins_queries
    from insurer_app.settings import get_settings

    get_settings.cache_clear()
    db.init_db()

    async def go() -> Any:
        try:
            return await ins_queries.on_timeout(uuid.UUID(case_id), round_no)
        finally:
            await db.dispose_db()

    out = asyncio.run(go())
    print(f"    round {round_no} deadline fired: {out}")


def callbacks_scenario(run: Run) -> None:
    """Replay and reorder signed insurer callbacks against the hospital: it must stay settled and stay consistent."""
    from claim_contract import signing

    s = run.s
    before = len(
        run.c.get(f"/v1/cases/{s['id']}/timeline?limit=200", headers=run.officer).json()["events"]
    )
    ins = case_row(s["ref"]) or {}
    body = {
        "claim_ref": s["ref"], "insurer_claim_no": ins["claim_no"], "status": "verifying", "hospital_visible_status": "acknowledged",
        "sequence": 1, "occurred_at": "2026-01-01T00:00:00Z", "note": "stale replay", "open_query_ids": [],
    }  # fmt: skip
    raw = json.dumps(body, separators=(",", ":"), sort_keys=True).encode()
    path = "/v1/insurer-callbacks/status"

    def send(idem: str) -> httpx.Response:
        h = signing.build_headers(
            E["INS_TO_HOSP_HMAC_SECRET"].encode(),
            "ins-001",
            "POST",
            path,
            raw,
            idem,
            contract_version="1.1",
        )
        return httpx.post(H.API + path, content=raw, headers=h, timeout=30)

    with H.step("stale (out-of-order) status callback is accepted and ignored"):
        r = send(str(uuid.uuid4()))
        assert r.status_code in (200, 202, 204), f"{r.status_code} {r.text}"
        assert run.status() == "settled", f"a stale callback moved the case to {run.status()}"
    with H.step("the same callback replayed with the same idempotency key is a no-op"):
        key = str(uuid.uuid4())
        first, second = send(key), send(key)
        assert first.status_code in (200, 202, 204) and second.status_code in (
            200,
            202,
            204,
            409,
        ), (first.text, second.text)
        assert run.status() == "settled"
    with H.step("a callback with a bad signature is rejected"):
        h = signing.build_headers(
            b"wrong-secret-wrong-secret-wrong-secret",
            "ins-001",
            "POST",
            path,
            raw,
            str(uuid.uuid4()),
            contract_version="1.1",
        )
        r = httpx.post(H.API + path, content=raw, headers=h, timeout=30)
        assert r.status_code == 401, f"{r.status_code} {r.text}"
    after = len(
        run.c.get(f"/v1/cases/{s['id']}/timeline?limit=200", headers=run.officer).json()["events"]
    )
    assert after == before, f"timeline grew from {before} to {after}: duplicates were applied"


def prepare(scn: Scn) -> tuple[dict[str, Any], Path]:
    from synth.archetypes import RECIPES
    from synth.case import build_case

    member = pick_member()
    used = member.pop(
        "_used"
    )  # earlier runs claimed for this member: move the stay forward so it is not a duplicate
    synth = build_case(
        int(os.environ.get("E2E_SEED", "42")),
        int(time.time()) % 100000,
        dataclasses.replace(RECIPES["S01"], procedure=scn.procedure),
        identity=member,
        discharged_on=dt.date(2026, 2, 1) + dt.timedelta(days=8 * used),
        price_scale=scn.scale,
    )
    out = LOGS / f"case_{scn.name}"
    for rel, data in synth["files"].items():
        (out / rel).parent.mkdir(parents=True, exist_ok=True)
        (out / rel).write_bytes(data)
    return synth["case"], out


def run_scenario(scn: Scn) -> bool:
    print(f"\n=== scenario {scn.name}: {scn.note}", flush=True)
    H.steps.clear()
    case, out = prepare(scn)
    run = Run(scn, case, out)
    if scn.kind == "bank_retry":
        httpx.post(
            f"{TPA}/sim/bank/profile", json={"profile": "fail_once_then_pay"}, timeout=10
        ).raise_for_status()
    try:
        run.hospital_pipeline()
        run.submit_and_verify()
        scenario_body(run)
    except Exception as e:  # noqa: BLE001
        print(f"stopped: {type(e).__name__}: {e}")
        print("--- insurer-api log ---\n" + tail("insurer-api"))
        print("--- hospital api log ---\n" + tail("api"))
        H.diagnose(run.c, run.desk, run.s)
        return False
    finally:
        if scn.kind == "bank_retry":
            httpx.post(f"{TPA}/sim/bank/profile", json={"profile": "always_pay"}, timeout=10)
    ok = bool(H.steps) and all(o for _, o, _, _ in H.steps)
    print(f"=== {scn.name}: {'PASSED' if ok else 'FAILED'}  claim {run.s.get('ref')}", flush=True)
    return ok


def serve_for_browser(stack: Any) -> int:
    """Seed claims in distinct states, start the insurer UI, print READY and wait: the Playwright tests drive the UI against them."""
    seeded: dict[str, dict[str, Any]] = {}
    for key, scn_name, extra in (
        ("single", "reviewer", None),
        ("dual", "dual", None),
        ("query", "reviewer", "query"),
        ("settled", "auto", "settle"),
    ):
        scn = SCENARIOS[scn_name]
        print(f"- seeding {key} ({scn.name}) ...", flush=True)
        H.steps.clear()
        case, out = prepare(scn)
        run = Run(scn, case, out)
        run.hospital_pipeline()
        run.submit_and_verify()
        if extra == "query":
            run.raise_query("Please explain the room rent charge on the final bill.")
        if extra == "settle":
            H.wait(lambda run=run: run.status() == "settled", 150, "settled at hospital")
        seeded[key] = {
            "id": run.s["ins"]["id"],
            "claim_no": run.s["ins"]["claim_no"],
            "ref": run.s["ref"],
            "hospital_id": run.s["id"],
        }
        if not all(o for _, o, _, _ in H.steps):
            print(f"seeding {key} failed: {H.steps}")
            return 1
    (LOGS / "ui-seed.json").write_text(json.dumps(seeded, indent=1))
    stack.start(
        "insurer-ui",
        ["npx", "next", "start", "-p", "3600"],
        ROOT / "insurer/ui",
        3600,
        {"INS_API_URL": INS, "INS_ALLOW_DEV_LOGIN": "1"},
        "http://localhost:3600/login",
    )
    print("READY", flush=True)
    while True:  # killed by the test script
        time.sleep(3600)


def main() -> int:
    wanted = os.environ.get("E2E_SCENARIO", "auto")
    names = (
        list(SCENARIOS) if wanted == "all" else [n.strip() for n in wanted.split(",") if n.strip()]
    )
    unknown = [n for n in names if n not in SCENARIOS]
    if unknown:
        print(f"unknown scenario(s) {unknown}; choose from {list(SCENARIOS)} or 'all'")
        return 2
    for need, url in (
        ("n8n", H.N8N + "/healthz"),
        ("keycloak", "http://localhost:8080/realms/hospital"),
        *(
            [
                (
                    "insurer n8n (make up-insurer-n8n)",
                    f"http://localhost:{E.get('INS_N8N_PORT', '5689')}/healthz",
                )
            ]
            if ORCH == "n8n"
            else []
        ),
    ):
        try:
            httpx.get(url, timeout=3).raise_for_status()
        except httpx.HTTPError:
            print(f"{need} is not reachable ({url}). Run `make up-infra` / `make up-n8n` first.")
            return 2
    for target in ("migrate", "migrate-insurer", "seed-insurer"):
        m = H.subprocess.run(["make", "-s", target], cwd=ROOT, capture_output=True, text=True)
        if m.returncode != 0:
            print(
                f"make {target} failed (is `make up-insurer` done?)\n{m.stdout[-500:]}{m.stderr[-500:]}"
            )
            return 2
    H.clear_user_cache()
    stack = H.Stack()
    H.reload_n8n_flows()
    try:
        local = E.get("HOSP_LLM_LOCAL_MODEL", "gemma4:latest")
        stack.start(
            "insurer-api",
            [
                "uv",
                "run",
                "uvicorn",
                "insurer_app.main:create_app",
                "--factory",
                "--port",
                "8600",
                "--log-level",
                "warning",
            ],
            ROOT / "insurer/api",
            8600,
            ins_env(),
            INS + "/v1/ready",
        )
        rag_tok = (
            CC.rag_token()
        )  # the crew grounds coverage in the knowledge base when `make run-rag` is up
        if rag_tok:
            print("    knowledge base: ON (rag-service reachable)", flush=True)
        if (
            CREW == "ollama"
        ):  # real agents on the local model; Ollama's OpenAI-compatible endpoint stands in for the gateway
            model = E.get("INS_CREW_MODEL", "gemma4:latest")
            stack.start(
                "insurer-crew",
                [
                    "uv",
                    "run",
                    "uvicorn",
                    "insurer_crew.main:app",
                    "--port",
                    "8610",
                    "--log-level",
                    "warning",
                ],
                ROOT / "insurer/crew",
                8610,
                {
                    "INS_LLM_GATEWAY_URL": "http://localhost:11434",
                    "INS_LLM_VIRTUAL_KEY": "ollama",
                    "INS_ALIAS_SMART": model,
                    "INS_ALIAS_FAST": model,
                    "INS_ALIAS_FALLBACK": model,
                    "INS_LLM_REASONING_EFFORT": "none",
                    "INS_CREW_SERVICE_TOKENS": E["INS_CREW_SERVICE_TOKEN"],
                    "INS_CREW_REQUEST_TIMEOUT": "240",
                    "INS_RAG_URL": RAG_URL if rag_tok else "",
                    "INS_RAG_TOKEN": rag_tok or "",
                },
                "http://localhost:8610/v1/health",
            )
        stack.start(
            "tpa-sim",
            [
                "uv",
                "run",
                "uvicorn",
                "tpa_sim.main:app",
                "--port",
                "8500",
                "--log-level",
                "warning",
            ],
            ROOT / "services/tpa-sim",
            8500,
            {"TPA_SIM_INS_BASE_URL": INS},
            TPA + "/sim/health",
        )
        stack.start(
            "api",
            ["uv", "run", "uvicorn", "app.asgi:app", "--port", "8100", "--log-level", "warning"],
            ROOT / "hospital/api",
            8100,
            {"HOSP_INSURER_BASE_URL": INS, "HOSP_N8N_WEBHOOK_BASE": H.N8N + "/webhook"},
            H.API + "/v1/ready",
        )
        stack.start(
            "docpipe",
            ["uv", "run", "uvicorn", "docpipe.main:app_factory", "--factory", "--port", "8200"],
            ROOT / "services/doc-pipeline",
            8200,
            {"DOCPIPE_ALLOW_CLOUD": "false", "DOCPIPE_LLM": "rules" if H.FAST else "ollama"},
            H.DOCP + "/v1/health",
        )
        stack.start(
            "vision",
            ["uv", "run", "uvicorn", "vision.main:app_factory", "--factory", "--port", "8300"],
            ROOT / "services/vision-service",
            8300,
            {},
            H.VISION + "/v1/health",
        )
        stack.start(
            "crew",
            ["uv", "run", "uvicorn", "crew.main:app_factory", "--factory", "--port", "8010"],
            ROOT / "hospital/crew",
            8010,
            {"HOSP_LLM_MODEL": local, "CREW_LLM": "rules" if H.FAST else "ollama"},
            H.CREW + "/v1/health",
        )
        if (
            "--serve" in sys.argv
        ):  # keep the stack and the insurer UI up for the browser tests (scripts/ins_ui_e2e.sh)
            return serve_for_browser(stack)
        results = {n: run_scenario(SCENARIOS[n]) for n in names}
    finally:
        stack.stop()
    print("\n" + "\n".join(f"  {'PASS' if ok else 'FAIL'}  {n}" for n, ok in results.items()))
    all_ok = all(results.values())
    print("FULL E2E PASSED" if all_ok else f"FULL E2E FAILED (logs in {LOGS})")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
