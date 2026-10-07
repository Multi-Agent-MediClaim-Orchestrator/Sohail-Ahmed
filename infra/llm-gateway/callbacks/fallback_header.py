"""Success hook: adds ``x-llm-fallback-used`` so the eval harness can report the model mix."""

from __future__ import annotations

from .core import fallback_tags

try:
    from litellm.integrations.custom_logger import CustomLogger
except ImportError:  # pragma: no cover
    class CustomLogger:  # type: ignore[no-redef]
        pass


class FallbackHeader(CustomLogger):
    async def async_post_call_success_hook(self, data, user_api_key_dict, response):  # noqa: ANN001
        requested = str((data.get("metadata") or {}).get("requested_model") or data.get("model", ""))
        hidden = getattr(response, "_hidden_params", {}) or {}
        tags = fallback_tags(requested, hidden.get("model_group") or hidden.get("model_id"))
        hidden.setdefault("additional_headers", {}).update({"x-llm-fallback-used": tags["x-llm-fallback-used"]})
        return response


proxy_handler_instance = FallbackHeader()
