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
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
import e2e_hospital as H  # noqa: E402

ROOT, E, LOGS = H.ROOT, H.E, H.LOGS
INS = "http://localhost:8600"
TPA = "http://localhost:8500"
SCENARIO = os.environ.get("E2E_SCENARIO", "auto")
CREW = os.environ.get("E2E_CREW", "off")  # off: no insurer crew (rules only); ollama: real agents on the local model
ORCH = os.environ.get("E2E_ORCH", "inline")  # inline: pipeline runs inside insurer-api; n8n: the insurer n8n flows sequence it


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
    """An active, long-standing member with the fewest claims so far (waiting periods are not the story of this test)."""
    rows = q(
        "SELECT m.member_id, m.full_name, m.dob, m.gender, p.policy_number FROM core.policy_member m "
        "JOIN core.policy p ON p.id = m.policy_id WHERE p.start_date <= DATE '2026-01-15' AND p.end_date >= DATE '2026-12-31' AND p.status = 'active' "
        "AND coalesce(p.premium_paid_until, p.end_date) >= DATE '2026-12-31' AND cardinality(m.pre_existing) = 0 "
        "AND m.cover_start <= DATE '2026-01-15' AND m.relationship = 'self' "
        "ORDER BY (SELECT count(*) FROM core.claim_case c WHERE c.member_id = m.id), m.member_id LIMIT 1"
    )
    if not rows:
        raise SystemExit("no suitable member in the insurer seed; run `make seed-insurer`")
    mid, name, dob, gender, pol = rows[0]
    used = q("SELECT count(*) FROM core.claim_case c JOIN core.policy_member m ON m.id = c.member_id WHERE m.member_id = %s", mid)[0][0]
    return {"member_id": mid, "full_name": name, "dob": dob.isoformat(), "gender": gender, "policy_number": pol, "_used": int(used)}


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
    return dict(zip(("id", "claim_no", "status", "claimed", "approved", "group"), r[0], strict=True))


def tail(name: str, n: int = 25) -> str:
    p = LOGS / f"{name}.log"
    return "\n".join(p.read_text(errors="ignore").splitlines()[-n:]) if p.exists() else "(no log)"


def main() -> int:
    from synth.archetypes import RECIPES
    from synth.case import build_case

    for need, url in (
        ("n8n", H.N8N + "/healthz"),
        ("keycloak", "http://localhost:8080/realms/hospital"),
        *([("insurer n8n (make up-insurer-n8n)", f"http://localhost:{E.get('INS_N8N_PORT', '5689')}/healthz")] if ORCH == "n8n" else []),
    ):
        try:
            httpx.get(url, timeout=3).raise_for_status()
        except httpx.HTTPError:
            print(f"{need} is not reachable ({url}). Run `make up-infra` / `make up-n8n` first.")
            return 2
    for target in ("migrate", "migrate-insurer", "seed-insurer"):
        m = H.subprocess.run(["make", "-s", target], cwd=ROOT, capture_output=True, text=True)
        if m.returncode != 0:
            print(f"make {target} failed (is `make up-insurer` done?)\n{m.stdout[-500:]}{m.stderr[-500:]}")
            return 2
    H.clear_user_cache()
    stack = H.Stack()
    H.reload_n8n_flows()
    member = pick_member()
    # clean, small, non-surgical claim: the happy path must not depend on a random procedure draw
    recipe = dataclasses.replace(RECIPES["S01"], procedure="pneumonia")
    used = member.pop("_used")  # earlier runs claimed for this member: move the stay forward so it is not a duplicate
    discharge = dt.date(2026, 2, 1) + dt.timedelta(days=8 * used)
    synth = build_case(
        int(os.environ.get("E2E_SEED", "42")),
        int(time.time()) % 100000,
        recipe,
        identity=member,
        discharged_on=discharge,
    )
    out = LOGS / "case_full"
    for rel, data in synth["files"].items():
        (out / rel).parent.mkdir(parents=True, exist_ok=True)
        (out / rel).write_bytes(data)
    try:
        local = E.get("HOSP_LLM_LOCAL_MODEL", "gemma4:latest")
        stack.start(
            "insurer-api",
            ["uv", "run", "uvicorn", "insurer_app.main:create_app", "--factory", "--port", "8600", "--log-level", "warning"],
            ROOT / "insurer/api",
            8600,
            ins_env(),
            INS + "/v1/ready",
        )
        if CREW == "ollama":  # real agents on the local model; Ollama's OpenAI-compatible endpoint stands in for the gateway
            model = E.get("INS_CREW_MODEL", "gemma4:latest")
            stack.start(
                "insurer-crew",
                ["uv", "run", "uvicorn", "insurer_crew.main:app", "--port", "8610", "--log-level", "warning"],
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
                },
                "http://localhost:8610/v1/health",
            )
        stack.start(
            "tpa-sim",
            ["uv", "run", "uvicorn", "tpa_sim.main:app", "--port", "8500", "--log-level", "warning"],
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
        return asyncio.run(scenario(synth["case"], out))
    finally:
        stack.stop()


async def scenario(case: dict[str, Any], out: Path) -> int:
    desk, officer = H.Live("desk1"), H.Live("officer1")
    step, wait = H.step, H.wait
    c = httpx.Client(base_url=H.API, timeout=60)
    ic = httpx.Client(base_url=INS, timeout=60)
    idem = lambda: {"Idempotency-Key": str(uuid.uuid4())}  # noqa: E731
    s: dict[str, Any] = {}
    status = lambda: c.get(f"/v1/cases/{s['id']}", headers=officer).json()["status"]  # noqa: E731
    try:
        with step("hospital: create case for an insurer member"):
            m, a = case["member"], case["admission"]
            r = c.post(
                "/v1/cases",
                headers={**desk, **idem()},
                json={
                    "patient": {"uhid": m["uhid"], "full_name": m["full_name"], "dob": m["dob"], "gender": m["gender"], "phone": "+91" + m["phone"]},
                    "policy": {"insurer_name": "Acme Health", "policy_number": m["policy_number"], "member_id": m["member_id"]},
                    "claim_type": "cashless", "admission_type": "planned", "admitted_on": a["admitted_on"], "discharged_on": a["discharged_on"],
                    "preauth_ref": "PA-2026-33001", "treating_doctor": a["treating_doctor"],
                },
            )  # fmt: skip
            assert r.status_code == 201, r.text
            s["id"], s["ref"] = r.json()["id"], r.json()["claim_ref"]
        with step("hospital: upload and parse documents"):
            files = [("files", (Path(d["file"]).name, (out / d["file"]).read_bytes(), "application/pdf")) for d in case["documents"]]
            r = c.post(f"/v1/cases/{s['id']}/documents", headers={**desk, **idem()}, files=files)
            assert r.status_code == 202, r.text

            def parsed() -> bool:
                docs = c.get(f"/v1/cases/{s['id']}/documents", headers=desk).json()["documents"]
                s["docs"] = docs
                return bool(docs) and all(d["parse_status"] in ("parsed", "needs_review", "failed") and d["doc_type"] for d in docs)

            wait(parsed, H.PARSE_TIMEOUT, "all documents parsed", 3)
            bad = [d["filename"] for d in s["docs"] if d["parse_status"] == "failed"]
            assert not bad, f"parse failed: {bad}"
        with step("hospital: checklist complete, officer builds and signs off the claim"):
            wait(lambda: c.get(f"/v1/cases/{s['id']}/completeness", headers=desk).json()["complete"], 120, "completeness", 5)
            r = c.post(f"/v1/cases/{s['id']}/claim/build", headers={**officer, **idem()})
            assert r.status_code == 202, r.text
            wait(lambda: status() == "ready_for_review", 300, "ready_for_review")
            claim = c.get(f"/v1/cases/{s['id']}/claim", headers=officer).json()
            assert not claim["has_errors"], claim["validation"]
            s["claimed"] = claim["payload"]["totals"]["claimed"]
            rc = c.get(f"/v1/cases/{s['id']}", headers=officer).json()
            ack = [w["code"] for w in (rc["route"] or {}).get("warnings", []) if w.get("needs_ack")]
            ack += [w["code"] for w in claim["validation"].get("warnings", [])]
            r = c.post(
                f"/v1/cases/{s['id']}/claim/signoff",
                headers={**officer, **idem()},
                json={"decision": "approved", "comment": "e2e-full", "acknowledged_warnings": sorted(set(ack))},
            )
            assert r.status_code == 200, r.text
        with step("hospital submits; insurer receives, acknowledges and verifies"):
            r = c.post(f"/v1/cases/{s['id']}/claim/submit", headers={**officer, **idem()}, json={})
            assert r.status_code == 202, r.text
            wait(lambda: case_row(s["ref"]) is not None, 60, "claim received by insurer-api")
            wait(lambda: status() in ("acknowledged", "approved", "partially_approved", "under_query"), 60, "hospital acknowledged")
            s["ins"] = wait(lambda: (cr := case_row(s["ref"])) and cr["status"] not in ("received", "verifying") and cr, 180, "insurer verification finished", 3)
            print(f"    insurer status {s['ins']['status']}, claimed {s['ins']['claimed']}, approved {s['ins']['approved']}")
        with step("insurer decision reaches the hospital (auto-approve or reviewer)"):
            if s["ins"]["status"] in ("ready_for_decision", "awaiting_approval"):
                cid = s["ins"]["id"]
                prev = ic.post(f"/v1/cases/{cid}/decision/recommend", headers=ins_token("reviewer", "admin")).json()
                rec = prev["recommendation"]
                assert rec, f"no recommendation: {prev}"
                print(f"    recommendation {rec['outcome']} {rec['approved_amount']}; gate {prev['gate']['tier']}")
                r = ic.post(
                    f"/v1/cases/{cid}/decision/submit",
                    headers=ins_token("reviewer"),
                    json={"outcome": rec["outcome"], "approved_amount": rec["approved_amount"]},
                )
                assert r.status_code in (200, 202), f"submit {r.status_code} {r.text}"
                for who in (("approver",), ("senior_reviewer", "approver"), ("approver2", "approver")):
                    cur = ic.get(f"/v1/cases/{cid}/decision", headers=ins_token("reviewer", "admin")).json()
                    if not cur.get("task"):
                        break
                    r = ic.post(
                        f"/v1/decisions/{cur['task']['decision_id']}/approvals",
                        headers=ins_token(*who),
                        json={"verdict": "approve"},
                    )
                    assert r.status_code == 200, f"vote {r.status_code} {r.text}"
            wait(lambda: status() in ("approved", "partially_approved", "rejected"), 90, "decision at hospital")
            assert status() in ("approved", "partially_approved"), f"hospital status {status()}"
        with step("settlement via the bank simulator reaches the hospital"):
            # approval starts the settlement by itself (inline job); the bank simulator then calls back with the UTR
            wait(lambda: status() == "settled", 120, "settled at hospital")
        with step("audit chains verify on both sides"):
            r = ic.get(f"/v1/cases/{s["ins"]["id"]}/audit/verify", headers=ins_token("reviewer", "admin", "auditor"))
            assert r.status_code == 200 and r.json().get("ok", r.json().get("valid", True)), r.text
            r = c.post(f"/v1/audit/{s['id']}/verify", headers=officer)
            assert r.status_code == 200 and r.json()["ok"], r.text
    except Exception as e:  # noqa: BLE001
        print(f"stopped: {type(e).__name__}: {e}")
        print("--- insurer-api log ---\n" + tail("insurer-api"))
        print("--- hospital api log ---\n" + tail("api"))
        H.diagnose(c, desk, s)
    ok = bool(H.steps) and all(o for _, o, _, _ in H.steps) and len(H.steps) == 7
    print("\n" + ("FULL E2E PASSED" if ok else f"FULL E2E FAILED (logs in {LOGS})") + f"  claim {s.get('ref')}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
