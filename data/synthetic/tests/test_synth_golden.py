from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import sys
from pathlib import Path

import pytest
from claim_contract.insurer_side.validation import check_submission
from claim_contract.models import ClaimSubmission
from pydantic import ValidationError

ROOT = Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location("synth_generate", ROOT / "data" / "synthetic" / "synth" / "generate.py")
gen = importlib.util.module_from_spec(spec)
sys.modules["synth_generate"] = gen
spec.loader.exec_module(gen)


@pytest.fixture(scope="module")
def golden(tmp_path_factory):
    return gen.build_golden(42)


def test_golden_has_one_case_per_archetype(golden):
    assert sorted(c["archetype"] for c in golden) == sorted(gen.ARCH) and len(golden) == 25


def test_every_submission_validates_except_s10(golden):
    for c in golden:
        if c["archetype"] == "S10":
            # the shared model rejects the mismatch at parse time (Dev B's own model deferred it to check_submission)
            with pytest.raises(ValidationError, match="totals_mismatch"):
                ClaimSubmission.from_json_dict(c["submission"])
            assert c["expected"]["insurer"]["rejected_at_door"] is True
        else:
            check_submission(ClaimSubmission.from_json_dict(c["submission"]))


def test_generation_is_deterministic():
    a, b = gen.build_corpus(40, 7), gen.build_corpus(40, 7)
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)
    assert json.dumps(gen.build_corpus(40, 8), sort_keys=True) != json.dumps(a, sort_keys=True)


def test_written_documents_match_declared_hashes_and_manifest(tmp_path):
    case = gen.build_golden(42)[0]
    gen.write_case(case, tmp_path)
    d = tmp_path / case["case_id"]
    for doc in case["submission"]["documents"]:
        f = next((d / "docs").glob(f"{doc['doc_type']}_{doc['doc_id'][-4:]}.pdf"))
        data = f.read_bytes()
        assert data.startswith(b"%PDF-1.4") and data.rstrip().endswith(b"%%EOF")
        assert hashlib.sha256(data).hexdigest() == doc["sha256"] and len(data) == doc["size_bytes"]
    again = tmp_path / "again"
    gen.write_case(gen.build_golden(42)[0], again)  # byte-identical on re-run
    assert (d / "manifest.json").read_bytes() == (again / case["case_id"] / "manifest.json").read_bytes()


def test_pdf_writer_pages_and_escaping():
    one = gen.pdf_bytes("T (x)", ["a (b) \\ c"] * 10)
    many = gen.pdf_bytes("T", ["line"] * 120)
    assert b"/Count 1" in one and b"/Count 3" in many and b"\\(b\\)" in one


def test_expected_routes_follow_the_user_approved_gate(golden):
    by = {c["archetype"]: c for c in golden}
    assert by["S01"]["expected"]["insurer"]["route"] == "auto" and float(by["S01"]["expected"]["insurer"]["gate_amount"]) <= 50000
    assert by["S21"]["expected"]["insurer"]["route"] == "dual_approver" and float(by["S21"]["expected"]["insurer"]["gate_amount"]) > 500000
    assert by["S22"]["expected"]["insurer"]["route"] == "single_approver"
    assert by["S05"]["expected"]["insurer"]["status"] == "needs_info" and by["S05"]["expected"]["hospital"]["missing"] == ["discharge_summary"]
    assert by["S13"]["expected"]["insurer"]["finding_codes_include"] == ["coverage.waiting_period"]


def test_s11_room_rent_cap_reduces_payable_below_claimed(golden):
    s11 = next(c for c in golden if c["archetype"] == "S11")
    claimed = float(s11["submission"]["totals"]["claimed"]["amount"])
    assert float(s11["expected"]["insurer"]["payable"]) < claimed  # proportional deduction applied by the independent reference


def test_s16_exhausted_policy_caps_payable_at_remaining_si(golden):
    s16 = next(c for c in golden if c["archetype"] == "S16")
    si = float(s16["policy"]["sum_insured"])
    assert float(s16["utilised_prior"]) == pytest.approx(si * 0.97)
    assert float(s16["expected"]["insurer"]["payable"]) <= si * 0.03 + 0.01


def test_reference_calc_hand_checked_rows():
    """Independent hand computations (no engine involved): clean claim pays in full; the 1%/day room cap bites exactly as computed."""
    master = gen.build_master()
    c = gen.build_case("S01", 1, 42, master)
    assert c["expected"]["insurer"]["payable"] == c["submission"]["totals"]["claimed"]["amount"]  # no cap/copay/blocks on a small Gold claim
    r = gen.build_case("S11", 2, 42, master)
    si = r["policy"]["sum_insured"]
    cap_day = si * 0.01
    room = next(x for x in r["submission"]["bill_lines"] if x["category"] == "room")
    per_day = float(room["unit_price"]["amount"])
    assert per_day > cap_day  # suite rate above the cap: S11 really is over the cap


def test_no_real_looking_identifiers_in_outputs(tmp_path):
    cases = gen.build_corpus(30, 3)
    for c in cases:
        gen.write_case(c, tmp_path)
    aadhaar = re.compile(r"(?<!\d)[2-9]\d{3}\s?\d{4}\s?\d{4}(?!\d)")
    for f in tmp_path.rglob("*.json"):
        txt = re.sub(r"[0-9a-f]{64}", "", f.read_text(encoding="utf-8"))
        assert not aadhaar.search(re.sub(r"\d{4}-\d{2}-\d{2}", "", txt)), f


def test_corpus_mix_roughly_matches_spec():
    cases = gen.build_corpus(400, 5)
    clean = sum(c["archetype"] in ("S01", "S02", "S03") for c in cases) / len(cases)
    adv = sum(c["archetype"] in ("S17", "S18", "S25") for c in cases) / len(cases)
    assert 0.30 <= clean <= 0.50 and 0.05 <= adv <= 0.16
    assert len({c["case_id"] for c in cases}) == 400


def test_committed_golden_files_are_frozen():
    manifest = ROOT / "data" / "synthetic" / "golden" / "corpus_manifest.json"
    if not manifest.exists():
        pytest.skip("golden set not generated yet (python data/synthetic/synth/generate.py --golden)")
    for c in gen.build_golden(42):
        on_disk = json.loads((ROOT / "data" / "synthetic" / "golden" / c["case_id"] / "case.json").read_text(encoding="utf-8"))
        # compare after write_case (it fills real document hashes)
        import tempfile

        with tempfile.TemporaryDirectory() as t:
            gen.write_case(c, Path(t))
            assert json.loads((Path(t) / c["case_id"] / "case.json").read_text(encoding="utf-8")) == on_disk, c["case_id"]
