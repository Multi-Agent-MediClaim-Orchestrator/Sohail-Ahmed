"""scripts/demo.py: which steps `make demo` / `make demo-offline` run, without running them."""

import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location("demo_script", Path(__file__).resolve().parents[1] / "scripts" / "demo.py")
demo = importlib.util.module_from_spec(spec)
spec.loader.exec_module(demo)


def titles(**kw):
    return [t for t, _ in demo.plan(**kw)]


def test_online_plan_pulls_models_and_seeds_qdrant():
    t = titles(offline=False)
    assert "Ollama models" in t and any("Qdrant" in x for x in t) and t[-2].startswith("Claims end to end") and t[-1] == "Evaluation report"


def test_offline_plan_needs_no_model_and_no_qdrant():
    t = titles(offline=True)
    assert "Ollama models" not in t and not any("Qdrant" in x for x in t)
    assert demo.e2e_env(True)["E2E_CREW"] == "offline" and demo.e2e_env(True)["E2E_LLM"] == "rules"
    assert demo.rag_env(True)["STORE"] == "memory" and demo.rag_env(True)["RAG_SEED_ON_START"] == "1"


def test_no_containers_skips_docker_steps(monkeypatch):
    monkeypatch.setenv("DEMO_ORCH", "n8n")
    assert any("n8n" in x for x in titles(offline=True))
    t = titles(offline=True, containers=False)
    assert not any("container" in x.lower() or "n8n" in x for x in t)
