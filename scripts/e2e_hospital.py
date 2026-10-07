"""Scripted end-to-end run of the hospital side against REAL services and the insurer simulator.

  make e2e-hospital            (needs `make up-infra` and `make up-n8n`; Ollama on :11434 with the local model)

Starts hospital-api (:8100), crew (:8010), doc-pipeline (:8200), vision-service (:8300) and an insurer simulator
(:8500, in this process), then: create case -> upload synthetic documents -> n8n -> vision + doc-pipeline -> completeness
-> claim build (crew) -> sign-off -> submit -> acknowledgement -> query (triage + draft by crew, two approvals when
needed) -> decision -> settlement. Cloud models are OFF (local model only). Exit code 1 if any step fails."""

from __future__ import annotations

import asyncio
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any

import httpx
import uvicorn

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "data" / "synthetic"))
LOGS = ROOT / ".e2e-logs"
API, CREW, DOCP, VISION, SIM, N8N = (
    f"http://localhost:{p}" for p in (8100, 8010, 8200, 8300, 8500, 5688)
)
KC = "http://localhost:8080/realms/hospital/protocol/openid-connect/token"
steps: list[tuple[str, bool, float, str]] = []


def env_file() -> dict[str, str]:
    out = {}
    for ln in (ROOT / ".env").read_text().splitlines():
        if "=" in ln and not ln.startswith("#"):
            k, v = ln.split("=", 1)
            out[k.strip()] = v.strip()
    return out


E = env_file()


def port_free(p: int) -> bool:
    with socket.socket() as s:
        return s.connect_ex(("127.0.0.1", p)) != 0


class Stack:
    def __init__(self) -> None:
        self.procs: list[subprocess.Popen[bytes]] = []
        LOGS.mkdir(exist_ok=True)

    def start(
        self, name: str, cmd: list[str], cwd: Path, port: int, extra: dict[str, str], health: str
    ) -> None:
        if not port_free(port):
            raise SystemExit(f"port {port} for {name} is already in use; stop that process first")
        env = {**os.environ, **E, **extra}
        log = open(LOGS / f"{name}.log", "wb")  # noqa: SIM115
        self.procs.append(
            subprocess.Popen(
                cmd, cwd=cwd, env=env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True
            )
        )  # noqa: S603
        for _ in range(120):
            try:
                if httpx.get(health, timeout=2).status_code < 500:
                    return
            except httpx.HTTPError:
                pass
            time.sleep(1)
        raise SystemExit(f"{name} did not become healthy; see {LOGS / (name + '.log')}")

    def stop(self) -> None:
        for p in self.procs:
            try:
                os.killpg(p.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass


def token(user: str) -> dict[str, str]:
    r = httpx.post(
        KC,
        data={
            "grant_type": "password",
            "client_id": "hospital-dev",
            "username": user,
            "password": E["DEMO_PW"],
        },
        timeout=15,
    )
    r.raise_for_status()
    return {"Authorization": "Bearer " + r.json()["access_token"]}


def step(name: str):  # type: ignore[no-untyped-def]
    class Ctx:
        def __enter__(self) -> None:
            self.t = time.time()
            print(f"- {name} ...", flush=True)

        def __exit__(self, et, ev, tb) -> bool:  # type: ignore[no-untyped-def]
            ok = et is None
            steps.append((name, ok, time.time() - self.t, "" if ok else f"{et.__name__}: {ev}"))
            print(
                f"  {'ok' if ok else 'FAILED'} ({time.time() - self.t:.1f}s) {'' if ok else ev}",
                flush=True,
            )
            return False

    return Ctx()


def wait(cond, timeout: float, what: str, every: float = 2.0) -> Any:  # type: ignore[no-untyped-def]
    end = time.time() + timeout
    last = None
    while time.time() < end:
        last = cond()
        if last:
            return last
        time.sleep(every)
    raise AssertionError(f"timed out after {timeout:.0f}s waiting for {what}")


def run_sim_server(sim: Any) -> uvicorn.Server:
    srv = uvicorn.Server(uvicorn.Config(sim.app, host="127.0.0.1", port=8500, log_level="warning"))
    threading.Thread(target=srv.run, daemon=True).start()
    for _ in range(50):
        if srv.started:
            return srv
        time.sleep(0.1)
    raise SystemExit("insurer simulator did not start")


def main() -> int:
    from claim_contract.testing.insurer_sim import InsurerSim
    from synth.archetypes import RECIPES
    from synth.case import build_case

    for need, url in (
        ("n8n", N8N + "/healthz"),
        ("keycloak", "http://localhost:8080/realms/hospital"),
        ("ollama", "http://localhost:11434/api/tags"),
    ):
        try:
            httpx.get(url, timeout=3).raise_for_status()
        except httpx.HTTPError:
            print(
                f"{need} is not reachable ({url}). Run `make up-infra` / `make up-n8n` and start Ollama first."
            )
            return 2
    stack = Stack()
    synth = build_case(
        int(os.environ.get("E2E_SEED", "42")), int(time.time()) % 100000, RECIPES["S01"]
    )
    case_json = synth["case"]
    out = LOGS / "case"
    for rel, data in synth["files"].items():
        (out / rel).parent.mkdir(parents=True, exist_ok=True)
        (out / rel).write_bytes(data)
    sim = InsurerSim(
        hosp_secrets={E.get("HOSP_KEY_ID", "hosp-001"): E["HOSP_TO_INS_HMAC_SECRET"].encode()},
        callback_secret=E["INS_TO_HOSP_HMAC_SECRET"].encode(),
        callback_key_id="ins-001",
    )
    sim.hospital = httpx.AsyncClient(base_url=API, timeout=30)
    try:
        run_sim_server(sim)
        local = E.get("HOSP_LLM_LOCAL_MODEL", "gemma4:latest")
        stack.start(
            "api",
            ["uv", "run", "uvicorn", "app.asgi:app", "--port", "8100", "--log-level", "warning"],
            ROOT / "hospital/api",
            8100,
            {"HOSP_INSURER_BASE_URL": SIM, "HOSP_N8N_WEBHOOK_BASE": N8N + "/webhook"},
            API + "/v1/health",
        )
        stack.start(
            "docpipe",
            ["uv", "run", "uvicorn", "docpipe.main:app_factory", "--factory", "--port", "8200"],
            ROOT / "services/doc-pipeline",
            8200,
            {"DOCPIPE_ALLOW_CLOUD": "false"},
            DOCP + "/v1/health",
        )
        stack.start(
            "vision",
            ["uv", "run", "uvicorn", "vision.main:app_factory", "--factory", "--port", "8300"],
            ROOT / "services/vision-service",
            8300,
            {},
            VISION + "/v1/health",
        )
        stack.start(
            "crew",
            ["uv", "run", "uvicorn", "crew.main:app_factory", "--factory", "--port", "8010"],
            ROOT / "hospital/crew",
            8010,
            {"HOSP_LLM_MODEL": local},
            CREW + "/v1/health",
        )
        return asyncio.run(scenario(sim, case_json, out, stack))
    finally:
        stack.stop()
        (LOGS / "summary.txt").write_text(
            "\n".join(f"{'ok ' if o else 'FAIL'} {n} ({d:.1f}s) {m}" for n, o, d, m in steps)
        )


async def scenario(sim: Any, case: dict[str, Any], out: Path, stack: Stack) -> int:
    desk, officer = token("desk1"), token("officer1")
    c = httpx.Client(base_url=API, timeout=60)
    idem = lambda: {"Idempotency-Key": str(uuid.uuid4())}  # noqa: E731
    s: dict[str, Any] = {}
    try:
        with step("create case"):
            m, a = case["member"], case["admission"]
            r = c.post("/v1/cases", headers={**desk, **idem()}, json={
                "patient": {"uhid": m["uhid"], "full_name": m["full_name"], "dob": m["dob"], "gender": m["gender"], "phone": "+91" + m["phone"]},
                "policy": {"insurer_name": "Acme Health", "policy_number": m["policy_number"], "member_id": m["member_id"]},
                "claim_type": "cashless", "admission_type": "planned", "admitted_on": a["admitted_on"], "discharged_on": a["discharged_on"],
                "preauth_ref": "PA-2026-33001", "treating_doctor": a["treating_doctor"]})  # fmt: skip
            assert r.status_code == 201, r.text
            s["id"], s["ref"] = r.json()["id"], r.json()["claim_ref"]
        with step("upload documents (virus scan, storage, n8n trigger)"):
            files = [
                ("files", (Path(d["file"]).name, (out / d["file"]).read_bytes(), "application/pdf"))
                for d in case["documents"]
            ]
            r = c.post(f"/v1/cases/{s['id']}/documents", headers={**desk, **idem()}, files=files)
            assert r.status_code == 202, r.text
            assert all(x["status"] == "accepted" for x in r.json()["documents"]), r.json()
        with step("n8n -> vision + doc-pipeline parse every document"):

            def parsed() -> bool:
                docs = c.get(f"/v1/cases/{s['id']}/documents", headers=desk).json()["documents"]
                s["docs"] = docs
                return all(
                    d["parse_status"] in ("parsed", "needs_review", "failed") and d["doc_type"]
                    for d in docs
                )

            wait(parsed, 900, "all documents parsed", 5)
            bad = [d["filename"] for d in s["docs"] if d["parse_status"] == "failed"]
            assert not bad, f"parse failed: {bad}"
        with step("completeness complete"):

            def complete() -> bool:
                s["comp"] = c.get(f"/v1/cases/{s['id']}/completeness", headers=desk).json()
                return bool(s["comp"]["complete"])

            try:
                wait(complete, 120, "completeness", 5)
            except AssertionError:
                items = [
                    (i["requirement"], i["status"], i["message"])
                    for i in s["comp"]["items"]
                    if i["status"] not in ("present_ok", "not_applicable")
                ]
                raise AssertionError(f"checklist incomplete: {items}") from None
        with step("officer builds the claim (crew)"):
            r = c.post(f"/v1/cases/{s['id']}/claim/build", headers={**officer, **idem()})
            assert r.status_code == 202, r.text
            wait(
                lambda: (
                    c.get(f"/v1/cases/{s['id']}", headers=officer).json()["status"]
                    == "ready_for_review"
                ),
                300,
                "ready_for_review",
            )
            claim = c.get(f"/v1/cases/{s['id']}/claim", headers=officer).json()
            assert not claim["has_errors"], claim["validation"]
            s["claimed"] = claim["payload"]["totals"]["claimed"]
        with step("sign-off and submit"):
            rc = c.get(f"/v1/cases/{s['id']}", headers=officer).json()
            ack = [w["code"] for w in (rc["route"] or {}).get("warnings", []) if w.get("needs_ack")]
            claim = c.get(f"/v1/cases/{s['id']}/claim", headers=officer).json()
            ack += [w["code"] for w in claim["validation"].get("warnings", [])]
            r = c.post(
                f"/v1/cases/{s['id']}/claim/signoff",
                headers={**officer, **idem()},
                json={
                    "decision": "approved",
                    "comment": "e2e",
                    "acknowledged_warnings": sorted(set(ack)),
                },
            )
            assert r.status_code == 200, r.text
            r = c.post(f"/v1/cases/{s['id']}/claim/submit", headers={**officer, **idem()}, json={})
            assert r.status_code == 202, r.text
        with step("insurer simulator receives the claim; hospital shows acknowledged"):
            wait(lambda: s["ref"] in sim.claims, 60, "claim at the simulator")
            wait(
                lambda: (
                    c.get(f"/v1/cases/{s['id']}", headers=officer).json()["status"]
                    == "acknowledged"
                ),
                60,
                "acknowledged",
            )
        with step("insurer asks a query; hospital triages and drafts with the crew"):
            q = {
                "query": {
                    "query_id": str(uuid.uuid4()),
                    "round": 1,
                    "category": "billing_discrepancy",
                    "text": "Please explain the room rent charge billed.",
                    "requested_doc_types": [],
                    "due_by": "2030-01-01T00:00:00Z",
                    "status": "open",
                    "raised_by": "adj-1",
                    "grounding": [],
                }
            }
            r = await sim.push(s["ref"], "queries", q)
            assert r.status_code == 204, r.text
            wait(
                lambda: (
                    c.get(f"/v1/cases/{s['id']}", headers=officer).json()["status"] == "under_query"
                ),
                30,
                "under_query",
            )
            row = wait(
                lambda: (
                    next(
                        (
                            x
                            for x in c.get(
                                "/v1/queries?status=open&limit=50", headers=officer
                            ).json()["items"]
                            if x["case_id"] == s["id"]
                        ),
                        None,
                    )
                    or next(
                        (
                            x
                            for x in c.get(
                                "/v1/queries?status=draft_ready&limit=50", headers=officer
                            ).json()["items"]
                            if x["case_id"] == s["id"]
                        ),
                        None,
                    )
                ),
                30,
                "query in the inbox",
            )
            s["qid"] = row["id"]
            r = c.post(f"/v1/queries/{s['qid']}/draft", headers={**officer, **idem()}, json={})
            assert r.status_code == 202, r.text
            wait(
                lambda: c.get(f"/v1/queries/{s['qid']}", headers=officer).json()["responses"],
                300,
                "draft from the crew",
                5,
            )
        with step("officers approve and send the reply"):
            d = c.get(f"/v1/queries/{s['qid']}", headers=officer).json()
            latest = d["responses"][-1]
            if latest["status"] == "needs_attention":
                latest = d["responses"][-1]
            body = (
                {"override_note": "Reviewed by hand against the final bill in the e2e run."}
                if latest["status"] == "needs_attention"
                else {}
            )
            r = c.post(f"/v1/queries/{s['qid']}/approve", headers={**officer, **idem()}, json=body)
            assert r.status_code == 200, r.text
            if not r.json()["approved"]:
                r = c.post(
                    f"/v1/queries/{s['qid']}/approve",
                    headers={**token("officer2"), **idem()},
                    json={},
                )
                assert r.status_code == 200 and r.json()["approved"], r.text
            r = c.post(f"/v1/queries/{s['qid']}/send", headers={**officer, **idem()})
            assert r.status_code == 200, r.text
            wait(lambda: len(sim.responses) >= 1, 60, "reply at the simulator")
        with step("decision then settlement callbacks"):
            r = await sim.push(
                s["ref"],
                "status",
                {
                    "status": "verifying",
                    "hospital_visible_status": "acknowledged",
                    "occurred_at": "2026-10-07T10:00:00Z",
                    "note": None,
                    "open_query_ids": [],
                },
            )
            assert r.status_code == 204, r.text
            dec = {
                "outcome": "approve",
                "approved_amount": {"amount": s["claimed"]},
                "deductions": [],
                "reason_codes": [],
                "reviewer_ids": ["rev-1"],
                "calc_trace_id": str(uuid.uuid4()),
                "policy_version": 6,
                "decided_at": "2026-10-07T11:00:00Z",
            }
            r = await sim.push(s["ref"], "decisions", {"decision": dec})
            assert r.status_code == 204, r.text
            wait(
                lambda: (
                    c.get(f"/v1/cases/{s['id']}", headers=officer).json()["status"] == "approved"
                ),
                30,
                "approved",
            )
            st = {
                "settlement_id": str(uuid.uuid4()),
                "amount": {"amount": s["claimed"]},
                "utr": "UTR" + uuid.uuid4().hex[:10].upper(),
                "paid_on": "2026-10-07",
                "mode": "NEFT",
                "tds": {"amount": "0.00"},
            }
            r = await sim.push(s["ref"], "settlements", {"settlement": st})
            assert r.status_code == 204, r.text
            wait(
                lambda: (
                    c.get(f"/v1/cases/{s['id']}", headers=officer).json()["status"] == "settled"
                ),
                30,
                "settled",
            )
        with step("audit trail is complete"):
            ev = [
                e["to"]
                for e in c.get(f"/v1/cases/{s['id']}/timeline?limit=100", headers=officer).json()[
                    "events"
                ]
            ]
            assert {
                "docs_complete",
                "ready_for_review",
                "submitted",
                "acknowledged",
                "approved",
                "settled",
            } <= set(ev), ev
    except Exception as e:  # noqa: BLE001
        print(f"stopped: {type(e).__name__}: {e}")
    ok = all(o for _, o, _, _ in steps) and len(steps) == 10
    print(
        "\n" + ("E2E PASSED" if ok else f"E2E FAILED (logs in {LOGS})") + f"  claim {s.get('ref')}"
    )
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
