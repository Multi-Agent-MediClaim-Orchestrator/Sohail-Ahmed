"""Writes tests/golden/*.json. Expected values are copied by hand from 07-calc-engine §9.1 / §9.2 — NEVER computed
by the engine. Run ``python tests/golden/make_goldens.py`` after editing."""

from __future__ import annotations

import json
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from builders import ADMITTED, line, make_input  # noqa: E402

OUT = Path(__file__).parent
GOLD: dict[str, dict] = {}


def case(name: str, inp: dict, payable: str, pays: str, blocked: str | None = None, flags: list[str] | None = None,
         lines: dict[str, str] | None = None, remaining_after: str | None = None, note: str = "",
         allowed: dict[str, str] | None = None) -> None:
    GOLD[name] = {"note": note, "input": inp, "expected": {
        "payable_total": payable, "patient_pays_total": pays, "blocked": blocked, "flags": flags or [],
        "line_payables": lines or {}, "line_allowed": allowed or {}, "remaining_after": remaining_after}}


# ---- Ex-1 clean ------------------------------------------------------------------------------------
case("ex01", make_input([
    line("L1", "room_rent", "13500.00", qty="3", unit_price="4500.00", days=3),
    line("L2", "surgeon_fees", "40000.00"), line("L3", "ot_charges", "20000.00"),
    line("L4", "medicine", "18000.00"), line("L5", "investigation", "6500.00")]),
    "98000.00", "0.00", remaining_after="402000.00", note="clean claim within caps")

# ---- Ex-2 room excess + proportionate -------------------------------------------------------------------
case("ex02", make_input([
    line("L1", "room_rent", "30000.00", qty="4", unit_price="7500.00", days=4),
    line("L2", "surgeon_fees", "50000.00"), line("L3", "anaesthesia", "10000.00"), line("L4", "ot_charges", "25000.00"),
    line("L5", "medicine", "30000.00"), line("L6", "implant", "40000.00", is_implant=True)], days=4),
    "146666.67", "38333.33",
    lines={"L1": "20000.00", "L2": "33333.33", "L3": "6666.67", "L4": "16666.67", "L5": "30000.00", "L6": "40000.00"})

# ---- Ex-3 sub-limit then senior co-pay -----------------------------------------------------------------
case("ex03", make_input([
    line("L1", "implant", "120000.00", procedure_group="knee_replacement", is_implant=True),
    line("L2", "surgeon_fees", "60000.00", procedure_group="knee_replacement"),
    line("L3", "ot_charges", "25000.00", procedure_group="knee_replacement"),
    line("L4", "room_rent", "20000.00", qty="5", unit_price="4000.00", days=5),
    line("L5", "medicine", "15000.00")],
    rules_ov={"sub_limits": {"knee_replacement": "150000"}}, days=5, dob="1960-01-15", procedure_group="knee_replacement"),
    "166500.00", "73500.00", allowed={"L1": "87804.88", "L2": "43902.44", "L3": "18292.68"})

# ---- Ex-4 deductible vs co-pay ordering -------------------------------------------------------------------
ex4 = lambda order: make_input([line("L1", "medicine", "100000.00")], dob="1964-01-01",  # noqa: E731
                               rules_ov={"deductible": {"amount": "10000"}, "co_pay_order": order})
case("ex04a", ex4("after_deductible"), "81000.00", "19000.00")
case("ex04b", ex4("before_deductible"), "80000.00", "20000.00", flags=["ORDER_PROFILE_NON_DEFAULT"])

# ---- Ex-5 co-pay stacking -------------------------------------------------------------------------------
ex5 = lambda stack: make_input([line("L1", "medicine", "100000.00")], dob="1960-01-01", network="non_network",  # noqa: E731
                               rules_ov={"stack_co_pay": stack})
case("ex05a", ex5(False), "80000.00", "20000.00", flags=["CO_PAY_SELECTED"])
case("ex05b", ex5(True), "70000.00", "30000.00", flags=["CO_PAY_SELECTED"])

# ---- Ex-6 sum insured cap ---------------------------------------------------------------------------------
case("ex06", make_input([line("L1", "medicine", "80000.00")], si="200000.00", utilised="150000.00"),
     "50000.00", "30000.00", flags=["SUM_INSURED_CAPPED"], remaining_after="0.00")

# ---- Ex-7 pre-existing group block / whole-claim ---------------------------------------------------------
ex7_lines = [
    line("L1", "surgeon_fees", "50000.00", procedure_group="appendectomy"),
    line("L2", "ot_charges", "20000.00", procedure_group="appendectomy"),
    line("L3", "medicine", "10000.00", procedure_group="appendectomy"),
    line("L4", "medicine", "8000.00", procedure_group="diabetes_management"),
    line("L5", "doctor_fees", "4000.00", procedure_group="diabetes_management")]
cover = (ADMITTED - timedelta(days=400)).isoformat()
case("ex07a", make_input(ex7_lines, dx=["K35.8", "E11.9"], cover_start=cover, pre_existing=["E11"],
                         rules_ov={"pre_existing_group_map": {"E11": ["diabetes_management"]}}),
     "80000.00", "12000.00", flags=["WAITING_PERIOD_HIT"], lines={"L4": "0.00", "L5": "0.00"})
case("ex07b", make_input(ex7_lines, dx=["K35.8", "E11.9"], cover_start=cover, pre_existing=["E11"]),
     "0.00", "92000.00", blocked="PRE_EXISTING_WAITING")

# ---- Ex-8 exclusion before sub-limit ---------------------------------------------------------------------
case("ex08", make_input([
    line("L1", "surgeon_fees", "90000.00", procedure_group="knee_replacement"),
    line("L2", "procedure_package", "80000.00", procedure_group="knee_replacement", exclusion_tags=["cosmetic"]),
    line("L3", "medicine", "5000.00")], rules_ov={"sub_limits": {"knee_replacement": "150000"}}, procedure_group="knee_replacement"),
    "95000.00", "80000.00", lines={"L1": "90000.00", "L2": "0.00"})

# ---- Ex-9 ICU + ward single ratio ------------------------------------------------------------------------
case("ex09", make_input([
    line("L1", "room_rent", "18000.00", qty="3", unit_price="6000.00", days=3, room_type="ward"),
    line("L2", "icu", "30000.00", qty="2", unit_price="15000.00", days=2, room_type="icu"),
    line("L3", "surgeon_fees", "60000.00"), line("L4", "medicine", "25000.00")], days=5),
    "100000.00", "33000.00", lines={"L1": "15000.00", "L2": "20000.00", "L3": "40000.00", "L4": "25000.00"})

# ---- Ex-10 ambulance cap + pre-hospitalisation window -----------------------------------------------------
case("ex10", make_input([
    line("L1", "surgeon_fees", "30000.00"), line("L2", "medicine", "10000.00"), line("L3", "ambulance", "5000.00"),
    line("L4", "pre_hospitalisation", "4000.00", service_date="2026-07-31"),
    line("L5", "pre_hospitalisation", "3000.00", service_date="2026-09-01")],
    admitted=date(2026, 9, 10), rules_ov={"line_caps": {"ambulance": "3000"}}),
    "46000.00", "6000.00", lines={"L3": "3000.00", "L4": "0.00", "L5": "3000.00"})

# ---- Ex-11 initial waiting vs accident --------------------------------------------------------------------
cover20 = (ADMITTED - timedelta(days=20)).isoformat()
case("ex11a", make_input([line("L1", "surgeon_fees", "50000.00")], dx=["K80.2"], cover_start=cover20),
     "0.00", "50000.00", blocked="INITIAL_WAITING")
case("ex11b", make_input([line("L1", "surgeon_fees", "50000.00")], dx=["S72.0"], cover_start=cover20, adm_type="emergency"),
     "50000.00", "0.00")

# ---- Ex-12 rounding stress ---------------------------------------------------------------------------------
case("ex12", make_input([line("L1", "medicine", "33333.34"), line("L2", "medicine", "33333.33"), line("L3", "medicine", "33333.33")],
                        rules_ov={"deductible": {"amount": "10000"}}),
     "90000.00", "10000.00", lines={"L1": "30000.00", "L2": "30000.00", "L3": "30000.00"})

# ---- Ex-13 exhausted -----------------------------------------------------------------------------------------
case("ex13", make_input([line("L1", "medicine", "40000.00")], si="100000.00", utilised="100000.00"),
     "0.00", "40000.00", blocked="SUM_INSURED_EXHAUSTED")

# ---- Ex-14 full stack ------------------------------------------------------------------------------------------
case("ex14", make_input([
    line("L1", "room_rent", "22500.00", qty="5", unit_price="4500.00", days=5),
    line("L2", "surgeon_fees", "45000.00"), line("L3", "anaesthesia", "9000.00"), line("L4", "ot_charges", "18000.00"),
    line("L5", "medicine", "40000.00"), line("L6", "implant", "60000.00", is_implant=True),
    line("L7", "consumable", "8000.00", exclusion_tags=["cosmetic"]), line("L8", "ambulance", "4000.00")],
    si="300000.00", bonus="30000.00", utilised="20000.00", dob="1956-05-05", network="non_network", days=5,
    rules_ov={"deductible": {"amount": "5000"}, "line_caps": {"ambulance": "3000"}}),
    "128800.00", "77700.00", flags=["CO_PAY_SELECTED"])

# ---- Ex-15 order-profile divergence -----------------------------------------------------------------------------
ex15_lines = [
    line("L1", "surgeon_fees", "100000.00", procedure_group="knee_replacement"),
    line("L2", "ot_charges", "60000.00", procedure_group="knee_replacement"),
    line("L3", "implant", "40000.00", procedure_group="knee_replacement", is_implant=True),
    line("L4", "room_rent", "18750.00", qty="3", unit_price="6250.00", days=3)]
case("ex15a", make_input(ex15_lines, rules_ov={"sub_limits": {"knee_replacement": "150000"}}, procedure_group="knee_replacement"),
     "141000.00", "77750.00", lines={"L1": "60000.00", "L2": "36000.00", "L3": "30000.00", "L4": "15000.00"})
case("ex15b", make_input(ex15_lines, rules_ov={"sub_limits": {"knee_replacement": "150000"}, "order_profile": "room_first"},
                         procedure_group="knee_replacement"),
     "165000.00", "53750.00", flags=["ORDER_PROFILE_NON_DEFAULT"],
     lines={"L1": "71428.57", "L2": "42857.14", "L3": "35714.29", "L4": "15000.00"})

# ---- edge cases -----------------------------------------------------------------------------------------------------
med10 = [line("L1", "medicine", "10000.00")]
case("edge01", make_input(med10, premium_until=(ADMITTED - timedelta(days=10)).isoformat()), "10000.00", "0.00", flags=["PREMIUM_IN_GRACE"])
case("edge02", make_input(med10, premium_until=(ADMITTED - timedelta(days=60)).isoformat()), "0.00", "10000.00", blocked="PREMIUM_LAPSED")
case("edge03", make_input(med10, cover_start=(ADMITTED + timedelta(days=1)).isoformat()), "0.00", "10000.00", blocked="OUTSIDE_COVER")
case("edge04", make_input([line("L1", "consumable", "5000.00", exclusion_tags=["cosmetic"]),
                           line("L2", "medicine", "7000.00", exclusion_tags=["cosmetic"])]), "0.00", "12000.00", blocked="ALL_LINES_EXCLUDED")
case("edge05", make_input([line("L1", "surgeon_fees", "20000.00"), line("L2", "pre_hospitalisation", "3000.00")]),
     "23000.00", "0.00", flags=["NO_DATE_ON_LINE"])
case("edge06", make_input([line("L1", "room_rent", "12000.00", qty="3", unit_price="4000.00")]), "12000.00", "0.00", flags=["ROOM_DAYS_ASSUMED"])
case("edge07", make_input(med10, declared_total="12000.00"), "10000.00", "0.00", flags=["TOTAL_MISMATCH"])
case("edge08", make_input([line("L1", "other", "4000.00")]), "4000.00", "0.00", flags=["UNMAPPED_LINE"])
case("edge09", make_input([line("L1", "procedure_package", "60000.00", procedure_group="cataract")], days=0, day_care=True,
                          procedure_group="cataract", rules_ov={"sub_limits": {"cataract": "40000"}}), "40000.00", "20000.00")
agent = [line(f"L{i}", "medicine", "1000.00", mapping_source="agent" if i <= 4 else "rule") for i in range(1, 11)]
case("edge10", make_input(agent), "10000.00", "0.00", flags=["DEGRADED_MAPPING"])
case("edge11_inactive", make_input(med10, status="cancelled"), "0.00", "10000.00", blocked="POLICY_INACTIVE")

for name, g in GOLD.items():
    (OUT / f"{name}.json").write_text(json.dumps(g, indent=1) + "\n", encoding="utf-8")
print(f"wrote {len(GOLD)} golden files")
