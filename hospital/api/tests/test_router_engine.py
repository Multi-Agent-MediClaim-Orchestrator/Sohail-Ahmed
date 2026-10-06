"""Pure router engine tests (doc 05 §9): decision matrix, predicate laws, validation, deadlines, preauth."""

import pathlib
import re
from copy import deepcopy
from datetime import UTC, date, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest
from app.router_engine.decide import decide
from app.router_engine.preauth import preauth_check
from app.router_engine.predicates import (
    FALSE,
    TRUE,
    UNKNOWN,
    Tri,
    and_,
    holds,
    not_,
    or_,
    split_suffix,
    validate_cond,
)
from app.router_engine.schema import validate_rules
from hypothesis import given, settings
from hypothesis import strategies as st
from seed.config_payloads import DEADLINES, ROUTER_RULES

NOW = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)
RULES = ROUTER_RULES


def facts(**kw: Any) -> dict[str, Any]:
    base: dict[str, Any] = dict(
        claim_type_proposed="cashless",
        preauth_ref_present=True,
        hospital_network=True,
        admission_source="OPD",
        admission_note_text_flags=set(),
        stay_days=4,
        icd10_codes=["M17.1"],
        procedure_codes=[],
        procedure_group=None,
        claimed_amount=Decimal("45000"),
        doc_types_present=set(),
        preauth_issue=False,
        admitted_at=datetime(2026, 9, 28, 6, 0, tzinfo=UTC),
        discharged_at=datetime(2026, 10, 2, 6, 0, tzinfo=UTC),
    )
    base.update(kw)
    return base


def run(**kw: Any) -> Any:
    ov = kw.pop("overrides", None)
    return decide(facts(**kw), RULES, ov, DEADLINES, NOW)


def shape(d: Any) -> tuple[str, str, list[str]]:
    return d.pipeline, d.admission_type, [f for f in d.flags if f != "provisional"]


# ----------------------------------------------------------------------------- R01-R12 and more
@pytest.mark.parametrize(
    ("name", "kw", "expected"),
    [
        (
            "R01 planned cashless knee",
            dict(procedure_group="ortho_implant", claimed_amount=Decimal("312000")),
            ("cashless", "planned", ["implant"]),
        ),
        (
            "R02 not in network -> reimbursement",
            dict(
                hospital_network=False,
                procedure_group="ortho_implant",
                claimed_amount=Decimal("312000"),
            ),
            ("reimbursement", "planned", ["implant"]),
        ),
        (
            "R03 ER cardiac stent no ref (seed rule)",
            dict(
                preauth_ref_present=False,
                admission_source="ER",
                icd10_codes=["I21.0"],
                procedure_group="cardiac_stent",
                claimed_amount=Decimal("640000"),
                stay_days=5,
            ),
            ("cashless", "emergency", ["high_value", "implant"]),
        ),
        (
            "R04 maternity",
            dict(icd10_codes=["O80"], claimed_amount=Decimal("80000"), stay_days=3),
            ("cashless", "planned", ["maternity"]),
        ),
        (
            "R05 day care",
            dict(icd10_codes=["H25.9"], stay_days=0),
            ("cashless", "planned", ["day_care"]),
        ),
        (
            "R06 RTA medico-legal",
            dict(
                admission_source="ER",
                icd10_codes=["S72.0"],
                stay_days=6,
                claimed_amount=Decimal("120000"),
            ),
            ("cashless", "emergency", ["medico_legal"]),
        ),
        (
            "R08 threshold inclusive",
            dict(icd10_codes=["C34.9"], claimed_amount=Decimal("500000"), stay_days=7),
            ("cashless", "planned", ["high_value"]),
        ),
        (
            "R09 just below threshold",
            dict(icd10_codes=["C34.9"], claimed_amount=Decimal("499999.99"), stay_days=7),
            ("cashless", "planned", []),
        ),
        (
            "R11 emergency note wins over OPD source",
            dict(admission_note_text_flags={"contains_emergency"}),
            ("cashless", "emergency", []),
        ),
        (
            "no preauth ref, OPD -> reimbursement",
            dict(preauth_ref_present=False),
            ("reimbursement", "planned", []),
        ),
        (
            "ER but not network, no ref",
            dict(admission_source="ER", hospital_network=False, preauth_ref_present=False),
            ("reimbursement", "emergency", []),
        ),
        (
            "medico legal via FIR doc only",
            dict(icd10_codes=["K80.2"], doc_types_present={"fir_mlc"}),
            ("cashless", "planned", ["medico_legal"]),
        ),
        (
            "S and T prefixes",
            dict(icd10_codes=["T14.9"]),
            ("cashless", "planned", ["medico_legal"]),
        ),
        ("X and Y prefixes", dict(icd10_codes=["Y09"]), ("cashless", "planned", ["medico_legal"])),
        (
            "lowercase icd prefix still matches",
            dict(icd10_codes=["o80"]),
            ("cashless", "planned", ["maternity"]),
        ),
        (
            "two flags sorted",
            dict(icd10_codes=["O80"], stay_days=0, claimed_amount=Decimal("900000")),
            ("cashless", "planned", ["day_care", "high_value", "maternity"]),
        ),
        ("stay 1 is not day care", dict(stay_days=1), ("cashless", "planned", [])),
        (
            "negative-free stay 0 and amount None",
            dict(stay_days=0, claimed_amount=None),
            ("cashless", "planned", ["day_care"]),
        ),
        (
            "admission source referral",
            dict(admission_source="referral"),
            ("cashless", "planned", []),
        ),
        (
            "preauth issue flag from check",
            dict(preauth_issue=True),
            ("cashless", "planned", ["preauth_issue"]),
        ),
    ],
)
def test_decision_matrix(
    name: str, kw: dict[str, Any], expected: tuple[str, str, list[str]]
) -> None:
    assert shape(run(**kw)) == expected, name


def test_r07_unknown_facts_is_provisional_not_wrong() -> None:
    d = run(
        icd10_codes=None,
        stay_days=None,
        claimed_amount=None,
        admission_source=None,
        admitted_at=None,
        discharged_at=None,
        procedure_group=None,
    )
    assert d.provisional and "provisional" in d.flags and d.pipeline == "cashless"
    d2 = run(hospital_network=None, preauth_ref_present=False)
    assert (
        d2.pipeline == "reimbursement" and d2.provisional
    )  # unknown network could have changed rule 1


def test_steps_follow_the_pipeline_not_scattered_branches() -> None:
    cl = run()
    assert cl.required_steps == [
        "preauth_check",
        "completeness",
        "claim_build",
        "officer_signoff",
        "submit",
    ]
    rb = run(preauth_ref_present=False)
    assert rb.required_steps == [
        "completeness",
        "claim_build",
        "officer_signoff",
        "submit",
        "filing_window_check",
    ]
    em = run(admission_source="ER")
    assert em.required_steps[-1] == "intimation_check" and em.admission_type == "emergency"
    assert (
        "intimation_check"
        not in run(
            admission_source="ER", hospital_network=False, preauth_ref_present=False
        ).required_steps
    )


def test_overrides_applied_last_rules_output_kept() -> None:
    d = run(
        claimed_amount=Decimal("900000"),
        overrides={
            "claim_type": "reimbursement",
            "flags_remove": ["high_value"],
            "flags_add": ["implant"],
        },
    )
    assert d.pipeline == "reimbursement" and [f for f in d.flags if f != "provisional"] == [
        "implant"
    ]
    assert d.rules_output == {
        "pipeline": "cashless",
        "admission_type": "planned",
        "flags": ["high_value"],
    }
    assert d.required_steps[-1] == "filing_window_check"


def test_decision_is_deterministic_and_pure() -> None:
    a, b = run(icd10_codes=["O80", "S72.0"]), run(icd10_codes=["S72.0", "O80"])
    assert a.to_dict() == b.to_dict()
    f = facts()
    before = deepcopy(f)
    decide(f, RULES, None, DEADLINES, NOW)
    assert f == before  # inputs never mutated


# ------------------------------------------------------------------------------------- deadlines
def test_filing_deadline_end_of_day_ist_and_reminders() -> None:
    d = run(preauth_ref_present=False, discharged_at=datetime(2026, 10, 2, 6, 0, tzinfo=UTC))
    assert d.filing_deadline == "2026-11-01T18:29:59Z"  # 2026-11-01 23:59:59 IST
    assert [r["at"] for r in d.reminders] == [
        "2026-10-25T18:29:59Z",
        "2026-10-30T18:29:59Z",
        "2026-11-01T12:29:59Z",
    ]
    assert "late_filing" not in d.flags


def test_late_filing_flag_and_unknown_discharge() -> None:
    d = decide(
        facts(preauth_ref_present=False, discharged_at=datetime(2026, 8, 1, tzinfo=UTC)),
        RULES,
        None,
        DEADLINES,
        NOW,
    )
    assert "late_filing" in d.flags
    d2 = run(preauth_ref_present=False, discharged_at=None)
    assert d2.filing_deadline is None and d2.provisional


def test_emergency_intimation_and_planned_preauth_lead() -> None:
    d = run(admission_source="ER", admitted_at=datetime(2026, 9, 28, 10, 0, tzinfo=UTC))
    assert d.intimation_deadline == "2026-09-29T10:00:00Z"
    p = run()
    assert p.preauth_by == "2026-09-26T06:00:00Z" and p.intimation_deadline is None
    assert run(admission_source="ER", admitted_at=None).intimation_deadline is None


# ------------------------------------------------------------------------------- predicate language
def test_three_valued_logic_tables() -> None:
    vals = [TRUE, FALSE, UNKNOWN]
    for a in vals:
        for b in vals:
            assert and_(a, b).v == (
                False if (a.v is False or b.v is False) else None if None in (a.v, b.v) else True
            )
            assert or_(a, b).v == (
                True if (a.v is True or b.v is True) else None if None in (a.v, b.v) else False
            )
            # De Morgan holds in Kleene logic
            assert not_(and_(a, b)) == or_(not_(a), not_(b)) and not_(or_(a, b)) == and_(
                not_(a), not_(b)
            )
        assert not_(not_(a)) == a


@pytest.mark.parametrize(
    ("cond", "f", "expected"),
    [
        ({"default": True}, {}, True),
        ({"admission_source": "ER"}, {"admission_source": "ER"}, True),
        ({"admission_source": "ER"}, {"admission_source": "OPD"}, False),
        ({"admission_source": "ER"}, {}, None),
        ({"admission_source": ["ER", "referral"]}, {"admission_source": "referral"}, True),
        ({"claimed_amount_gte": 100}, {"claimed_amount": Decimal("100")}, True),
        ({"claimed_amount_gt": 100}, {"claimed_amount": Decimal("100")}, False),
        ({"claimed_amount_lte": 100}, {"claimed_amount": 99.5}, True),
        ({"claimed_amount_lt": 100}, {"claimed_amount": "100"}, False),
        ({"claimed_amount_gte": 100}, {"claimed_amount": "abc"}, None),
        ({"icd10_prefix": ["O", "Z3"]}, {"icd10_codes": ["Z38.0"]}, True),
        ({"icd10_prefix": ["O"]}, {"icd10_codes": []}, False),
        ({"icd10_prefix": ["O"]}, {"icd10_codes": None}, None),
        (
            {"admission_note_text_flags": {"contains": ["contains_emergency"]}},
            {"admission_note_text_flags": {"contains_emergency"}},
            True,
        ),
        (
            {"admission_note_text_flags": {"contains": ["x"]}},
            {"admission_note_text_flags": set()},
            False,
        ),
        (
            {"any": [{"admission_source": "ER"}, {"stay_days_lte": 0}]},
            {"admission_source": "OPD", "stay_days": 0},
            True,
        ),
        (
            {"any": [{"admission_source": "ER"}, {"stay_days_lte": 0}]},
            {"admission_source": "OPD"},
            None,
        ),
        (
            {"all": [{"admission_source": "ER"}, {"stay_days_lte": 0}]},
            {"admission_source": "OPD"},
            False,
        ),
        ({"not": {"admission_source": "ER"}}, {"admission_source": "OPD"}, True),
        ({"not": {"admission_source": "ER"}}, {}, None),
        (
            {"icd10_prefix": ["S"], "or_doc_type_present": ["fir_mlc"]},
            {"icd10_codes": ["K1"], "doc_types_present": {"fir_mlc"}},
            True,
        ),
        (
            {"icd10_prefix": ["S"], "or_doc_type_present": ["fir_mlc"]},
            {"icd10_codes": ["K1"], "doc_types_present": set()},
            False,
        ),
        (
            {"hospital_network": True, "preauth_ref_present": True},
            {"hospital_network": True, "preauth_ref_present": False},
            False,
        ),
        ({"hospital_network": True, "preauth_ref_present": True}, {"hospital_network": True}, None),
    ],
)
def test_predicates(cond: dict[str, Any], f: dict[str, Any], expected: bool | None) -> None:
    assert holds(cond, f).v is expected


def test_split_suffix() -> None:
    assert split_suffix("claimed_amount_gte") == ("claimed_amount", "gte") and split_suffix(
        "stay_days"
    ) == ("stay_days", "eq")
    assert split_suffix("x_lt") == ("x", "lt") and split_suffix("x_gt") == ("x", "gt")


COND = st.recursive(
    st.one_of(
        st.builds(lambda v: {"admission_source": v}, st.sampled_from(["ER", "OPD", "referral"])),
        st.builds(lambda v: {"stay_days_lte": v}, st.integers(0, 10)),
        st.builds(lambda v: {"claimed_amount_gte": v}, st.integers(0, 1_000_000)),
        st.builds(
            lambda v: {"icd10_prefix": v},
            st.lists(st.sampled_from(["O", "S", "I", "M"]), min_size=1, max_size=3),
        ),
    ),
    lambda c: st.one_of(
        st.builds(lambda xs: {"any": xs}, st.lists(c, min_size=1, max_size=3)),
        st.builds(lambda xs: {"all": xs}, st.lists(c, min_size=1, max_size=3)),
        st.builds(lambda x: {"not": x}, c),
    ),
    max_leaves=8,
)
FACTS = st.fixed_dictionaries(
    {},
    optional={
        "admission_source": st.sampled_from(["ER", "OPD", "referral"]),
        "stay_days": st.integers(0, 10),
        "claimed_amount": st.integers(0, 1_000_000).map(Decimal),
        "icd10_codes": st.lists(st.sampled_from(["O80", "S72", "I21"]), max_size=3),
    },
)


@settings(max_examples=150, deadline=None)
@given(COND, FACTS)
def test_fuzz_valid_grammar_never_raises_and_obeys_laws(
    cond: dict[str, Any], f: dict[str, Any]
) -> None:
    assert validate_cond(cond) == []
    r = holds(cond, f)
    assert isinstance(r, Tri)
    assert holds({"not": {"not": cond}}, f) == r  # double negation
    assert (
        holds({"all": [cond, cond]}, f) == r and holds({"any": [cond, cond]}, f) == r
    )  # idempotence
    if r.v is not None:  # fully known facts: classical logic
        assert holds({"not": cond}, f).v is (not r.v)


@settings(max_examples=100, deadline=None)
@given(COND)
def test_fuzz_mutated_trees_are_rejected(cond: dict[str, Any]) -> None:
    assert validate_cond({"mystery_fact": 1, **cond}) != [] or "mystery_fact" in cond
    assert validate_cond({"any": []}) != []
    assert validate_cond({"claimed_amount_gte": [1, 2]}) != []
    assert validate_cond({"icd10_prefix": "O"}) != []
    assert validate_cond({"default": False}) != []
    assert validate_cond([]) != []


# --------------------------------------------------------------------------------------- validation
def test_seed_rules_validate_and_mutations_fail() -> None:
    assert validate_rules(RULES) == []

    def bad(mutate: Any) -> list[str]:
        r = deepcopy(RULES)
        mutate(r)
        return validate_rules(r)

    assert any("last claim_type rule" in e for e in bad(lambda r: r["claim_type_rules"].pop()))
    assert any(
        "only in the last" in e
        for e in bad(
            lambda r: r["claim_type_rules"].insert(0, {"if": {"default": True}, "then": "cashless"})
        )
    )
    assert any(
        "unknown predicate" in e
        for e in bad(lambda r: r["flag_rules"].append({"flag": "implant", "if": {"bogus": 1}}))
    )
    assert any(
        "not a known flag" in e
        for e in bad(lambda r: r["flag_rules"].append({"flag": "weird", "if": {"default": True}}))
    )
    assert any(
        "positive integer" in e
        for e in bad(lambda r: r["emergency_rules"].update(intimation_hours=0))
    )
    assert any("step_templates" in e for e in bad(lambda r: r["step_templates"].pop("cashless")))
    assert any(
        "known steps" in e
        for e in bad(lambda r: r["step_templates"]["cashless"].append("teleport"))
    )
    assert any(
        "duplicate" in e for e in bad(lambda r: r["step_templates"]["cashless"].append("submit"))
    )
    assert any(
        "must be one of" in e
        for e in bad(lambda r: r["claim_type_rules"][0].update({"then": "barter"}))
    )
    assert any("top-level" in e for e in bad(lambda r: r.update(extra=1)))
    assert validate_rules({}) != []


def test_no_eval_in_router_engine() -> None:
    root = pathlib.Path(__file__).resolve().parents[1] / "app/router_engine"
    pat = re.compile(r"\b(eval|exec|compile)\s*\(")
    hits = [
        f"{p.name}:{i}"
        for p in root.glob("*.py")
        for i, line in enumerate(p.read_text().splitlines(), 1)
        if pat.search(line) and not line.lstrip().startswith(("#", '"""'))
    ]
    assert hits == [], hits


# ----------------------------------------------------------------------------------------- preauth
def row(**kw: Any) -> Any:
    base = dict(
        member_id="M-1",
        status="approved",
        valid_from=date(2026, 1, 1),
        valid_to=date(2026, 12, 31),
        approved_amount=Decimal("100000"),
    )
    base.update(kw)
    return SimpleNamespace(**base)


@pytest.mark.parametrize(
    ("r", "member", "adm", "amount", "state", "issues"),
    [
        (None, "M-1", date(2026, 9, 1), None, "not_found", []),
        (row(), "M-1", date(2026, 9, 1), None, "ok", []),
        (row(), "M-2", date(2026, 9, 1), None, "issues", ["member_mismatch"]),
        (row(status="revoked"), "M-1", date(2026, 9, 1), None, "issues", ["status_revoked"]),
        (row(status="expired"), "M-1", date(2026, 9, 1), None, "issues", ["status_expired"]),
        (row(), "M-1", date(2027, 3, 1), None, "issues", ["outside_validity"]),
        (row(), "M-1", date(2026, 9, 1), Decimal("110001"), "issues", ["expected_exceeds_preauth"]),
        (row(), "M-1", date(2026, 9, 1), Decimal("110000"), "ok", []),
        (row(status="enhanced"), "M-1", date(2026, 9, 1), None, "ok", []),
        (
            row(status="pending"),
            "M-2",
            date(2026, 9, 1),
            None,
            "issues",
            ["member_mismatch", "status_pending"],
        ),
    ],
)
def test_preauth_check(
    r: Any, member: str, adm: date, amount: Any, state: str, issues: list[str]
) -> None:
    res = preauth_check(r, member, adm, amount)
    assert (res.state, res.issues) == (state, issues)
