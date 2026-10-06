"""Builds the OpenAPI 3.0 document for contract v1.1 from the pydantic models, so the spec cannot
drift from the code. `scripts/export_schemas.py` writes it to contract/openapi/."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from claim_contract import models as m
from claim_contract.errors import CATALOGUE, ProblemDetail

MODELS: list[type[BaseModel]] = [
    m.ClaimSubmission,
    m.Acknowledgement,
    m.StatusUpdate,
    m.Query,
    m.QueryResponse,
    m.Decision,
    m.SettlementNotice,
    m.DocSupplement,
    m.WithdrawRequest,
    m.DocRefreshRequest,
    m.DocRefreshResponse,
    m.DocumentRef,
    m.Patient,
    m.Admission,
    m.BillLine,
    m.ClaimTotals,
    m.Money,
    m.ConfigVersions,
    m.Citation,
    m.Deduction,
    ProblemDetail,
]


def schemas() -> dict[str, dict[str, Any]]:
    """name -> JSON schema with refs rewritten to #/components/schemas/<name>."""
    out: dict[str, dict[str, Any]] = {}
    for model in MODELS:
        js = model.model_json_schema(
            ref_template="#/components/schemas/{model}", mode="serialization"
        )
        for name, d in js.pop("$defs", {}).items():
            out.setdefault(name, d)
        out[model.__name__] = js
    return dict(sorted(out.items()))


def _ref(name: str) -> dict[str, str]:
    return {"$ref": f"#/components/schemas/{name}"}


def _body(name: str) -> dict[str, Any]:
    return {"required": True, "content": {"application/json": {"schema": _ref(name)}}}


def _resp(desc: str, schema: str | None = None) -> dict[str, Any]:
    r: dict[str, Any] = {"description": desc}
    if schema:
        r["content"] = {"application/json": {"schema": _ref(schema)}}
    return r


def _err(*codes: int) -> dict[str, Any]:
    return {str(c): {"$ref": "#/components/responses/Problem"} for c in codes}


STD = [
    {"$ref": f"#/components/parameters/{p}"}
    for p in ("ContractVersion", "KeyId", "Timestamp", "Signature", "RequestId", "JourneyId")
]
IDEM = [{"$ref": "#/components/parameters/IdempotencyKey"}]


def _path_param(name: str) -> dict[str, Any]:
    return {"name": name, "in": "path", "required": True, "schema": {"type": "string"}}


def build() -> dict[str, Any]:
    ref_param = _path_param("claim_ref")
    paths: dict[str, Any] = {
        "/v1/hospital-api/claims": {
            "post": {
                "tags": ["hospital->insurer"],
                "operationId": "submitClaim",
                "summary": "Submit claim",
                "parameters": STD + IDEM,
                "requestBody": _body("ClaimSubmission"),
                "responses": {
                    "202": _resp("Accepted", "Acknowledgement"),
                    **_err(400, 401, 409, 413, 422, 424, 429),
                },
            }
        },
        "/v1/hospital-api/claims/{claim_ref}": {
            "get": {
                "tags": ["hospital->insurer"],
                "operationId": "getClaimStatus",
                "summary": "Current status",
                "parameters": STD
                + [
                    ref_param,
                    {
                        "name": "include",
                        "in": "query",
                        "schema": {"type": "string", "enum": ["queries"]},
                    },
                ],
                "responses": {"200": _resp("Status", "StatusUpdate"), **_err(401, 404)},
            }
        },
        "/v1/hospital-api/claims/{claim_ref}/documents": {
            "post": {
                "tags": ["hospital->insurer"],
                "operationId": "supplementDocuments",
                "summary": "Supplement documents",
                "parameters": STD + IDEM + [ref_param],
                "requestBody": _body("DocSupplement"),
                "responses": {"202": _resp("Accepted"), **_err(401, 404, 409, 422)},
            }
        },
        "/v1/hospital-api/claims/{claim_ref}/queries": {
            "get": {
                "tags": ["hospital->insurer"],
                "operationId": "listQueries",
                "summary": "List queries",
                "parameters": STD + [ref_param],
                "responses": {"200": _resp("Paginated queries"), **_err(401, 404)},
            }
        },
        "/v1/hospital-api/queries/{query_id}/responses": {
            "post": {
                "tags": ["hospital->insurer"],
                "operationId": "respondToQuery",
                "summary": "Answer a query",
                "parameters": STD + IDEM + [_path_param("query_id")],
                "requestBody": _body("QueryResponse"),
                "responses": {"202": _resp("Accepted"), **_err(401, 404, 409, 422)},
            }
        },
        "/v1/hospital-api/claims/{claim_ref}/withdraw": {
            "post": {
                "tags": ["hospital->insurer"],
                "operationId": "withdrawClaim",
                "summary": "Withdraw claim",
                "parameters": STD + IDEM + [ref_param],
                "requestBody": _body("WithdrawRequest"),
                "responses": {"200": _resp("Withdrawn"), **_err(401, 404, 409)},
            }
        },
        "/v1/insurer-callbacks/status": {
            "post": {
                "tags": ["insurer->hospital"],
                "operationId": "callbackStatus",
                "summary": "Status push",
                "parameters": STD + IDEM,
                "requestBody": _body("StatusUpdate"),
                "responses": {"204": _resp("Received"), **_err(401, 404, 409, 422)},
            }
        },
        "/v1/insurer-callbacks/queries": {
            "post": {
                "tags": ["insurer->hospital"],
                "operationId": "callbackQuery",
                "summary": "New query",
                "parameters": STD + IDEM,
                "requestBody": _body("Query"),
                "responses": {"204": _resp("Received"), **_err(401, 404, 409, 422)},
            }
        },
        "/v1/insurer-callbacks/decisions": {
            "post": {
                "tags": ["insurer->hospital"],
                "operationId": "callbackDecision",
                "summary": "Decision",
                "parameters": STD + IDEM,
                "requestBody": _body("Decision"),
                "responses": {"204": _resp("Received"), **_err(401, 404, 409, 422)},
            }
        },
        "/v1/insurer-callbacks/settlements": {
            "post": {
                "tags": ["insurer->hospital"],
                "operationId": "callbackSettlement",
                "summary": "Settlement",
                "parameters": STD + IDEM,
                "requestBody": _body("SettlementNotice"),
                "responses": {"204": _resp("Received"), **_err(401, 404, 409, 422)},
            }
        },
        "/v1/insurer-callbacks/documents/{doc_id}/refresh-url": {
            "post": {
                "tags": ["insurer->hospital"],
                "operationId": "refreshDocumentUrl",
                "summary": "Re-presign (v1.1)",
                "parameters": STD + IDEM + [_path_param("doc_id")],
                "requestBody": _body("DocRefreshRequest"),
                "responses": {
                    "200": _resp("New URL", "DocRefreshResponse"),
                    **_err(401, 403, 404, 422, 429),
                },
            }
        },
        "/v1/health": {
            "get": {
                "tags": ["utility"],
                "operationId": "health",
                "summary": "Health (unsigned)",
                "responses": {
                    "200": _resp("ok"),
                    "503": {"$ref": "#/components/responses/Problem"},
                },
            }
        },
        "/v1/contract": {
            "get": {
                "tags": ["utility"],
                "operationId": "contractInfo",
                "summary": "Supported versions (unsigned)",
                "responses": {"200": _resp("versions")},
            }
        },
    }

    def hdr(name: str, desc: str, req: bool = True, **schema: str) -> dict[str, Any]:
        return {
            "name": name,
            "in": "header",
            "required": req,
            "description": desc,
            "schema": {"type": "string", **schema},
        }

    parameters = {
        "ContractVersion": hdr("X-Contract-Version", "MAJOR.MINOR, e.g. 1.1", pattern=r"^1\.\d+$"),
        "KeyId": hdr("X-Key-Id", "HMAC key id, e.g. hosp-001"),
        "Timestamp": hdr("X-Timestamp", "RFC 3339 UTC seconds, skew limit 300 s"),
        "Signature": hdr(
            "X-Signature", "base64 HMAC-SHA256 of the canonical string (see 01-01 §4)"
        ),
        "IdempotencyKey": hdr(
            "X-Idempotency-Key", "UUID; required on mutating calls", format="uuid"
        ),
        "RequestId": hdr(
            "X-Request-Id",
            "optional trace id, 1-64 chars of A-Za-z0-9._-",
            False,
            pattern=r"^[A-Za-z0-9._-]{1,64}$",
        ),
        "JourneyId": hdr(
            "X-Journey-Id", "v1.1: UUID following a claim across both systems", False, format="uuid"
        ),
    }
    for path, item in paths.items():  # errors every protected call can return (middleware level)
        if path in ("/v1/health", "/v1/contract"):
            continue
        for op in item.values():
            for code in (400, 401, 413, 429):
                op["responses"].setdefault(str(code), {"$ref": "#/components/responses/Problem"})
    return {
        "openapi": "3.1.0",
        "info": {
            "title": "Hospital <-> Insurer claims contract",
            "version": "1.1.0",
            "description": "Joint contract (both developers approve changes). Generated from claim_contract.",
        },
        "paths": paths,
        "components": {
            "schemas": schemas(),
            "parameters": parameters,
            "responses": {
                "Problem": {
                    "description": "RFC 7807 problem (code in "
                    + ", ".join(sorted(CATALOGUE))
                    + ")",
                    "content": {"application/problem+json": {"schema": _ref("ProblemDetail")}},
                }
            },
        },
    }
