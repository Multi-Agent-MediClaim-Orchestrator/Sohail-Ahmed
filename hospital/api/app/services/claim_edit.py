"""Officer edits: a small RFC 6902 JSON Patch (add/replace/remove) restricted to editable paths (doc 06 §4.1)."""

from __future__ import annotations

import copy
import re
from typing import Any

from pydantic import BaseModel, ConfigDict

from app.core.errors import ApiError

ALLOWED_PREFIXES = (
    "/bill_lines",
    "/totals",
    "/admission/diagnosis_codes",
    "/admission/procedure_codes",
    "/admission/treating_doctor",
    "/patient/gender",
)


class PatchOp(BaseModel):
    model_config = ConfigDict(extra="forbid")
    op: str
    path: str
    value: Any = None


def _tokens(path: str) -> list[str]:
    if not path.startswith("/"):
        raise ApiError("validation_error", f"invalid JSON Pointer {path!r}")
    return [t.replace("~1", "/").replace("~0", "~") for t in path[1:].split("/")]


def editable(path: str) -> bool:
    return any(path == p or path.startswith(p + "/") for p in ALLOWED_PREFIXES)


def _walk(doc: Any, toks: list[str]) -> tuple[Any, str]:
    cur = doc
    for t in toks[:-1]:
        if isinstance(cur, list):
            try:
                cur = cur[int(t)]
            except (ValueError, IndexError):
                raise ApiError("validation_error", f"path segment {t!r} not found") from None
        elif isinstance(cur, dict) and t in cur:
            cur = cur[t]
        else:
            raise ApiError("validation_error", f"path segment {t!r} not found")
    return cur, toks[-1]


def display_path(toks: list[str]) -> str:
    out = ""
    for t in toks:
        out += f"[{t}]" if t.isdigit() or t == "-" else (("." if out else "") + t)
    return out


def apply_patch(payload: dict[str, Any], ops: list[PatchOp]) -> tuple[dict[str, Any], list[str]]:
    """Returns (new payload, changed field names). Names only, never values (audit hygiene)."""
    doc = copy.deepcopy(payload)
    changed: list[str] = []
    for op in ops:
        if op.op not in ("add", "replace", "remove"):
            raise ApiError("validation_error", f"unsupported op {op.op!r}")
        if not editable(op.path):
            raise ApiError(
                "path_not_editable",
                f"{op.path} cannot be edited here; correct it on the case record",
                path=op.path,
            )
        toks = _tokens(op.path)
        parent, last = _walk(doc, toks)
        if isinstance(parent, list):
            if op.op == "add":
                if last == "-":
                    parent.append(op.value)
                else:
                    parent.insert(int(last), op.value)
            else:
                try:
                    i = int(last)
                    parent[i] if i < len(parent) else (_ for _ in ()).throw(IndexError)
                except (ValueError, IndexError):
                    raise ApiError("validation_error", f"index {last!r} out of range") from None
                if op.op == "replace":
                    parent[i] = op.value
                else:
                    del parent[i]
        elif isinstance(parent, dict):
            if op.op == "remove":
                if last not in parent:
                    raise ApiError("validation_error", f"{op.path} does not exist")
                del parent[last]
            else:
                if op.op == "replace" and last not in parent:
                    raise ApiError("validation_error", f"{op.path} does not exist")
                parent[last] = op.value
        else:
            raise ApiError("validation_error", f"cannot edit {op.path}")
        changed.append(re.sub(r"\[-\]", "[+]", display_path(toks)))
    # keep line numbers sequential after inserts/removals
    for i, ln in enumerate(doc.get("bill_lines", []), 1):
        if isinstance(ln, dict):
            ln["line_no"] = i
    return doc, sorted(set(changed))
