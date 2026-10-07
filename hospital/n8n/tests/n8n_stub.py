"""Stub of every service the flows call (doc 08 task 7): records calls, answers from a scripted table."""

from __future__ import annotations

import threading
import time
from typing import Any

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse


class Stub:
    def __init__(self, port: int) -> None:
        self.port = port
        self.calls: list[tuple[str, str, Any]] = []
        self.reset()
        self.app = FastAPI()
        self._routes()
        self._server: uvicorn.Server | None = None

    def reset(self) -> None:
        self.calls.clear()
        self.seen: set[str] = set()
        self.scan_status = "clean"
        self.quality: dict[str, Any] = {
            "quality_score": 0.9,
            "flags": [],
            "has_required_stamp": True,
        }
        self.parse_state = "succeeded"  # succeeded | failed | running
        self.triage_state = "succeeded"
        self.draft_ok = True
        self.doc_get_status = 200
        self.reminders: list[dict[str, Any]] = []
        self.query = {
            "id": "q1",
            "case_id": "c1",
            "round": 1,
            "escalation_risk": False,
            "category": "billing_discrepancy",
        }
        self.ack = False
        self.outbox_stalled: list[dict[str, Any]] = []

    def paths(self, *prefixes: str) -> list[str]:
        out = [f"{m} {p}" for m, p, _ in self.calls if not p.startswith("/token")]
        return [x for x in out if not prefixes or any(pref in x for pref in prefixes)]

    def body(self, method: str, path: str) -> Any:
        for m, p, b in self.calls:
            if m == method and p == path:
                return b
        return None

    def _routes(self) -> None:
        a = self.app

        @a.middleware("http")
        async def log(request: Request, call_next: Any) -> Any:
            raw = await request.body()
            try:
                import json

                body = json.loads(raw) if raw else None
            except ValueError:
                body = raw.decode()
            self.calls.append((request.method, request.url.path, body))
            return await call_next(request)

        @a.post("/token")
        async def token() -> dict[str, Any]:
            return {"access_token": "tok", "expires_in": 300}

        @a.post("/v1/internal/idempotency")
        async def idem(request: Request) -> dict[str, bool]:
            b = await request.json()
            k = f"{b['workflow']}:{b['key']}"
            dup = k in self.seen
            self.seen.add(k)
            return {"duplicate": dup}

        @a.get("/v1/internal/documents/{i}")
        async def doc(i: str) -> Any:
            if self.doc_get_status != 200:
                return JSONResponse({"code": "internal_error"}, status_code=self.doc_get_status)
            return {
                "id": i,
                "case_id": "c1",
                "scan_status": self.scan_status,
                "parse_status": "pending",
                "presigned_url": "http://x/y",
            }

        @a.post("/vision/v1/quality")
        async def vq() -> dict[str, Any]:
            return self.quality

        @a.post("/docpipe/v1/parse")
        async def parse() -> dict[str, str]:
            return {"job_id": "pj-1"}

        @a.get("/docpipe/v1/jobs/{i}")
        async def pjob(i: str) -> dict[str, Any]:
            if self.parse_state == "succeeded":
                return {
                    "state": "succeeded",
                    "result": {
                        "doc_type": "final_bill",
                        "classify_confidence": 0.9,
                        "passes": [
                            {"pass_no": 1, "engine": "mineru", "typed_json": {}, "confidence": 0.9},
                            {"pass_no": 2, "engine": "gemma", "typed_json": {}, "confidence": 0.9},
                        ],
                    },
                }
            return {"state": self.parse_state, "error": "boom"}

        @a.post("/crew/v1/jobs/query-triage")
        async def triage() -> Any:
            if self.triage_state == "down":
                return JSONResponse({}, status_code=503)
            return {"job_id": "tj-1"}

        @a.get("/crew/v1/jobs/{i}")
        async def cjob(i: str) -> dict[str, Any]:
            if self.triage_state == "succeeded":
                return {
                    "state": "succeeded",
                    "result": {"action": "clarify", "needs_docs": False, "escalation_risk": False},
                }
            return {"state": "failed", "error": "bad"}

        @a.post("/crew/v1/jobs/query-draft")
        async def cdraft() -> Any:
            return {"job_id": "dj-1"} if self.draft_ok else JSONResponse({}, status_code=503)

        @a.get("/v1/internal/queries/{i}")
        async def getq(i: str) -> dict[str, Any]:
            return self.query

        @a.post("/v1/internal/queries/{i}/triage-result")
        async def trr(i: str) -> dict[str, Any]:
            return {"query_id": i, "triage_source": "crew", "escalation_risk": False}

        @a.get("/v1/internal/cases/{i}/ack-status")
        async def ack(i: str) -> dict[str, Any]:
            return {"acknowledged": self.ack, "status": "submitted"}

        @a.get("/v1/internal/reminders/due")
        async def due() -> dict[str, Any]:
            return {"items": self.reminders}

        @a.get("/v1/internal/outbox/stalled")
        async def stalled() -> dict[str, Any]:
            return {"items": self.outbox_stalled}

        @a.post("/{path:path}")
        async def anything(path: str) -> dict[str, Any]:
            return {"ok": True}

    def start(self) -> None:
        cfg = uvicorn.Config(self.app, host="127.0.0.1", port=self.port, log_level="warning")
        self._server = uvicorn.Server(cfg)
        threading.Thread(target=self._server.run, daemon=True).start()
        for _ in range(50):
            if self._server.started:
                return
            time.sleep(0.1)

    def stop(self) -> None:
        if self._server:
            self._server.should_exit = True
