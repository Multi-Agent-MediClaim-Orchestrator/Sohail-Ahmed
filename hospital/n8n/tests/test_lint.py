import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


def _load(name: str, path: Path):  # type: ignore[no-untyped-def]
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    sys.modules[name] = m
    spec.loader.exec_module(m)  # type: ignore[union-attr]
    return m


def test_committed_flows_pass_lint() -> None:
    assert _load("lint_flows", ROOT / "scripts" / "lint_flows.py").lint() == []


def test_flows_are_regenerated_identically() -> None:
    b = _load("build_flows", ROOT / "hospital" / "n8n" / "build_flows.py")
    for f in b.all_flows():
        committed = (b.OUT / f"{f.id}.json").read_text()
        assert committed == json.dumps(f.to_json(), indent=2, sort_keys=True) + "\n", f.id


def test_lint_catches_violations(tmp_path) -> None:  # type: ignore[no-untyped-def]
    lf = _load("lint_flows2", ROOT / "scripts" / "lint_flows.py")
    bad = json.loads((ROOT / "hospital/n8n/flows/hosp_f4_submission_watch.json").read_text())
    for n in bad["nodes"]:
        if n["type"].endswith(".wait"):
            n["parameters"]["amount"] = 90000
            break
    bad["settings"].pop("errorWorkflow")
    (tmp_path / "hosp_f4_submission_watch.json").write_text(json.dumps(bad))
    lf.FLOWS = tmp_path
    errs = lf.lint()
    assert any("Wait node over 24 h" in e for e in errs) and any(
        "no error workflow" in e for e in errs
    )


def test_nested_braces_are_rejected() -> None:
    b = _load("build_flows2", ROOT / "hospital" / "n8n" / "build_flows.py")
    f = b.Flow("x", "hosp_x")
    try:
        f.http("a", "POST", "$env.X", "{a: {b: 1}}")
    except ValueError:
        return
    raise AssertionError("expected ValueError")
