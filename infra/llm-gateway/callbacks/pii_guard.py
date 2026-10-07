"""LiteLLM pre-call hook: metadata validation, model allow-list, PII guard for ``*-cloud`` aliases, daily token budget."""

from __future__ import annotations

import logging
import os

from fastapi import HTTPException

from .core import GatewayError, TokenBudget, check_model_allowed, pii_guard

try:  # litellm is only present inside the gateway image
    from litellm.integrations.custom_logger import CustomLogger
except ImportError:  # pragma: no cover - unit tests run without litellm
    class CustomLogger:  # type: ignore[no-redef]
        pass

log = logging.getLogger("llm-gateway.pii")


def _audit(event: str, **fields: object) -> None:
    log.warning("%s %s", event, fields)  # pattern names only, never matched text


def _redis():
    import redis.asyncio as aioredis

    return aioredis.Redis(host=os.environ.get("REDIS_HOST", "redis"), port=6379, password=os.environ.get("LLM_REDIS_PW") or None, decode_responses=True)


class ProxyHandler(CustomLogger):
    def __init__(self) -> None:
        self._budget: TokenBudget | None = None

    @property
    def budget(self) -> TokenBudget:
        if self._budget is None:
            self._budget = TokenBudget(_redis())
        return self._budget

    async def async_pre_call_hook(self, user_api_key_dict, cache, data, call_type):  # noqa: ANN001
        key_alias = getattr(user_api_key_dict, "key_alias", None)
        try:
            check_model_allowed(str(data.get("model", "")), key_alias)
            data = pii_guard(data, key_alias, audit=_audit)
            await self.budget.check(key_alias or "", str(data.get("model", "")))
        except GatewayError as e:
            raise HTTPException(status_code=e.status, detail=e.body()) from e
        return data

    async def async_log_success_event(self, kwargs, response_obj, start_time, end_time):  # noqa: ANN001
        usage = getattr(response_obj, "usage", None)
        total = int(getattr(usage, "total_tokens", 0) or 0)
        meta = (kwargs.get("litellm_params") or {}).get("metadata") or {}
        key_alias = meta.get("user_api_key_alias") or ""
        await self.budget.record(key_alias, str(kwargs.get("model", "")), total)


proxy_handler_instance = ProxyHandler()
