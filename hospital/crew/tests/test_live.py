"""Opt-in live smoke against the local Ollama (`pytest -m llm`). Skipped when it is not reachable."""

import httpx
import pytest
from crew.agents import responder
from crew.llm import OllamaLLM
from crew.settings import Settings

pytestmark = pytest.mark.llm
ST = Settings.from_env()


@pytest.fixture
def llm():
    try:
        httpx.get(ST.llm_base_url.removesuffix("/v1") + "/api/tags", timeout=2).raise_for_status()
    except httpx.HTTPError:
        pytest.skip("ollama not reachable")
    return OllamaLLM(ST.llm_base_url, 180)


async def test_local_model_triage_returns_valid_json(llm):
    out = await responder.triage(
        {
            "category": "missing_document",
            "round": 1,
            "text": "Please send the discharge summary of the patient.",
        },
        llm,
        Settings(model_general=ST.model_local),
    )
    assert out["action"] in ("send_documents", "clarify") and isinstance(out["needs_docs"], bool)
