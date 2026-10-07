import base64
import os

import pytest
from docpipe.llm import Fake, LLMUnavailable
from docpipe.pipeline import run
from docpipe.settings import Settings
from docpipe.stages import classify, extract, guard, mask, render, tables, validate
from docpipe.stages.entities import verhoeff_ok
from dp_helpers import BILL, make_pdf, make_png

S = Settings(
    parser="auto", pii_key_b64=base64.b64encode(os.urandom(32)).decode(), allow_cloud=False
)
LOCAL, CLOUD = S.model_local, S.model_cloud


def verhoeff_number(prefix: str) -> str:
    for d in range(10):
        if verhoeff_ok(prefix + str(d)):
            return prefix + str(d)
    raise AssertionError


AADHAAR = verhoeff_number("23412341234")


def good(extra=None):
    v = {
        "bill_number": {"value": "FB-2026-0042", "quote": "FB-2026-0042"},
        "date": {"value": "02/10/2026", "quote": "02/10/2026"},
        "patient_name": {"value": "<PERSON_1>", "quote": "<PERSON_1>"},
        "admitted_on": {"value": "28/09/2026", "quote": "28/09/2026"},
        "discharged_on": {"value": "02/10/2026", "quote": "02/10/2026"},
        "total": {"value": "16000", "quote": "16,000.00"},
        "discounts": {"value": "500", "quote": "500.00"},
        "hospital_name": {"value": "CITY CARE HOSPITAL", "quote": "CITY CARE HOSPITAL"},
    }
    v.update(extra or {})
    return v


# ---- render
def test_text_layer_pdf_is_read_without_ocr(bill_pdf):
    r = render.render(bill_pdf)
    assert r.engine == "pdftotext" and r.pages[0].source == "textlayer" and r.pages[0].conf == 0.99
    assert "Room rent general ward" in r.pages[0].text


def test_scanned_image_goes_through_ocr():
    r = render.render(make_png(BILL))
    assert r.engine == "tesseract" and r.pages[0].conf > 0.6
    assert "16,000.00" in r.pages[0].text and "FINAL BILL" in r.pages[0].text


def test_bad_inputs_are_typed_errors():
    with pytest.raises(render.ParseError) as e:
        render.render(b"hello")
    assert e.value.code == "unsupported_media_type"
    with pytest.raises(render.ParseError) as e:
        render.render(b"%PDF-1.4 garbage")
    assert e.value.code in ("corrupt_file", "ocr_failed")
    with pytest.raises(render.ParseError) as e:
        render.render(make_pdf(BILL, pages=4), max_pages=3)
    assert e.value.code == "too_many_pages"


# ---- tables / classify
def test_bill_lines_are_extracted_by_code():
    lines, total, disc = tables.extract_lines("\n".join(BILL))
    assert [x["description"] for x in lines] == [
        "Room rent general ward",
        "ICU charges",
        "Consultation fees",
    ]
    assert (
        lines[0]
        == {
            "description": "Room rent general ward",
            "amount": "10000.00",
            "qty": "4",
            "unit_price": "2500.00",
        }
        or lines[0]["amount"] == "10000.00"
    )
    assert str(total) == "16000.00" and str(disc) == "500.00"


def test_classification_rules():
    dt, conf, decisive = classify.classify("\n".join(BILL))
    assert dt == "final_bill" and decisive and conf >= 0.75
    assert classify.classify("lorem ipsum dolor")[2] is False


# ---- entities and masking
def test_masking_covers_identifiers_and_keeps_clinical_text():
    text = f"Patient Name: Ravi Kumar Phone 9876543210 Aadhaar {AADHAAR[:4]} {AADHAAR[4:8]} {AADHAAR[8:]} PAN ABCDE1234F email ravi@example.com\nTreating Doctor: Dr. Anil Rao ICD K80.2 Paracetamol"
    m = mask.mask(text)
    for raw in ("9876543210", "ABCDE1234F", "ravi@example.com", "Ravi Kumar"):
        assert raw not in m.text, raw
    assert AADHAAR[:4] not in m.text
    assert (
        "K80.2" in m.text and "Paracetamol" in m.text and "Anil Rao" in m.text
    )  # doctor and clinical text readable
    assert mask.unmask(m.text, m.pii_map) == text


def test_same_person_gets_same_token_and_map_is_encrypted():
    m = mask.mask("Ravi Kumar paid. Receipt for Ravi Kumar. Phone 9876543210")
    assert m.text.count("<PERSON_1>") == 2
    blob = mask.seal(m.pii_map, S.pii_key_b64)
    assert b"Ravi" not in blob and mask.open_(blob, S.pii_key_b64) == m.pii_map
    with pytest.raises(Exception):  # noqa: B017
        mask.open_(blob, base64.b64encode(os.urandom(32)).decode())


def test_verhoeff_and_guard():
    assert verhoeff_ok(AADHAAR) and not verhoeff_ok(
        "234123412346" if AADHAAR != "234123412346" else "234123412347"
    )
    with pytest.raises(guard.GuardTripped):
        guard.assert_safe("contact 9876543210 today", {})
    with pytest.raises(guard.GuardTripped):
        guard.assert_safe("name is Ravi Kumar here", {"<PERSON_1>": "Ravi Kumar"})
    guard.assert_safe("name is <PERSON_1> phone <PHONE_1>", {"<PERSON_1>": "Ravi Kumar"})


# ---- extraction evidence
async def test_unsupported_values_are_nulled_whatever_the_model_says():
    text = "FINAL BILL Total 16,000.00 Date 02/10/2026"
    llm = Fake(
        {
            LOCAL: {
                "total": {"value": "99999", "quote": "99,999"},
                "date": {"value": "02/10/2026", "quote": "02/10/2026"},
                "bill_number": {"value": "X-1", "quote": "X-1"},
            }
        }
    )
    out = await extract.run(llm, LOCAL, "final_bill", text)
    assert (
        out.values["total"] is None
        and out.values["bill_number"] is None
        and out.values["date"] == "02/10/2026"
    )
    assert {i["field"] for i in out.issues} == {"total", "bill_number"}


# ---- pipeline
async def test_two_agreeing_passes_on_a_text_pdf(bill_pdf):
    llm = Fake({LOCAL: good(), CLOUD: good()})
    r = await run(bill_pdf, S, llm)
    assert (
        r["doc_type"] == "final_bill" and r["parser"] == "pdftotext" and r["needs_review"] is False
    ), r
    assert [p["pass_no"] for p in r["passes"]] == [1, 2]
    p1 = r["passes"][0]["typed_json"]
    assert p1["patient_name"] == "Ravi Kumar"  # unmasked locally for the hospital API
    assert p1["total"] == "16000" and len(p1["lines"]) == 3 and p1["discounts"] == "500"
    assert r["passes"][0]["confidence"] == 0.99  # parser confidence, never a model's


async def test_cloud_pass_sees_masked_text_only(bill_pdf):
    cloud_s = Settings(parser="auto", pii_key_b64=S.pii_key_b64, allow_cloud=True)
    llm = Fake({LOCAL: good(), CLOUD: good()})
    await run(bill_pdf, cloud_s, llm)
    cloud_prompts = [p for m, p in llm.calls if m == CLOUD]
    assert cloud_prompts
    for p in cloud_prompts:
        for raw in ("Ravi Kumar", "9876543210"):
            assert raw not in p
        assert "<PERSON_1>" in p


async def test_without_cloud_a_second_local_pass_still_runs(bill_pdf):
    """hospital-api leaves a document 'processing' until it has two passes, so a local-only setup must still produce two."""
    llm = Fake({LOCAL: good(), CLOUD: AssertionError("no cloud")})
    r = await run(bill_pdf, S, llm)
    assert [p["pass_no"] for p in r["passes"]] == [1, 2] and all(m == LOCAL for m, _ in llm.calls)
    assert (
        r["passes"][1]["engine"].endswith(LOCAL)
        and "second_pass_local" in r["review_reasons"]
        and r["needs_review"] is False
    )
    assert "Read the document again" in llm.calls[1][1]


async def test_disagreement_on_a_critical_field_needs_review(bill_pdf):
    llm = Fake(
        {
            LOCAL: good(),
            CLOUD: good(
                {
                    "total": {"value": "16000", "quote": "16,000.00"},
                    "discharged_on": {"value": "28/09/2026", "quote": "28/09/2026"},
                }
            ),
        }
    )
    r = await run(
        bill_pdf, Settings(parser="auto", pii_key_b64=S.pii_key_b64, allow_cloud=True), llm
    )
    assert "critical_disagreement" in r["review_reasons"] and r["needs_review"] is True


async def test_llm_down_gives_a_reviewable_result_not_an_exception(bill_pdf):
    r = await run(bill_pdf, S, Fake({LOCAL: LLMUnavailable("down")}))
    assert (
        r["needs_review"]
        and "llm_unavailable" in r["review_reasons"]
        and r["passes"][0]["typed_json"]["lines"]
    )  # code-extracted lines survive


async def test_identity_documents_never_reach_a_cloud_model():
    pdf = make_pdf(
        [
            "GOVERNMENT OF INDIA",
            "Unique Identification Authority of India",
            "Aadhaar",
            "Name: Ravi Kumar",
            "DOB: 12/03/1984",
            f"{AADHAAR[:4]} {AADHAAR[4:8]} {AADHAAR[8:]}",
        ]
    )
    llm = Fake(
        {
            LOCAL: {
                "id_type": {"value": "Aadhaar", "quote": "Aadhaar"},
                "name": {"value": "<PERSON_1>", "quote": "<PERSON_1>"},
                "dob": {"value": "12/03/1984", "quote": "12/03/1984"},
            },
            CLOUD: AssertionError("cloud must not be called"),
        }
    )
    r = await run(pdf, S, llm, force_second_pass=True)
    assert r["doc_type"] == "id_proof" and all(m == LOCAL for m, _ in llm.calls)
    t = r["passes"][0]["typed_json"]
    assert (
        len(t["id_number_sha256"]) == 64
        and AADHAAR not in str(r)
        and AADHAAR[:4] not in str(llm.calls)
    )


async def test_validation_flags_arithmetic_problems():
    bad = list(BILL)
    bad[-2] = "Total                                           17,000.00"
    r = await run(
        make_pdf(bad),
        S,
        Fake(
            {
                LOCAL: good({"total": {"value": "17000", "quote": "17,000.00"}}),
                CLOUD: good({"total": {"value": "17000", "quote": "17,000.00"}}),
            }
        ),
    )
    assert "validation_error" in r["review_reasons"] and any(
        i["code"] == "table_total_mismatch" for i in r["issues"]
    )


async def test_blank_page_is_review_not_error():
    r = await run(make_png([" "]), S, Fake({}))
    assert r["needs_review"] and "empty_document" in r["review_reasons"] and r["passes"] == []


def test_validators():
    v = validate.validate(
        "final_bill",
        {"admitted_on": "05/10/2026", "discharged_on": "01/10/2026", "total": "-5"},
        [],
        None,
    )
    assert {"date_order", "amount_format"} <= {i["code"] for i in v}
    assert (
        validate.validate("x", {"icd_codes": ["K80.2", "BAD"]}, [], None)[0]["code"] == "icd_format"
    )


def test_net_amount_row_does_not_replace_the_printed_total():
    text = "Description        Qty   Rate   Amount\nRoom rent            2  1,000.00   2,000.00\nTotal                                 2,000.00\nDiscount                                200.00\nNet amount                            1,800.00"
    lines, total, disc = tables.extract_lines(text)
    assert len(lines) == 1 and str(total) == "2000.00" and str(disc) == "200.00"
    only_net = tables.extract_lines("Room rent    1  500.00  500.00\nNet amount        500.00")
    assert str(only_net[1]) == "500.00"  # a net-only bill still gets a total


def test_descriptions_may_end_in_digits():
    lines, _, _ = tables.extract_lines("Tab Paracetamol 500mg #2     3     10.00      30.00")
    assert lines and lines[0]["description"].endswith("#2")


def test_names_are_cut_at_the_next_label():
    from docpipe.stages.extract import clean_name

    assert clean_name("<PERSON_1>        UHID: UH-062672") == "<PERSON_1>"
    assert (
        clean_name("Ravi Kumar UHID UH-1") == "Ravi Kumar"
        and clean_name("Dr. Anil Rao") == "Dr. Anil Rao"
    )
    assert clean_name("Asha Verma  Age 40") == "Asha Verma"


def test_a_person_span_stops_at_a_column_gap():
    m = mask.mask("Patient Name: Hemang Gopal      Age/Sex: 38/M   UHID: UH-000001")
    assert (
        "Age/Sex" in m.text
        and "Hemang" not in m.text
        and m.pii_map == {"<PERSON_1>": "Hemang Gopal"}
    )


def test_surgical_final_bill_is_still_a_final_bill():
    text = "CITY CARE HOSPITAL\nFINAL BILL\nBill No: FB-1\nOT charges operation theatre   1  23,000.00  23,000.00\nSurgeon fees  1  40,000.00  40,000.00\nAnaesthesia charges  1  9,000.00  9,000.00\nNet amount   72,000.00"
    dt, conf, decisive = classify.classify(text)
    assert dt == "final_bill" and decisive
    assert (
        classify.classify(
            "PROCEDURE CHARGES\nOT charges operation theatre\nSurgeon fees\nAnaesthesia"
        )[0]
        == "procedure_bill"
    )
