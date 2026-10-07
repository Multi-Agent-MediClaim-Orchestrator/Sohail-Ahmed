"""Thin FastAPI wrapper (07 §4). Stateless, no DB. Auth: service JWT (HS256) unless ``CALC_ENGINE_AUTH=off``."""

from __future__ import annotations

import os
from typing import Any

import jwt
from claim_contract.errors import ProblemError, install_handlers
from fastapi import Depends, FastAPI, Header, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, ValidationError

from . import __version__
from .engine import ENGINE_VERSION, MAX_LINES, run
from .errors import EngineInvariantError, RulesInvalidError, TooManyLinesError
from .explain import explain
from .models import CalcInput, CalcResult
from .rules_schema import ORDER_PROFILES, SUPPORTED_SCHEMA_VERSIONS

BATCH_MAX = int(os.environ.get("CALC_ENGINE_BATCH_MAX", "500"))
ALLOWED_SUBJECTS = {"svc-insurer-api", "svc-eval", "svc-n8n-insurer"}


def require_service(authorization: str | None = Header(default=None)) -> str:
    if os.environ.get("CALC_ENGINE_AUTH", "on") == "off":
        return "dev"
    secret = os.environ.get("CALC_ENGINE_JWT_SECRET", "")
    if not authorization or not authorization.lower().startswith("bearer ") or not secret:
        raise ProblemError("invalid_signature", "service token required", status=401)
    try:
        claims = jwt.decode(
            authorization.split(" ", 1)[1], secret, algorithms=["HS256"],
            audience=os.environ.get("CALC_ENGINE_JWT_AUDIENCE", "calc-engine"),
            issuer=os.environ.get("CALC_ENGINE_JWT_ISSUER") or None,
            options={"verify_iss": bool(os.environ.get("CALC_ENGINE_JWT_ISSUER"))},
        )
    except jwt.PyJWTError as exc:
        raise ProblemError("invalid_signature", "invalid service token", status=401) from exc
    if claims.get("sub") not in ALLOWED_SUBJECTS:
        raise ProblemError("forbidden", "service not allowed", status=403)
    return str(claims["sub"])


class BatchRequest(BaseModel):
    inputs: list[dict[str, Any]]


def _parse(raw: dict[str, Any]) -> CalcInput:
    if isinstance(raw.get("lines"), list) and len(raw["lines"]) > MAX_LINES:
        raise ProblemError("payload_too_large", f"more than {MAX_LINES} lines", status=413, title="Too many lines")
    try:
        return CalcInput.model_validate(raw)
    except ValidationError as exc:
        errs = [{"field": ".".join(str(p) for p in e["loc"]), "message": e["msg"]} for e in exc.errors()]
        rules_bad = any(str(e["loc"][0]) == "rules" for e in exc.errors() if e["loc"])
        code = "rules_invalid" if rules_bad else "validation_error"
        raise ProblemError(code, "input validation failed", status=422, errors=errs) from exc


def _run(inp: CalcInput) -> CalcResult:
    try:
        return run(inp)
    except TooManyLinesError as exc:
        raise ProblemError("payload_too_large", str(exc), status=413) from exc
    except RulesInvalidError as exc:
        raise ProblemError("rules_invalid", str(exc), status=422) from exc
    except EngineInvariantError as exc:
        raise ProblemError("engine_invariant_violated", f"{exc} trace={exc.trace}", status=500, retryable=False) from exc


def create_app() -> FastAPI:
    app = FastAPI(title="calc-engine", version=__version__)
    install_handlers(app)

    @app.get("/v1/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/v1/version")
    async def version() -> dict[str, Any]:
        return {"engine_version": ENGINE_VERSION, "rules_schema_versions": SUPPORTED_SCHEMA_VERSIONS, "order_profiles": ORDER_PROFILES}

    @app.post("/v1/calculate", dependencies=[Depends(require_service)])
    async def calculate(request: Request) -> Any:
        res = _run(_parse(await request.json()))
        return res.model_dump(mode="json")

    @app.post("/v1/calculate/batch", dependencies=[Depends(require_service)])
    async def batch(body: BatchRequest) -> Any:
        if len(body.inputs) > BATCH_MAX:
            raise ProblemError("payload_too_large", f"batch limited to {BATCH_MAX}", status=413)
        results: list[Any] = []
        for raw in body.inputs:
            try:
                results.append(_run(_parse(raw)).model_dump(mode="json"))
            except ProblemError as pe:
                results.append(pe.to_problem().model_dump(mode="json"))
        return {"results": results}

    @app.post("/v1/calculate/explain", dependencies=[Depends(require_service)], response_class=PlainTextResponse)
    async def explain_ep(request: Request) -> str:
        inp = _parse(await request.json())
        return explain(_run(inp), str(inp.case_id))

    return app


app = create_app()
