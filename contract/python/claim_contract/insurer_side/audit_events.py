"""Audit event-type registry (01-04 §8). Unknown types are rejected; each type lists the required payload
keys and the actor types allowed to emit it. Event names are ``domain.entity.verb``."""

from __future__ import annotations

from dataclasses import dataclass


class UnknownEventType(ValueError):
    pass


class InvalidEventPayload(ValueError):
    pass


@dataclass(frozen=True)
class EventSpec:
    actors: frozenset[str]
    required: frozenset[str] = frozenset()


def _s(actors: str, *required: str) -> EventSpec:
    return EventSpec(frozenset(actors.split("|")), frozenset(required))


ANY = "agent|human|system|external"

EVENT_REGISTRY: dict[str, EventSpec] = {
    # --- shared contract registry (01-04 §8) ---
    "case.created": _s("system|human", "claim_type"),
    "doc.uploaded": _s("human|system", "doc_id", "sha256"),
    "doc.scanned": _s("system", "doc_id", "result"),
    "doc.parsed": _s("agent|system", "doc_id"),
    "doc.classified": _s("agent", "doc_id", "doc_type"),
    "completeness.evaluated": _s("system", "missing"),
    "claim.built": _s("agent", "bill_line_count"),
    "human.signoff": _s("human", "role"),
    "claim.submitted": _s("system", "claim_ref"),
    "ack.received": _s("external", "insurer_claim_no"),
    "query.received": _s("external", "query_id"),
    "query.draft_generated": _s("agent|system|human", "query_id"),
    "query.edited": _s("human", "query_id"),
    "query.sent": _s("human|system", "query_id"),
    "verification.step.completed": _s("agent|system", "step"),
    "decision.recommended": _s("agent|human|system", "outcome"),
    "human.approved": _s("human", "decision_id"),
    "human.rejected": _s("human", "decision_id"),
    "escalation.raised": _s("system|human", "reason"),
    "settlement.recorded": _s("system", "settlement_id"),
    "config.published": _s("human", "domain", "name", "version"),
    "config.retired": _s("human", "domain", "name", "version"),
    "config.drafted": _s("human", "domain", "name", "version"),
    "config.validated": _s("human|system", "domain", "name", "version"),
    "config.reevaluated": _s("human", "domain"),
    "outbox.dead": _s("system", "endpoint", "attempts"),
    "audit.verify.failed": _s("system", "reason"),
    # --- insurer-side events (03-02 .. 03-06) ---
    "claim.received": _s("system", "hospital"),
    "claim.refused": _s("system", "reason"),
    "claim.withdrawn": _s("external|system", "reason"),
    "doc.fetched": _s("system", "doc_id"),
    "doc.fetch_failed": _s("system", "doc_id", "reason"),
    "doc.viewed": _s("human", "doc_id"),
    "docs.supplemented": _s("external", "accepted"),
    "verification.run.started": _s("system|human", "run_id"),
    "verification.run.completed": _s("system", "run_id", "outcome"),
    "verification.override": _s("human", "finding_code", "reason_hash"),
    "case.assigned": _s("system|human", "assignee"),
    "case.status_changed": _s("system|human|external", "from", "to"),
    "priority.changed": _s("human|system", "priority"),
    "sla.breached": _s("system", "due_at"),
    "query.response_received": _s("external", "query_id"),
    "query.triaged": _s("agent|system|human", "query_id", "verdict"),
    "query.closed": _s("human|system", "query_id"),
    "round.opened": _s("system", "round"),
    "round.closed": _s("system", "round", "outcome"),
    "escalation.resolved": _s("human", "action"),
    "decision.submitted": _s("human", "decision_id"),
    "decision.returned": _s("human", "decision_id"),
    "decision.finalised": _s("system|human", "decision_id", "outcome"),
    "approval.voted": _s("human", "decision_id", "verdict"),
    "settlement.initiated": _s("system|human", "settlement_id"),
    "settlement.paid": _s("system", "settlement_id", "utr_hash"),
    "settlement.failed": _s("system", "settlement_id", "reason"),
    "settlement.retry_scheduled": _s("system", "settlement_id"),
    "settlement.reversed": _s("system|human", "settlement_id"),
    "settlement.anomaly": _s("system", "settlement_id", "kind"),
    "settlement.utilisation_released": _s("human", "settlement_id"),
    "reconcile.utilisation": _s("system", "drift"),
    "validation.failed": _s("system", "error_summary"),
    "pii.revealed": _s("human", "fields"),
    # --- hospital-side events used by the shared library tests ---
    "human.override": _s("human", "reason_hash"),
    "human.edited": _s("human", "field"),
}


def assert_event_type_known(event_type: str) -> EventSpec:
    try:
        return EVENT_REGISTRY[event_type]
    except KeyError as exc:
        raise UnknownEventType(f"unknown audit event type: {event_type}") from exc


def validate_event(event_type: str, actor_type: str, payload: dict[str, object]) -> None:
    spec = assert_event_type_known(event_type)
    if actor_type not in spec.actors:
        raise InvalidEventPayload(
            f"{event_type}: actor_type {actor_type!r} not allowed (allowed: {sorted(spec.actors)})"
        )
    missing = spec.required - payload.keys()
    if missing:
        raise InvalidEventPayload(f"{event_type}: missing payload keys {sorted(missing)}")
