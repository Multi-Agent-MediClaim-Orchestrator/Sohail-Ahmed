"""Hypothesis strategies producing valid random ``CalcInput`` dicts (07 §9.3)."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from builders import ADMITTED, line, make_input
from hypothesis import strategies as st

GROUPS = [
    "room_rent", "icu", "nursing", "doctor_fees", "surgeon_fees", "anaesthesia", "ot_charges", "implant", "medicine",
    "consumable", "investigation", "procedure_package", "ambulance", "pre_hospitalisation", "post_hospitalisation",
    "non_medical", "other",
]


def paise(lo: int, hi: int) -> st.SearchStrategy[Decimal]:
    return st.integers(lo, hi).map(lambda p: Decimal(p) / 100)


@st.composite
def line_dicts(draw: st.DrawFn, n: int, admitted: date, discharged: date, groups: list[str] | None = None) -> list[dict[str, Any]]:
    out = []
    for i in range(1, n + 1):
        g = draw(st.sampled_from(groups or GROUPS))
        claimed = draw(paise(0, 100_000_000))
        kw: dict[str, Any] = {}
        if g in ("room_rent", "icu"):
            days = draw(st.integers(1, 10))
            kw["days"] = days
            kw["unit_price"] = f"{(claimed / days).quantize(Decimal('0.01')):.2f}"
        if g in ("pre_hospitalisation", "post_hospitalisation"):
            if draw(st.booleans()):
                off = draw(st.integers(0, 90))
                kw["service_date"] = (admitted - timedelta(days=off) if g == "pre_hospitalisation" else discharged + timedelta(days=off)).isoformat()
        kw["procedure_group"] = draw(st.sampled_from([None, None, "g1", "g2"]))
        if draw(st.integers(0, 9)) == 0:
            kw["exclusion_tags"] = ["cosmetic"]
        kw["mapping_source"] = draw(st.sampled_from(["rule", "rule", "agent"]))
        kw["is_non_medical"] = g == "non_medical"
        out.append(line(f"L{i:02d}", g, f"{claimed:.2f}", **kw))
    return out


@st.composite
def calc_inputs(draw: st.DrawFn, groups: list[str] | None = None, simple_rules: bool = False) -> dict[str, Any]:
    n = draw(st.integers(1, 12))
    days = draw(st.integers(0, 12))
    adm_type = draw(st.sampled_from(["planned", "emergency"]))
    admitted = ADMITTED
    discharged = admitted + timedelta(days=days)
    lines = draw(line_dicts(n, admitted, discharged, groups))
    si = draw(st.integers(50_000, 1_000_000))
    rules_ov: dict[str, Any] = {}
    if simple_rules:
        rules_ov = {"co_pay": {"percent": "0"}, "non_network_co_pay_percent": "0", "deductible": {"amount": "0"},
                    "exclusions": {"icd_prefixes": [], "tags": [], "non_medical_policy": "allow"},
                    "proportionate_deduction": False, "room_rent": {"percent": "100", "icu_percent": "100"},
                    "waiting_periods_days": {"initial": 0, "pre_existing": 0, "specific": {}},
                    "hospitalisation_windows": {"pre_days": 10_000, "post_days": 10_000}}
    else:
        rules_ov = {
            "room_rent": {"percent": str(draw(st.sampled_from(["0.5", "1.0", "2.0", "5"]))), "icu_percent": str(draw(st.sampled_from(["1", "2", "4", "8"])))},
            "proportionate_deduction": draw(st.booleans()),
            "co_pay": {"percent": str(draw(st.integers(0, 30))), "conditions": {"age_gte": draw(st.sampled_from([None, 40, 61]))}},
            "non_network_co_pay_percent": str(draw(st.integers(0, 30))),
            "stack_co_pay": draw(st.booleans()),
            "co_pay_order": draw(st.sampled_from(["after_deductible", "before_deductible"])),
            "order_profile": draw(st.sampled_from(["standard", "room_first"])),
            "deductible": {"amount": f"{draw(paise(0, 5_000_000)):.2f}"},
            "sub_limits": draw(st.sampled_from([{}, {"g1": f"{draw(paise(0, 20_000_000)):.2f}"}, {"g1": "50000", "g2": "80000"}])),
            "line_caps": draw(st.sampled_from([{}, {"ambulance": "3000"}, {"medicine": "20000"}])),
            "waiting_periods_days": {"initial": draw(st.sampled_from([0, 30])), "pre_existing": 730, "specific": {}},
        }
    return make_input(
        lines, rules_ov=rules_ov, days=days, adm_type=adm_type, si=f"{si}.00",
        utilised=f"{draw(st.integers(0, si))}.00", dob=draw(st.sampled_from(["1981-04-02", "1960-01-01"])),
        network=draw(st.sampled_from(["network", "non_network"])),
        cover_start=draw(st.sampled_from(["2020-01-01", (ADMITTED - timedelta(days=15)).isoformat()])),
        procedure_group=draw(st.sampled_from([None, "g1"])),
    )
