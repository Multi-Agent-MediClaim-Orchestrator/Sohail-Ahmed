"""insurer-sim: an in-memory implementation of the hospital->insurer API (01-01 §6.1) that can also push the
insurer->hospital callbacks (signed). Reference for Dev A's tests; Dev B's tpa-sim is the richer version.

    sim = InsurerSim(hosp_secrets={"hosp-001": b"..."}, callback_secret=b"...", hospital=httpx_client)
    await sim.push("HC-2026-000001", "status", {...})   # signed callback to the hospital
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import httpx
from pydantic import BaseModel, ValidationError
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from claim_contract import models as m
from claim_contract import signing
from claim_contract.enums import InsurerCaseStatus
from claim_contract.errors import ContractError, from_validation_error
from claim_contract.idempotency import MemoryStore
from claim_contract.middleware import (
    ContractConfig,
    ContractMiddleware,
    contract_payload,
    health_payload,
    request_id_var,
    starlette_route_matcher,
)

CALLBACK_PATHS = {
    "status": "/v1/insurer-callbacks/status",
    "queries": "/v1/insurer-callbacks/queries",
    "decisions": "/v1/insurer-callbacks/decisions",
    "settlements": "/v1/insurer-callbacks/settlements",
}


class InsurerSim:
    def __init__(
        self,
        hosp_secrets: dict[str, bytes],
        callback_secret: bytes = b"",
        hospital: httpx.AsyncClient | None = None,
        callback_key_id: str = "ins-001",
        verify_signatures: bool = True,
        store: Any = None,
    ) -> None:
        self.claims: dict[str, dict[str, Any]] = {}
        self.fail_next: int = 0  # answer the next N claim submissions with 503
        self.fail_status: int = 503
        self.reject_code: str | None = (
            None  # answer the next submission with this terminal (4xx) problem code
        )
        self.received: list[
            dict[str, Any]
        ] = []  # every request that reached a handler (after middleware)
        self.responses: list[m.QueryResponse] = []
        self.supplements: list[m.DocSupplement] = []
        self.withdrawn: list[str] = []
        self.hospital, self.callback_secret, self.callback_key_id = (
            hospital,
            callback_secret,
            callback_key_id,
        )
        self._n = 0
        routes = [
            Route("/v1/hospital-api/claims", self._submit, methods=["POST"]),
            Route("/v1/hospital-api/claims/{claim_ref}", self._status),
            Route(
                "/v1/hospital-api/claims/{claim_ref}/documents", self._supplement, methods=["POST"]
            ),
            Route("/v1/hospital-api/claims/{claim_ref}/queries", self._queries),
            Route("/v1/hospital-api/queries/{query_id}/responses", self._respond, methods=["POST"]),
            Route("/v1/hospital-api/claims/{claim_ref}/withdraw", self._withdraw, methods=["POST"]),
            Route("/v1/health", self._health),
            Route("/v1/contract", self._contract),
        ]
        self.app = Starlette(routes=routes, exception_handlers={ContractError: self._on_error})
        self.app.add_middleware(
            ContractMiddleware,
            config=ContractConfig(
                secrets=hosp_secrets,
                store=store or MemoryStore(),
                verify_signatures=verify_signatures,
                protect_prefixes=("/v1/hospital-api/",),
                route_match=starlette_route_matcher(routes),
            ),
        )

    @staticmethod
    async def _on_error(request: Request, exc: Exception) -> Response:
        if not isinstance(exc, ContractError):
            raise exc
        p = exc.problem()
        p.trace_id = p.trace_id or request_id_var.get()
        return JSONResponse(
            p.model_dump(mode="json"), status_code=p.status, media_type="application/problem+json"
        )

    async def _parse(self, request: Request, model: type[BaseModel]) -> Any:
        try:
            return model.model_validate_json(await request.body())
        except ValidationError as e:
            raise from_validation_error(e) from e

    def _claim(self, claim_ref: str) -> dict[str, Any]:
        if claim_ref not in self.claims:
            raise ContractError("unknown_claim", f"unknown claim {claim_ref}")
        return self.claims[claim_ref]

    async def _submit(self, request: Request) -> Response:
        raw = await request.body()
        self.received.append(
            {"kind": "submit", "body": json.loads(raw), "headers": dict(request.headers)}
        )
        if self.reject_code:
            code, self.reject_code = self.reject_code, None
            raise ContractError(code, "scripted rejection")
        if self.fail_next > 0:
            self.fail_next -= 1
            raise ContractError(
                "service_unavailable" if self.fail_status == 503 else "internal_error",
                "scripted failure",
            )
        sub = await self._parse(request, m.ClaimSubmission)
        known = self.claims.get(sub.claim_ref)
        if known is not None:
            if known["sub"].model_dump_json() != sub.model_dump_json():
                raise ContractError(
                    "duplicate_claim", "claim_ref already received with a different body"
                )
            ack = known["ack"]
        else:
            self._n += 1
            ack = m.Acknowledgement(
                claim_ref=sub.claim_ref,
                insurer_claim_no=f"IC-{sub.submitted_at.year}-{self._n:06d}",
                status=InsurerCaseStatus.RECEIVED,
                received_at=sub.submitted_at,
                sequence=1,  # type: ignore[arg-type]
                document_ingest={"queued": len(sub.documents), "failed": 0},
            )
            self.claims[sub.claim_ref] = {
                "sub": sub,
                "ack": ack,
                "status": "received",
                "seq": 1,
                "queries": {},
            }
        return JSONResponse(ack.model_dump(mode="json"), status_code=202)

    async def _status(self, request: Request) -> Response:
        c = self._claim(request.path_params["claim_ref"])
        return JSONResponse(
            {
                "claim_ref": request.path_params["claim_ref"],
                "insurer_claim_no": c["ack"].insurer_claim_no,
                "status": c["status"],
                "sequence": c["seq"],
                "open_query_ids": list(c["queries"]),
                "decision": None,
            }
        )

    async def _supplement(self, request: Request) -> Response:
        self._claim(request.path_params["claim_ref"])
        sup = await self._parse(request, m.DocSupplement)
        self.supplements.append(sup)
        return JSONResponse({"accepted": len(sup.documents), "sequence": 2}, status_code=202)

    async def _queries(self, request: Request) -> Response:
        c = self._claim(request.path_params["claim_ref"])
        return JSONResponse(
            {
                "items": [q.model_dump(mode="json") for q in c["queries"].values()],
                "next_cursor": None,
                "limit": 50,
            }
        )

    async def _respond(self, request: Request) -> Response:
        r = await self._parse(request, m.QueryResponse)
        self.responses.append(r)
        return JSONResponse({"status": "answered", "sequence": 3}, status_code=202)

    async def _withdraw(self, request: Request) -> Response:
        await self._parse(request, m.WithdrawRequest)
        c = self._claim(request.path_params["claim_ref"])
        c["status"] = "closed"
        self.withdrawn.append(request.path_params["claim_ref"])
        return JSONResponse({"status": "closed"})

    async def _health(self, request: Request) -> Response:
        return JSONResponse(health_payload())

    async def _contract(self, request: Request) -> Response:
        return JSONResponse(contract_payload())

    # ---- scripted insurer->hospital callbacks -------------------------------------------------
    def next_seq(self, claim_ref: str) -> int:
        c = self._claim(claim_ref)
        c["seq"] += 1
        return int(c["seq"])

    async def push(
        self,
        claim_ref: str,
        kind: str,
        payload: dict[str, Any],
        *,
        sequence: int | None = None,
        idem: str | None = None,
        ts: str | None = None,
        tamper: bool = False,
    ) -> httpx.Response:
        """Send a signed callback to the hospital. `kind` in status|queries|decisions|settlements."""
        assert self.hospital is not None, "pass hospital=<httpx client> to push callbacks"  # noqa: S101
        c = self._claim(claim_ref)
        seq = sequence if sequence is not None else self.next_seq(claim_ref)
        body_obj = (
            {"claim_ref": claim_ref, "sequence": seq, **payload}
            if kind != "status"
            else {
                "claim_ref": claim_ref,
                "insurer_claim_no": c["ack"].insurer_claim_no,
                "sequence": seq,
                **payload,
            }
        )
        body = json.dumps(body_obj, sort_keys=True, separators=(",", ":"), default=str).encode()
        path = CALLBACK_PATHS[kind]
        ts = ts or signing.now_ts()
        idem = idem or str(uuid.uuid4())
        sig = signing.sign(self.callback_secret, "POST", path, ts, idem, body)
        headers = {
            "X-Contract-Version": "1.1",
            "X-Key-Id": self.callback_key_id,
            "X-Timestamp": ts,
            "X-Idempotency-Key": idem,
            "X-Signature": sig,
            "Content-Type": "application/json",
        }
        if tamper:
            body = body.replace(b"1", b"2", 1)
        return await self.hospital.post(path, content=body, headers=headers)
