"""``uvicorn insurer_crew.main:app --port 8110``."""

from __future__ import annotations

import os

from .app import create_app
from .runtime import GatewayLLM, get_settings
from .tools import RagClient


def build():
    cfg = get_settings()
    rag = RagClient(cfg.rag_url, cfg.rag_token) if cfg.rag_url else None
    if os.environ.get("INS_CREW_LLM") == "offline":  # make demo-offline: the CrewAI agents run on a deterministic stand-in model
        from .demo import OfflineLLM

        return create_app(cfg, llm=OfflineLLM(), rag=rag)
    return create_app(cfg, llm=GatewayLLM(cfg.llm_gateway_url, cfg.llm_virtual_key, reasoning_effort=cfg.llm_reasoning_effort), rag=rag)


app = build()
