"""State machines as data (01-02 section 8). Both APIs call assert_transition in the same
DB transaction that updates the status."""

from claim_contract.enums import HospitalCaseStatus as H
from claim_contract.enums import InsurerCaseStatus as I  # noqa: N817
from claim_contract.enums import QueryStatus as Q
from claim_contract.errors import (
    InvalidTransition,  # noqa: E402  (ProblemError subclass; 409 problem+json)
)

_PRE_SUBMIT = (H.DRAFT, H.DOCS_PENDING, H.DOCS_COMPLETE, H.BUILDING_CLAIM, H.READY_FOR_REVIEW)
_DECIDED = (H.APPROVED, H.PARTIALLY_APPROVED, H.REJECTED)

HOSPITAL_TRANSITIONS: dict[H, set[H]] = {
    H.DRAFT: {H.DOCS_PENDING},
    H.DOCS_PENDING: {H.DOCS_COMPLETE},
    H.DOCS_COMPLETE: {H.DOCS_PENDING, H.BUILDING_CLAIM},
    H.BUILDING_CLAIM: {H.READY_FOR_REVIEW, H.DOCS_PENDING},
    H.READY_FOR_REVIEW: {H.SUBMITTED, H.DOCS_PENDING},
    # READY_FOR_REVIEW: only when the receiver rejected the submission outright (4xx), so nothing was accepted
    H.SUBMITTED: {H.ACKNOWLEDGED, *_DECIDED, H.CLOSED, H.READY_FOR_REVIEW},
    H.ACKNOWLEDGED: {H.UNDER_QUERY, *_DECIDED, H.CLOSED},
    H.UNDER_QUERY: {H.ACKNOWLEDGED, *_DECIDED, H.CLOSED},
    H.APPROVED: {H.SETTLED},
    H.PARTIALLY_APPROVED: {H.SETTLED},
    H.REJECTED: {H.CLOSED},
    H.SETTLED: {H.CLOSED},
    H.CLOSED: set(),
}
for _s in _PRE_SUBMIT:  # cancelled before submission
    HOSPITAL_TRANSITIONS[_s].add(H.CLOSED)

INSURER_TRANSITIONS: dict[I, set[I]] = {
    # withdraw (-> CLOSED), escalation from verifying, reviewer rerun and send-back edges come from Dev B's insurer
    I.RECEIVED: {I.VERIFYING, I.CLOSED},
    I.VERIFYING: {I.NEEDS_INFO, I.READY_FOR_DECISION, I.ESCALATED, I.CLOSED},
    I.NEEDS_INFO: {I.VERIFYING, I.ESCALATED, I.CLOSED},
    # system auto-approves when all gates pass and payable <= T_auto (decision.auto_approved)
    # NEEDS_INFO: a reviewer who is about to decide can still ask the hospital a question (reviewer-authored query)
    I.READY_FOR_DECISION: {
        I.NEEDS_INFO,
        I.AWAITING_APPROVAL,
        I.APPROVED,
        I.PARTIALLY_APPROVED,
        I.REJECTED,
        I.VERIFYING,
        I.CLOSED,
    },
    I.AWAITING_APPROVAL: {I.APPROVED, I.PARTIALLY_APPROVED, I.REJECTED, I.READY_FOR_DECISION, I.NEEDS_INFO},
    I.ESCALATED: {
        I.APPROVED,
        I.PARTIALLY_APPROVED,
        I.REJECTED,
        I.READY_FOR_DECISION,
        I.NEEDS_INFO,
    },
    I.APPROVED: {I.SETTLED, I.CLOSED},  # CLOSED: zero payable
    I.PARTIALLY_APPROVED: {I.SETTLED, I.CLOSED},
    I.REJECTED: {I.CLOSED},
    I.SETTLED: {I.CLOSED},
    I.CLOSED: set(),
}

QUERY_TRANSITIONS: dict[Q, set[Q]] = {
    Q.OPEN: {Q.DRAFT_READY, Q.ANSWERED, Q.ESCALATED, Q.CLOSED},
    Q.DRAFT_READY: {Q.OPEN, Q.ANSWERED, Q.ESCALATED, Q.CLOSED},
    Q.ANSWERED: {Q.CLOSED, Q.OPEN},  # OPEN = next round, new query_id
    Q.ESCALATED: {Q.CLOSED},
    Q.CLOSED: set(),
}

# 01-02 section 8.4: what the hospital shows for each insurer status.
HOSPITAL_VISIBLE: dict[I, H] = {
    I.RECEIVED: H.ACKNOWLEDGED,
    I.VERIFYING: H.ACKNOWLEDGED,
    I.READY_FOR_DECISION: H.ACKNOWLEDGED,
    I.AWAITING_APPROVAL: H.ACKNOWLEDGED,
    I.NEEDS_INFO: H.UNDER_QUERY,
    I.ESCALATED: H.UNDER_QUERY,
    I.APPROVED: H.APPROVED,
    I.PARTIALLY_APPROVED: H.PARTIALLY_APPROVED,
    I.REJECTED: H.REJECTED,
    I.SETTLED: H.SETTLED,
    I.CLOSED: H.CLOSED,
}


# Reverse edges allowed only through the settlement reversal service.
REVERSAL_EDGES: frozenset[tuple[I, I]] = frozenset(
    {(I.SETTLED, I.APPROVED), (I.SETTLED, I.PARTIALLY_APPROVED)}
)
_FINAL = frozenset({I.APPROVED, I.PARTIALLY_APPROVED, I.REJECTED})


def assert_transition(table: dict, current: object, target: object) -> None:  # type: ignore[type-arg]
    if target not in table.get(current, set()):
        raise InvalidTransition(str(current), str(target))


def allowed_next(table: dict, current: object) -> set:  # type: ignore[type-arg]
    return set(table.get(current, set()))


def assert_hospital(current: H, target: H) -> None:
    assert_transition(HOSPITAL_TRANSITIONS, current, target)


def assert_insurer(current: I, target: I) -> None:
    assert_transition(INSURER_TRANSITIONS, current, target)


def assert_query(current: Q, target: Q) -> None:
    assert_transition(QUERY_TRANSITIONS, current, target)


def assert_hospital_transition(current: H, target: H) -> None:
    assert_hospital(current, target)


def assert_insurer_transition(current: I, target: I, *, via_reversal: bool = False) -> None:
    if via_reversal and (current, target) in REVERSAL_EDGES:
        return
    assert_insurer(current, target)


def assert_query_transition(current: Q, target: Q) -> None:
    assert_query(current, target)


def hospital_allowed_next(current: H) -> set[H]:
    return allowed_next(HOSPITAL_TRANSITIONS, current)


def insurer_allowed_next(current: I) -> set[I]:
    return allowed_next(INSURER_TRANSITIONS, current)


def is_terminal_insurer(status: I) -> bool:
    return status == I.CLOSED


def is_final_decision(status: I) -> bool:
    return status in _FINAL
