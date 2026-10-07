"""Alert rules must only reference metrics that the services really export (a typo here means a silent alert)."""

from __future__ import annotations

import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[3]
RULES = yaml.safe_load((ROOT / "infra" / "observability" / "alerts.yml").read_text(encoding="utf-8"))
PROM = yaml.safe_load((ROOT / "infra" / "observability" / "prometheus.yml").read_text(encoding="utf-8"))
FUNCS = {"rate", "increase", "sum", "avg", "max", "min", "irate", "by", "without", "for", "and", "or", "on"}


def exported_names() -> set[str]:
    sys.path.insert(0, str(ROOT / "insurer" / "api"))
    from app import metrics as api_metrics

    names = set(re.findall(r"^# TYPE (\w+) ", api_metrics.render().decode(), re.M))
    # crew + rag metrics are per-app registries created inside create_app(); read the metric names from their source
    for src in (ROOT / "insurer" / "crew" / "insurer_crew" / "app.py", ROOT / "services" / "rag-service" / "rag_service" / "app.py"):
        names |= set(re.findall(r'(?:Counter|Gauge|Histogram)\(\s*"(\w+)"', src.read_text(encoding="utf-8")))
    return names


def test_every_metric_in_alert_exprs_is_exported():
    have = exported_names()
    have |= {n.removesuffix("_total") for n in have} | {n + "_total" for n in have}
    for g in RULES["groups"]:
        for rule in g["rules"]:
            idents = set(re.findall(r"[a-zA-Z_:][a-zA-Z0-9_:]*", re.sub(r"\{[^}]*\}|\[[^\]]*\]|\"[^\"]*\"", "", rule["expr"])))
            metrics = {i for i in idents if i not in FUNCS and not i.isdigit()}
            missing = metrics - have
            assert not missing, f"{rule['alert']} references unknown metric(s) {missing}"


def test_rules_have_severity_and_runbook_and_scrape_targets_unique():
    for g in RULES["groups"]:
        for rule in g["rules"]:
            assert rule["labels"]["severity"] in ("low", "medium", "high", "critical") and rule["annotations"]["runbook"]
    jobs = [j["job_name"] for j in PROM["scrape_configs"]]
    assert len(jobs) == len(set(jobs)) and "insurer-api" in jobs


def test_no_high_cardinality_labels_in_declared_metrics():
    sys.path.insert(0, str(ROOT / "insurer" / "api"))
    src = (ROOT / "insurer" / "api" / "app" / "metrics.py").read_text(encoding="utf-8")
    assert not re.search(r"\[[^\]]*(case_id|claim_ref|user)[^\]]*\]", src)
