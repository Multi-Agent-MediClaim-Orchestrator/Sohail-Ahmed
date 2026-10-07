"""Orchestrator (07 §6.2-6.3). Pure: no I/O, no clock, no randomness."""

from __future__ import annotations

import os
from decimal import Decimal

from claim_contract.models import Deduction, Money

from . import __version__
from .errors import TooManyLinesError
from .models import CalcFlag, CalcInput, CalcResult, FlagCode, LineResult, LineRuleHit, MappedGroup
from .money import ZERO
from .state import EngineState
from .steps import (
    s00_prechecks,
    s01_waiting,
    s02_exclusions,
    s03_sublimit,
    s04_room,
    s05_caps,
    s06_eligible,
    s07_deductible,
    s08_copay,
    s10_sum_insured,
    s11_allocate,
    s12_invariants,
)

MAX_LINES = int(os.environ.get("CALC_ENGINE_MAX_LINES", "2000"))
ENGINE_VERSION = f"{__version__}+{os.environ.get('CALC_ENGINE_GIT_SHA', 'dev')}"
DEGRADED_RATIO = Decimal("0.30")


def _input_flags(st: EngineState, inp: CalcInput) -> None:
    lines = inp.lines
    for ln in lines:
        if ln.mapped_group == MappedGroup.other:
            st.flag(FlagCode.UNMAPPED_LINE, "line not mapped to a rule group; reviewer must check", ln.line_ref)
    agent = sum(1 for ln in lines if ln.mapping_source == "agent")
    if Decimal(agent) / Decimal(len(lines)) > DEGRADED_RATIO:
        st.flag(FlagCode.DEGRADED_MAPPING, f"{agent}/{len(lines)} lines mapped by the agent")
    if inp.declared_total is not None:
        total = sum((ln.claimed_amount for ln in lines), ZERO)
        if total != inp.declared_total:
            st.flag(FlagCode.TOTAL_MISMATCH, f"lines sum to {total} but declared total is {inp.declared_total}")
    if inp.rules.order_profile != "standard" or inp.rules.co_pay_order != "after_deductible":
        st.flag(FlagCode.ORDER_PROFILE_NON_DEFAULT,
                f"order_profile={inp.rules.order_profile}, co_pay_order={inp.rules.co_pay_order}")


def run_state(inp: CalcInput) -> EngineState:
    if len(inp.lines) > MAX_LINES:
        raise TooManyLinesError(f"{len(inp.lines)} lines > {MAX_LINES}")
    st = EngineState.from_lines(inp.lines)
    _input_flags(st, inp)
    st = s00_prechecks(st, inp)
    if not st.blocked:
        st = s01_waiting(st, inp)
    if not st.blocked:
        st = s02_exclusions(st, inp)
        order = [s03_sublimit, s04_room] if inp.rules.order_profile == "standard" else [s04_room, s03_sublimit]
        for step in order:
            st = step(st, inp)
        st = s05_caps(st, inp)
        st = s06_eligible(st, inp)
        if not st.blocked:
            if inp.rules.co_pay_order == "after_deductible":
                st = s07_deductible(st, inp)
                st = s08_copay(st, inp)
            else:
                st = s08_copay(st, inp)
                st = s07_deductible(st, inp)
            st = s10_sum_insured(st, inp)
    st = s11_allocate(st, inp)
    s12_invariants(st, inp)
    return st


def to_result(st: EngineState, inp: CalcInput) -> CalcResult:
    lines: list[LineResult] = []
    deductions: dict[tuple[str, str], tuple[Decimal, str]] = {}
    for ls in st.lines:
        lines.append(
            LineResult(
                line_ref=ls.ref, claimed=ls.claimed, disallowed=ls.claimed - ls.allowed, allowed_after_caps=ls.allowed,
                deductible_share=ls.ded, co_pay_share=ls.copay, sum_insured_cut=ls.sicut, payable=ls.net,
                rule_trace=[LineRuleHit(rule_id=h.rule_id, step=h.step, amount=h.amount, explanation=h.explanation) for h in ls.hits],
            )
        )
        for h in ls.hits:
            amt, why = deductions.get((ls.ref, h.rule_id), (ZERO, h.explanation))
            deductions[(ls.ref, h.rule_id)] = (amt + h.amount, why)
    claimed_total = sum((ls.claimed for ls in st.lines), ZERO)
    payable_total = sum((ls.net for ls in st.lines), ZERO)
    summary = [
        Deduction(line_ref=ref, rule_id=rule, amount=Money(amount=amt), explanation=why[:500])
        for (ref, rule), (amt, why) in sorted(deductions.items())
        if amt > 0
    ]
    flags: list[CalcFlag] = list(st.flags)
    return CalcResult(
        engine_version=ENGINE_VERSION, rules_version=inp.rules_version, claimed_total=claimed_total,
        eligible_total=st.eligible_total, payable_total=payable_total, patient_pays_total=claimed_total - payable_total,
        lines=lines, summary_deductions=summary, flags=flags, trace=st.trace, blocked=st.blocked,
        remaining_sum_insured_before=st.remaining_before, remaining_sum_insured_after=st.remaining_after,
    )


def run(inp: CalcInput) -> CalcResult:
    return to_result(run_state(inp), inp)
