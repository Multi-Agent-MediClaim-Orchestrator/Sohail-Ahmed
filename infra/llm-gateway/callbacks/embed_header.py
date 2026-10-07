"""Adds ``x-embed-model`` to embedding responses; rag-service records it in collection metadata (05-05 §3)."""

from __future__ import annotations

from .core import EMBED_MODEL_HEADER

try:
    from litellm.integrations.custom_logger import CustomLogger
except ImportError:  # pragma: no cover
    class CustomLogger:  # type: ignore[no-redef]
        pass


class EmbedHeader(CustomLogger):
    async def async_post_call_success_hook(self, data, user_api_key_dict, response):  # noqa: ANN001
        if data.get("model") == "embed":
            hidden = getattr(response, "_hidden_params", {}) or {}
            hidden.setdefault("additional_headers", {})["x-embed-model"] = EMBED_MODEL_HEADER
        return response


proxy_handler_instance = EmbedHeader()
