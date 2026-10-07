"""hospital-sim (03-dev-B / 04-06 §9.6): behaves like the hospital so the insurer can be built without hospital-api.

* submits signed requests to the insurer app (ASGI, no network)
* hosts a callback receiver that verifies signatures/idempotency/sequence and records every callback
* can reject the next N callbacks (to test insurer outbox retry)"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any

import httpx
from claim_contract import signing
from claim_contract.errors import install_handlers
from claim_contract.idempotency import MemoryIdempotencyStore
from claim_contract.inbox import SequenceAction, apply_sequence
from claim_contract.middleware import ContractAuthMiddleware
from claim_contract.samples import make_submission
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response


class ReceivedCallback:
    def __init__(self, kind: str, body: dict[str, Any], idem: str, applied: bool, gap: bool) -> None:
        self.kind, self.body, self.idem, self.applied, self.gap = kind, body, idem, applied, gap

    @property
    def sequence(self) -> int:
        return int(self.body.get("sequence", 0))


class HospitalSim:
    def __init__(self, insurer_app: Any, *, key_id: str = "hosp-001", secret: bytes = b"dev-hosp-to-ins-secret-0000000000000000",
                 callback_secret: bytes = b"dev-ins-to-hosp-secret-0000000000000000", ins_key_id: str = "ins-001") -> None:
        self.key_id, self.secret = key_id, secret
        self.insurer = httpx.AsyncClient(transport=httpx.ASGITransport(app=insurer_app), base_url="http://insurer")
        self.received: list[ReceivedCallback] = []
        self.applied_seq: dict[str, int] = {}
        self._reject: list[int] = []
        self.refresh_urls: dict[str, str] = {}
        self.gap_flags: list[str] = []
        self.receiver = self._build_receiver(callback_secret, ins_key_id)

    # ------------------------------------------------------------------ receiver (insurer -> hospital)
    def _build_receiver(self, secret: bytes, ins_key_id: str) -> FastAPI:
        app = FastAPI()
        install_handlers(app)

        async def handle(request: Request, kind: str) -> Response:
            if self._reject:
                code = self._reject.pop(0)
                return JSONResponse({"code": "service_unavailable"}, status_code=code)
            body = json.loads(request.state.raw_body)
            ref = body["claim_ref"]
            dec = apply_sequence(self.applied_seq.get(ref, 0), int(body["sequence"]))
            applied = dec.action is SequenceAction.apply
            if applied:
                self.applied_seq[ref] = int(body["sequence"])
            if dec.gap_detected:
                self.gap_flags.append(ref)
            self.received.append(ReceivedCallback(kind, body, request.state.contract_auth.idempotency_key or "", applied, dec.gap_detected))
            return Response(status_code=204)

        def make_endpoint(k: str) -> Any:
            async def endpoint(request: Request) -> Response:
                return await handle(request, k)

            return endpoint

        for kind in ("status", "queries", "decisions", "settlements"):
            app.add_api_route(f"/v1/insurer-callbacks/{kind}", make_endpoint(kind), methods=["POST"])

        @app.post("/v1/insurer-callbacks/documents/{doc_id}/refresh-url")
        async def refresh(doc_id: str, request: Request) -> JSONResponse:
            url = self.refresh_urls.get(doc_id)
            if url is None:
                return JSONResponse({"code": "unknown_document"}, status_code=404)
            return JSONResponse({"doc_id": doc_id, "download_url": url, "expires_at": "2026-10-06T11:15:30Z", "sha256": "0" * 64, "size_bytes": 1})

        app.add_middleware(ContractAuthMiddleware, protected_prefixes=["/v1/insurer-callbacks/"], secrets=lambda k: [secret] if k == ins_key_id else None,
                           idempotency=MemoryIdempotencyStore())
        return app

    def receiver_client(self) -> httpx.AsyncClient:
        """httpx client the insurer's OutboxSender uses to reach this hospital (no network)."""
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=self.receiver), base_url="http://hospital")

    def reject_next(self, n: int = 1, status: int = 503) -> None:
        self._reject = [status] * n

    # ------------------------------------------------------------------ sender (hospital -> insurer)
    @staticmethod
    def make_claim(**overrides: Any) -> dict[str, Any]:
        return make_submission(**overrides)

    def headers(self, method: str, target: str, body: bytes, idem: str | None, *, key_id: str | None = None, secret: bytes | None = None,
                ts: datetime | None = None, version: str = "1.0") -> dict[str, str]:
        return signing.build_headers(secret or self.secret, key_id or self.key_id, method, target, body, idem if method != "GET" else None,
                                     contract_version=version, now=ts or datetime.now(UTC))

    async def request(self, method: str, target: str, payload: Any = None, *, idem: str | None = None, raw: bytes | None = None, **hk: Any) -> httpx.Response:
        body = raw if raw is not None else (b"" if payload is None else json.dumps(payload, separators=(",", ":")).encode())
        idem = idem or str(uuid.uuid4())
        return await self.insurer.request(method, target, content=body or None, headers=self.headers(method, target, body, idem, **hk))

    async def submit(self, claim: dict[str, Any], idem: str | None = None, **hk: Any) -> httpx.Response:
        return await self.request("POST", "/v1/hospital-api/claims", claim, idem=idem, **hk)

    async def status(self, claim_ref: str, include: str | None = None, **hk: Any) -> httpx.Response:
        t = f"/v1/hospital-api/claims/{claim_ref}" + (f"?include={include}" if include else "")
        return await self.request("GET", t, **hk)

    async def supplement(self, claim_ref: str, payload: dict[str, Any], **hk: Any) -> httpx.Response:
        return await self.request("POST", f"/v1/hospital-api/claims/{claim_ref}/documents", payload, **hk)

    async def withdraw(self, claim_ref: str, reason: str = "patient_requested", **hk: Any) -> httpx.Response:
        return await self.request("POST", f"/v1/hospital-api/claims/{claim_ref}/withdraw", {"reason": reason, "note": "test"}, **hk)

    async def respond_to_query(self, query_id: str, text: str = "Please find the requested details attached.", attach: list[str] | None = None,
                               idem: str | None = None, **hk: Any) -> httpx.Response:
        payload = {"query_id": query_id, "answer_text": text, "attached_doc_ids": attach or [], "responded_by": "desk.test",
                   "responded_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")}
        return await self.request("POST", f"/v1/hospital-api/queries/{query_id}/responses", payload, idem=idem, **hk)

    # ------------------------------------------------------------------ assertions
    def of_kind(self, kind: str) -> list[ReceivedCallback]:
        return [r for r in self.received if r.kind == kind]

    def assert_monotonic_sequences(self) -> None:
        by_claim: dict[str, list[int]] = {}
        for r in self.received:
            if r.applied:
                by_claim.setdefault(r.body["claim_ref"], []).append(r.sequence)
        for ref, seqs in by_claim.items():
            assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs), f"{ref}: {seqs}"

    def assert_status_path(self, expected: list[str], claim_ref: str | None = None) -> None:
        got = [r.body["hospital_visible_status"] for r in self.of_kind("status") if r.applied and (claim_ref is None or r.body["claim_ref"] == claim_ref)]
        assert got == expected, f"{got} != {expected}"

    async def aclose(self) -> None:
        await self.insurer.aclose()
