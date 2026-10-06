import itertools

import pytest
from claim_contract import transitions as t
from claim_contract.enums import HospitalCaseStatus as H
from claim_contract.enums import InsurerCaseStatus as I  # noqa: N817
from claim_contract.enums import QueryStatus as Q

TABLES = [(t.HOSPITAL_TRANSITIONS, H), (t.INSURER_TRANSITIONS, I), (t.QUERY_TRANSITIONS, Q)]


@pytest.mark.parametrize(("table", "enum"), TABLES)
def test_exhaustive(table: dict, enum: type) -> None:  # type: ignore[type-arg]
    assert set(table) == set(enum)  # every state has an entry
    for a, b in itertools.product(enum, enum):
        if b in table[a]:
            t.assert_transition(table, a, b)
        else:
            with pytest.raises(t.InvalidTransition):
                t.assert_transition(table, a, b)


def test_closed_is_terminal_and_no_skipping() -> None:
    assert t.allowed_next(t.HOSPITAL_TRANSITIONS, H.CLOSED) == set()
    with pytest.raises(t.InvalidTransition):
        t.assert_hospital(H.DRAFT, H.SUBMITTED)
    with pytest.raises(t.InvalidTransition):
        t.assert_insurer(I.VERIFYING, I.APPROVED)  # must pass ready_for_decision first


def test_visible_mapping_total() -> None:
    assert set(t.HOSPITAL_VISIBLE) == set(I)
