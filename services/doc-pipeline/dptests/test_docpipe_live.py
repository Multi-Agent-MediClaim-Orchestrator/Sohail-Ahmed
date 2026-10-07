"""Opt-in: real parser + local model (`pytest -m llm`)."""

import httpx
import pytest
from docpipe.llm import Ollama
from docpipe.pipeline import run
from docpipe.settings import Settings
from dp_helpers import BILL, make_pdf

pytestmark = pytest.mark.llm
S = Settings.from_env()


async def test_local_model_extracts_a_bill():
    try:
        httpx.get(S.llm_base_url.removesuffix("/v1") + "/api/tags", timeout=2).raise_for_status()  # noqa: ASYNC210
    except httpx.HTTPError:
        pytest.skip("ollama not reachable")
    r = await run(make_pdf(BILL), Settings(allow_cloud=False), Ollama(S.llm_base_url, 240))
    t = r["passes"][0]["typed_json"]
    assert r["doc_type"] == "final_bill" and len(t["lines"]) == 3
    print({k: v for k, v in t.items() if k != "lines"}, r["review_reasons"])
