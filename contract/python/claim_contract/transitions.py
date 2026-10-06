"""State machines as data (01-02 section 8). Both APIs call assert_transition in the same
DB transaction that updates the status."""

from claim_contract.enums import HospitalCaseStatus as H
from claim_contract.enums import InsurerCaseStatus as I  # noqa: N817
from claim_contract.enums import QueryStatus as Q


class InvalidTransition(Exception):
    def __init__(self, current: object, target: object) -> None:
        super().__init__(f"invalid transition {current} -> {target}")
        self.current = current
        self.target = target


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
    I.RECEIVED: {I.VERIFYING},
    I.VERIFYING: {I.NEEDS_INFO, I.READY_FOR_DECISION},
    I.NEEDS_INFO: {I.VERIFYING, I.ESCALATED},
    # system auto-approves when all gates pass and payable <= T_auto (decision.auto_approved)
    I.READY_FOR_DECISION: {I.AWAITING_APPROVAL, I.APPROVED, I.PARTIALLY_APPROVED},
    I.AWAITING_APPROVAL: {I.APPROVED, I.PARTIALLY_APPROVED, I.REJECTED},
    I.ESCALATED: {I.APPROVED, I.PARTIALLY_APPROVED, I.REJECTED},
    I.APPROVED: {I.SETTLED},
    I.PARTIALLY_APPROVED: {I.SETTLED},
    I.REJECTED: {I.CLOSED},
    I.SETTLED: {I.CLOSED},
    I.CLOSED: set(),
}

QUERY_TRANSITIONS: dict[Q, set[Q]] = {
    Q.OPEN: {Q.DRAFT_READY, Q.ANSWERED, Q.ESCALATED},
    Q.DRAFT_READY: {Q.ANSWERED, Q.ESCALATED},
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


def assert_transition(table: dict, current: object, target: object) -> None:  # type: ignore[type-arg]
    if target not in table.get(current, set()):
        raise InvalidTransition(current, target)


def allowed_next(table: dict, current: object) -> set:  # type: ignore[type-arg]
    return set(table.get(current, set()))


def assert_hospital(current: H, target: H) -> None:
    assert_transition(HOSPITAL_TRANSITIONS, current, target)


def assert_insurer(current: I, target: I) -> None:
    assert_transition(INSURER_TRANSITIONS, current, target)


def assert_query(current: Q, target: Q) -> None:
    assert_transition(QUERY_TRANSITIONS, current, target)
