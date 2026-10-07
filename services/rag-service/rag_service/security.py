"""Service tokens (HS256 JWT) and the caller -> collection access matrix (04-05 §2.3, §4.2)."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

import jwt

INSURER_READ = {"ins_policy_wording", "ins_medical_guidelines", "ins_case_history"}
HOSP_READ = {"hosp_insurer_rules", "hosp_case_context"}

# service -> (system, read, write)
CALLERS: dict[str, tuple[str, set[str] | str, set[str] | str]] = {
    "insurer-crew": ("insurer", INSURER_READ, set()),
    "insurer-api": ("insurer", INSURER_READ, {"ins_policy_wording", "ins_medical_guidelines", "ins_case_history"}),
    "hospital-crew": ("hospital", HOSP_READ, {"hosp_case_context"}),
    "hospital-api": ("hospital", HOSP_READ, {"hosp_insurer_rules", "hosp_case_context"}),
    "eval-harness": ("shared", "*", "eval_*"),
}


class AccessError(Exception):
    def __init__(self, status: int, code: str, message: str = "") -> None:
        super().__init__(f"{code}: {message}")
        self.status, self.code, self.message = status, code, message


@dataclass
class Caller:
    svc: str
    system: str
    role: str = "service"
    collections: list[str] | None = None
    case_scope: bool = False
    claims: dict[str, Any] = field(default_factory=dict)


def issue_token(secret: str, svc: str, *, role: str = "service", collections: list[str] | None = None, ttl: int = 30 * 86400, now: float | None = None) -> str:
    if svc not in CALLERS:
        raise ValueError(f"unknown service {svc}")
    system = CALLERS[svc][0]
    t = int(now or time.time())
    claims: dict[str, Any] = {"svc": svc, "system": system, "role": role, "iat": t, "exp": t + ttl, "case_scope": svc == "hospital-crew"}
    if collections is not None:
        claims["collections"] = collections
    return jwt.encode(claims, secret, algorithm="HS256")


def decode_token(secret: str, token: str) -> Caller:
    try:
        c = jwt.decode(token, secret, algorithms=["HS256"], options={"require": ["exp", "svc"]})
    except jwt.PyJWTError as e:
        raise AccessError(401, "invalid_token", str(e)) from e
    if c["svc"] not in CALLERS:
        raise AccessError(401, "invalid_token", "unknown service")
    return Caller(svc=c["svc"], system=CALLERS[c["svc"]][0], role=c.get("role", "service"), collections=c.get("collections"), case_scope=bool(c.get("case_scope", False)), claims=c)


def _allowed(spec: set[str] | str, collection: str) -> bool:
    if spec == "*":
        return True
    if isinstance(spec, str):  # "eval_*"
        return collection.startswith(spec.rstrip("*"))
    return collection in spec


def check(caller: Caller, collection: str, *, write: bool = False) -> None:
    """403 ``collection_forbidden`` for cross-system access, unknown collections, or writes without write rights."""
    _, read, wr = CALLERS[caller.svc]
    if caller.system == "hospital" and collection.startswith("ins_"):
        raise AccessError(403, "collection_forbidden", "hospital callers cannot access insurer collections")
    if caller.system == "insurer" and collection.startswith("hosp_"):
        raise AccessError(403, "collection_forbidden", "insurer callers cannot access hospital collections")
    if caller.collections is not None and collection not in caller.collections:  # token may narrow, never widen
        raise AccessError(403, "collection_forbidden", "collection not in token scope")
    if not _allowed(wr if write else read, collection):
        raise AccessError(403, "collection_forbidden", f"{caller.svc} may not {'write' if write else 'read'} {collection}")
    if write and caller.role == "service" and collection in INSURER_READ | {"hosp_insurer_rules"} and caller.svc.endswith("-crew"):
        raise AccessError(403, "collection_forbidden", "crews cannot write knowledge-base collections")


def forced_filter(caller: Caller, collection: str, case_id: str | None) -> dict[str, list[Any]]:
    """ACL-injected filter conditions that callers cannot override."""
    if collection == "hosp_case_context" and caller.system == "hospital" and caller.role != "admin" and caller.svc == "hospital-crew":
        if not case_id:
            raise AccessError(403, "collection_forbidden", "X-Case-Id header required for hosp_case_context")
        return {"case_id": [case_id]}
    return {}
