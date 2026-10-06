"""hospital-sim: an in-memory implementation of the insurer->hospital callbacks (01-01 §6.2).
Lets Dev B test their sender without Dev A's hospital-api, and backs the schemathesis contract run.

    sim = HospitalSim(secrets={"ins-001": b"..."}, known_claims={"HC-2026-000001"})
    app = sim.app            # ASGI app, mount with uvicorn or httpx.ASGITransport
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ValidationError
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from claim_contract import models as m
from claim_contract.errors import ContractError, from_validation_error
from claim_contract.inbox import SeqResult, apply_sequence
from claim_contract.middleware import (
    ContractConfig,
    ContractMiddleware,
    contract_payload,
    health_payload,
    request_id_var,
    starlette_route_matcher,
)


class HospitalSim:
    def __init__(
        self,
        secrets: dict[str, bytes],
        known_claims: set[str] | None = None,
        verify_signatures: bool = True,
        accept_any_claim: bool = False,
        store: Any = None,
        rate_limiter: Any = None,
    ) -> None:
        self.known = set(known_claims or ())
        self.accept_any = accept_any_claim
        self.last_seq: dict[str, int] = {}
        self.statuses: list[m.StatusUpdate] = []
        self.queries: dict[str, m.Query] = {}
        self.decisions: list[m.Decision] = []
        self.settlements: list[m.SettlementNotice] = []
        self.gaps: list[tuple[str, int]] = []
        self.documents: dict[str, m.DocRefreshResponse] = {}
        routes = [
            Route("/v1/insurer-callbacks/status", self._status, methods=["POST"]),
            Route("/v1/insurer-callbacks/queries", self._query, methods=["POST"]),
            Route("/v1/insurer-callbacks/decisions", self._decision, methods=["POST"]),
            Route("/v1/insurer-callbacks/settlements", self._settlement, methods=["POST"]),
            Route(
                "/v1/insurer-callbacks/documents/{doc_id}/refresh-url",
                self._refresh,
                methods=["POST"],
            ),
            Route("/v1/health", self._health),
            Route("/v1/contract", self._contract),
        ]
        self.app = Starlette(
            routes=routes, exception_handlers={ContractError: self._on_contract_error}
        )
        self.app.add_middleware(
            ContractMiddleware,
            config=ContractConfig(
                secrets=secrets,
                store=store,
                rate_limiter=rate_limiter,
                verify_signatures=verify_signatures,
                protect_prefixes=("/v1/insurer-callbacks/",),
                route_match=starlette_route_matcher(routes),
            ),
        )

    @staticmethod
    async def _on_contract_error(request: Request, exc: Exception) -> Response:
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

    def _check_claim(self, claim_ref: str) -> None:
        if not self.accept_any and claim_ref not in self.known:
            raise ContractError("unknown_claim", f"unknown claim {claim_ref}")

    def _seq(self, claim_ref: str, sequence: int) -> bool:
        """True when the callback must be applied; False for a stale/duplicate one."""
        d = apply_sequence(self.last_seq.get(claim_ref, 0), sequence)
        if d.result is SeqResult.IGNORE:
            return False
        if d.result is SeqResult.APPLY_GAP:
            self.gaps.append((claim_ref, sequence))
        self.last_seq[claim_ref] = d.new_last
        return True

    async def _status(self, request: Request) -> Response:
        u = await self._parse(request, m.StatusUpdate)
        self._check_claim(u.claim_ref)
        if self._seq(u.claim_ref, u.sequence):
            self.statuses.append(u)
        return Response(status_code=204)

    async def _query(self, request: Request) -> Response:
        class Body(BaseModel):
            claim_ref: str
            sequence: int
            query: m.Query

        b = await self._parse(request, Body)
        self._check_claim(b.claim_ref)
        key = str(b.query.query_id)
        prev = self.queries.get(key)
        if prev is not None:
            if prev.text != b.query.text:
                raise ContractError("idempotency_conflict", "query re-sent with changed text")
            return Response(status_code=204)
        self._seq(b.claim_ref, b.sequence)
        self.queries[key] = b.query
        return Response(status_code=204)

    async def _decision(self, request: Request) -> Response:
        class Body(BaseModel):
            claim_ref: str
            sequence: int
            decision: m.Decision

        b = await self._parse(request, Body)
        self._check_claim(b.claim_ref)
        if self._seq(b.claim_ref, b.sequence):
            self.decisions.append(b.decision)
        return Response(status_code=204)

    async def _settlement(self, request: Request) -> Response:
        class Body(BaseModel):
            claim_ref: str
            sequence: int
            settlement: m.SettlementNotice

        b = await self._parse(request, Body)
        self._check_claim(b.claim_ref)
        if self._seq(b.claim_ref, b.sequence):
            self.settlements.append(b.settlement)
        return Response(status_code=204)

    async def _refresh(self, request: Request) -> Response:
        req = await self._parse(request, m.DocRefreshRequest)
        self._check_claim(req.claim_ref)
        doc = self.documents.get(request.path_params["doc_id"])
        if doc is None:
            raise ContractError("unknown_document", "unknown document")
        return JSONResponse(doc.model_dump(mode="json"))

    async def _health(self, request: Request) -> Response:
        return JSONResponse(health_payload())

    async def _contract(self, request: Request) -> Response:
        return JSONResponse(contract_payload())
