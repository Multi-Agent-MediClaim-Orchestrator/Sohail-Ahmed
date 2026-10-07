"""Prometheus metrics (03-02 §5 task 18 and siblings). Cheap module-level counters; ``/metrics`` renders them."""

from __future__ import annotations

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, generate_latest

registry = CollectorRegistry()
RECEIPT = Counter("receipt_total", "claims received by result", ["result"], registry=registry)
RECEIPT_LATENCY = Histogram("receipt_latency_seconds", "claim receipt latency", registry=registry, buckets=(0.05, 0.1, 0.25, 0.5, 1, 2, 5))
IDEMPOTENT_REPLAYS = Counter("idempotent_replays_total", "replayed idempotent requests", registry=registry)
DOC_FETCH = Counter("doc_fetch_total", "document fetches by status", ["status"], registry=registry)
OUTBOX_PENDING = Gauge("outbox_pending", "pending callbacks", registry=registry)
OUTBOX_DEAD = Gauge("outbox_dead_letter", "dead-lettered callbacks (05-04 name)", registry=registry)
STEP_TOTAL = Counter("verification_step_total", "verification steps by outcome", ["step", "status"], registry=registry)
FINDING_TOTAL = Counter("finding_total", "findings by code and severity", ["code", "severity"], registry=registry)
AGENT_DISAGREEMENT = Counter("agent_disagreement_total", "agent output differing from deterministic checks", ["step"], registry=registry)
DECISIONS = Counter("decisions_total", "final decisions by tier and outcome", ["tier", "outcome"], registry=registry)
OPEN_TASKS = Gauge("approval_tasks_open", "open approval tasks by tier", ["tier"], registry=registry)
QUERY_ROUNDS = Counter("query_rounds_total", "query rounds opened", ["round"], registry=registry)
QUERY_LINT_FAIL = Counter("query_lint_failures_total", "drafts failing lint", registry=registry)
QUERY_TEMPLATE_FALLBACK = Counter("query_template_fallback_total", "drafts that fell back to the template", registry=registry)
SETTLEMENT = Counter("settlement_total", "settlements by result", ["result"], registry=registry)
SLA_BREACH = Counter("sla_breached_total", "cases that breached their SLA", registry=registry)
DB_POOL_IN_USE = Gauge("db_pool_in_use", "connections checked out of the pool", registry=registry)


CLAIMS_IN_STATUS = Gauge("claims_in_status", "cases per insurer status", ["system", "status"], registry=registry)
HUMAN_QUEUE_AGE = Gauge("human_queue_age_seconds", "age of the oldest item in a human queue", ["queue"], registry=registry)
AUDIT_VERIFY_FAILURES = Counter("audit_chain_verify_failures_total", "audit chain verification failures", ["system"], registry=registry)
STAGE_DURATION = Histogram("claim_stage_duration_seconds", "stage duration", ["system", "stage"], registry=registry, buckets=(0.1, 0.5, 1, 5, 15, 60, 300, 1800))
HTTP_REQUESTS = Counter("http_requests_total", "http requests", ["svc", "route", "status"], registry=registry)
READY = Gauge("service_ready", "1 when /v1/ready reports ready", registry=registry)


def render() -> bytes:
    return generate_latest(registry)
