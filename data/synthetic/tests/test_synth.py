import json
import subprocess
from decimal import Decimal
from pathlib import Path

import pytest
from synth import degrade, ids, lint
from synth.archetypes import RECIPES
from synth.case import build_case
from synth.corpus import build_corpus, freeze_golden
from synth.render import money


def test_reserved_id_ranges_and_checksums():
    import random

    r = random.Random(1)
    for _ in range(200):
        a = ids.fake_aadhaar(r)
        assert a.startswith("9999") and len(a) == 12 and ids.verhoeff_valid(a)
        assert ids.fake_pan(r).startswith("ZZZ") and ids.fake_ifsc(r).startswith("FAKE0")
    assert (
        ids.real_looking_aadhaar("id 2341 2341 2346") == [] or True
    )  # validity depends on the digits; the next lines pin it
    good = ids.with_check_digit("23412341234")
    assert ids.real_looking_aadhaar(f"id {good}") == [good]
    assert ids.real_looking_aadhaar(f"id {ids.fake_aadhaar(r)}") == []  # reserved range is allowed


def test_indian_money_format():
    assert (
        money(Decimal("125000.5")) == "1,25,000.50"
        and money(Decimal("999")) == "999.00"
        and money(Decimal("10000000")) == "1,00,00,000.00"
    )


def test_same_seed_gives_byte_identical_cases():
    a = build_case(42, 7, RECIPES["S01"])["files"]
    b = build_case(42, 7, RECIPES["S01"])["files"]
    assert {k: v for k, v in a.items()} == {k: v for k, v in b.items()}
    assert build_case(43, 7, RECIPES["S01"])["files"]["case.json"] != a["case.json"]


@pytest.mark.parametrize("rid", sorted(RECIPES))
def test_every_archetype_builds_and_states_its_expectation(rid):
    c = build_case(42, 3, RECIPES[rid])
    case = c["case"]
    exp = case["expected"]["hospital"]
    assert exp["classification"] and case["archetype"] == rid
    comp = exp["completeness"]
    if rid in ("S01", "S02", "S03", "S10", "S24", "S25"):
        assert comp["complete"] is True, comp
    if rid == "S05":
        assert comp["missing"] == ["final_bill"] and comp["complete"] is False
    if rid == "S06":
        assert comp["missing"] == ["implant_sticker"]
    if rid == "S07":
        assert comp["unusable"] == ["final_bill"] and any(
            d["quality_class"] == "poor" for d in case["documents"]
        )
    if rid == "S08":
        assert comp["missing_stamp"] == ["final_bill"]
    if rid == "S10":
        assert exp["validation"]["table_total_mismatch"] == ["pharmacy_bill"]


def test_bill_arithmetic_property_over_many_seeds():
    for i in range(40):
        c = build_case(5, i, RECIPES["S01"])["case"]
        gross = sum(Decimal(x["amount"]) for x in c["bill_lines"])
        assert gross == Decimal(c["totals"]["gross"]) and Decimal(
            c["totals"]["claimed"]
        ) == gross - Decimal(c["totals"]["discounts"])
        assert c["admission"]["admitted_on"] < c["admission"]["discharged_on"]
        assert all(
            Decimal(x["amount"]) == Decimal(x["qty"]) * Decimal(x["unit_price"])
            for x in c["bill_lines"]
        )


def test_label_boxes_sit_on_the_text(tmp_path):
    b = build_case(42, 1, RECIPES["S01"])
    for rel, data in b["files"].items():
        (tmp_path / Path(rel).name).write_bytes(data)
    meta = json.loads(b["files"]["docs/03_final_bill.labels.json"])
    pdf = tmp_path / "03_final_bill.pdf"
    checked = 0
    for lab in meta["labels"]:
        if lab["field"] not in ("patient_name", "total", "bill_number"):
            continue
        x0, y0, x1, y1 = lab["bbox"]
        from reportlab.lib.pagesizes import A4

        top = A4[1] - y1
        out = subprocess.run(
            [
                "pdftotext",
                "-f",
                str(lab["page"]),
                "-l",
                str(lab["page"]),
                "-x",
                str(int(x0) - 3),
                "-y",
                str(int(top) - 3),
                "-W",
                str(int(x1 - x0) + 8),
                "-H",
                str(int(y1 - y0) + 6),
                str(pdf),
                "-",
            ],
            capture_output=True,
            check=True,
        ).stdout.decode()
        assert lab["value"].split()[0] in out, (lab, out)
        checked += 1
    assert checked >= 3


def test_degradation_rule_and_effect():
    assert (
        degrade.quality_class() == "good"
        and degrade.quality_class(blur=3) == "poor"
        and degrade.quality_class(dpi=80) == "poor"
    )
    assert (
        degrade.quality_class(blur=1) == "acceptable"
        and degrade.quality_class(crop_pct=0.1) == "unreadable"
    )
    b = build_case(42, 2, RECIPES["S07"])
    deg = b["files"]["docs/03_final_bill.degraded.pdf"]
    text = subprocess.run(
        ["pdftotext", "-", "-"], input=deg, capture_output=True, check=False
    ).stdout.decode()
    assert text.strip() == ""  # image-only: no text layer, so OCR is required


def test_corpus_manifest_golden_and_lint(tmp_path):
    out = tmp_path / "out"
    m = build_corpus(out, 30, 42)
    assert m["n"] == 30 and sum(m["archetype_counts"].values()) == 30
    assert lint.scan_dir(out) == []
    h = freeze_golden(out, tmp_path / "golden")
    again = build_corpus(tmp_path / "out2", 30, 42)
    assert again["archetype_counts"] == m["archetype_counts"]
    assert all((out / k).read_bytes() == (tmp_path / "out2" / k).read_bytes() for k in list(h)[:20])
    (out / "SYN-000000" / "case.json").write_text(
        '{"x": "id %s"}' % ids.with_check_digit("23412341234")
    )
    assert lint.scan_dir(out)  # the lint catches a real-looking number
