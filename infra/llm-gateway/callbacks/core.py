"""Pure, dependency-free gateway policy (04-04 §5.2, §6.1, §6.4). The LiteLLM hooks in the sibling modules are thin wrappers
around these functions so every rule is unit-testable without litellm, Redis or a network."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

PATTERNS: dict[str, re.Pattern[str]] = {
    "aadhaar": re.compile(r"(?<!\d)[2-9]\d{3}[\s-]?\d{4}[\s-]?\d{4}(?!\d)"),
    "pan": re.compile(r"\b[A-Z]{5}\d{4}[A-Z]\b"),
    "mobile": re.compile(r"(?<!\d)(?:\+?91[\s-]?)?[6-9]\d{9}(?!\d)"),
    "email": re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.]+\b"),
    "member_id_raw": re.compile(r"\bMEM-\d{8,}\b"),  # raw ids never leave; masked form is MEM-****1234
}
ALLOW_LISTED_TOKENS = re.compile(r"<(PERSON|PHONE|ID|EMAIL|ADDRESS|AADHAAR|PAN|LOCATION|DATE_TIME)_\d+>")
_CURRENCY_BEFORE = re.compile(r"(?:₹|INR|Rs\.?|Rupees|USD|\$)\s*[:\-]?\s*$", re.IGNORECASE)

VALID_SYSTEMS = {"hospital", "insurer", "shared"}
KEYS_WITHOUT_AGENT_META = {"eval-harness", "n8n-hospital", "n8n-insurer"}

# virtual key -> (allowed aliases, rpm, daily cloud-token budget). None = all aliases.
VIRTUAL_KEYS: dict[str, dict[str, Any]] = {
    "hospital-crew": {"models": ["reason-cloud", "reason-local", "cleanup-local"], "rpm": 20, "budget": 400_000},
    "insurer-crew": {"models": ["reason-cloud", "reason-local"], "rpm": 30, "budget": 600_000},
    "doc-pipeline": {"models": ["cleanup-local", "cleanup-cloud"], "rpm": 30, "budget": 500_000},
    "vision-service": {"models": ["vision-cloud"], "rpm": 10, "budget": 100_000},
    "rag-service": {"models": ["embed", "reason-cloud", "reason-local"], "rpm": 60, "budget": 500_000},
    "n8n-hospital": {"models": ["reason-local"], "rpm": 10, "budget": 50_000},
    "n8n-insurer": {"models": ["reason-local"], "rpm": 10, "budget": 50_000},
    "eval-harness": {"models": None, "rpm": 40, "budget": 1_000_000},
    "tpa-sim": {"models": ["reason-local"], "rpm": 10, "budget": 50_000},
}


class GatewayError(Exception):
    def __init__(self, status: int, code: str, message: str = "") -> None:
        super().__init__(f"{status} {code}: {message}")
        self.status, self.code, self.message = status, code, message

    def body(self) -> dict[str, Any]:
        return {"error": {"code": self.code, "message": self.message}}


def is_cloud(model: str) -> bool:
    return "cloud" in model


def flatten(data: dict[str, Any]) -> str:
    """All text a provider would see: messages, tool call args, `input` of embeddings. Base64 image bytes are skipped."""
    parts: list[str] = []

    def walk(v: Any) -> None:
        if isinstance(v, str):
            if not (v.startswith("data:") and ";base64," in v[:64]):
                parts.append(v)
        elif isinstance(v, dict):
            for k, x in v.items():
                if k in ("image_url", "api_key", "metadata"):
                    if k == "image_url" and isinstance(x, dict) and isinstance(x.get("url"), str) and not x["url"].startswith("data:"):
                        parts.append(x["url"])
                    continue
                walk(x)
        elif isinstance(v, list | tuple):
            for x in v:
                walk(x)

    walk(data.get("messages", []))
    walk(data.get("input", []))
    walk(data.get("tools", []))
    return "\n".join(parts)


def pii_hits(text: str) -> set[str]:
    """Pattern *names* that match; Aadhaar/mobile-shaped digits right after a currency marker are amounts, not identifiers."""
    text = ALLOW_LISTED_TOKENS.sub("", text)
    hits: set[str] = set()
    for name, pat in PATTERNS.items():
        for m in pat.finditer(text):
            if name in ("aadhaar", "mobile") and _CURRENCY_BEFORE.search(text[max(0, m.start() - 12) : m.start()]):
                continue
            hits.add(name)
            break
    return hits


def check_metadata(data: dict[str, Any], key_alias: str | None) -> dict[str, Any]:
    meta = data.get("metadata") or {}
    system = meta.get("system")
    if system not in VALID_SYSTEMS:
        raise GatewayError(400, "metadata_required", "metadata.system must be hospital|insurer|shared")
    if key_alias not in KEYS_WITHOUT_AGENT_META:
        for f in ("agent", "prompt_version"):
            if not meta.get(f):
                raise GatewayError(400, "metadata_required", f"metadata.{f} is required")
    if not meta.get("claim_ref") and not meta.get("session_id"):
        meta["session_id"] = "kb-ingest"
    if data.get("stream") and data.get("response_format"):
        raise GatewayError(400, "stream_not_supported", "stream=true is not supported for JSON calls")
    data["metadata"] = meta
    return data


def check_model_allowed(model: str, key_alias: str | None) -> None:
    spec = VIRTUAL_KEYS.get(key_alias or "")
    if spec is not None and spec["models"] is not None and model not in spec["models"]:
        raise GatewayError(403, "model_not_allowed", f"alias {model!r} is not allowed for this key")


def pii_guard(data: dict[str, Any], key_alias: str | None, mode: str | None = None, audit: Any = None) -> dict[str, Any]:
    """Pre-call hook body. Raises ``GatewayError(400, 'pii_detected')`` for cloud aliases; never echoes matched text."""
    mode = mode or os.environ.get("PII_GUARD_MODE", "enforce")
    data = check_metadata(data, key_alias)
    model = str(data.get("model", ""))
    if not is_cloud(model):
        return data
    hits = pii_hits(flatten(data))
    if hits:
        if audit:
            audit("pii_blocked", key=key_alias, hits=sorted(hits), claim_ref=data["metadata"].get("claim_ref"))
        if mode == "enforce":
            raise GatewayError(400, "pii_detected", f"patterns: {sorted(hits)}")
    return data


# --------------------------------------------------------------------------------------------------
# daily token budget (cloud aliases only)
# --------------------------------------------------------------------------------------------------
class AsyncKV(Protocol):
    async def get(self, key: str) -> Any: ...
    async def incrby(self, key: str, amount: int) -> Any: ...
    async def expire(self, key: str, seconds: int) -> Any: ...


def budget_key(key_alias: str, now: datetime | None = None) -> str:
    return f"tokbudget:{key_alias}:{(now or datetime.now(UTC)).strftime('%Y%m%d')}"


@dataclass
class TokenBudget:
    kv: AsyncKV
    budgets: dict[str, int] = field(default_factory=lambda: {k: int(v["budget"]) for k, v in VIRTUAL_KEYS.items()})
    fail_open: bool = True

    async def check(self, key_alias: str, model: str, now: datetime | None = None) -> None:
        if not is_cloud(model) or key_alias not in self.budgets:
            return
        try:
            used = int(await self.kv.get(budget_key(key_alias, now)) or 0)
        except Exception:  # noqa: BLE001 - Redis down: fail open but loudly (04-04 §8 #6)
            if self.fail_open:
                return
            raise
        if used >= self.budgets[key_alias]:
            raise GatewayError(429, "budget_exceeded", f"daily token budget for {key_alias} is exhausted")

    async def record(self, key_alias: str, model: str, total_tokens: int, now: datetime | None = None) -> None:
        if not is_cloud(model) or key_alias not in self.budgets or total_tokens <= 0:
            return
        k = budget_key(key_alias, now)
        try:
            await self.kv.incrby(k, int(total_tokens))
            await self.kv.expire(k, 25 * 3600)
        except Exception:  # noqa: BLE001
            return


def fallback_tags(requested: str, served_by: str | None) -> dict[str, str]:
    """Header/tag pair for the fallback_header callback."""
    used = served_by is not None and served_by != "" and served_by != requested and not served_by.startswith(requested)
    out = {"x-llm-fallback-used": "true" if used else "false"}
    if used:
        out["langfuse_tag"] = f"fallback:{requested}->{served_by}"
    return out


EMBED_MODEL_HEADER = "nomic-embed-text@768"
