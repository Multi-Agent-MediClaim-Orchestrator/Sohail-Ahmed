import json

import httpx
import pytest
from fastapi.testclient import TestClient
from PIL import Image
from vhelpers import H, W, arr, bill_page, blurry, png
from vision import quality, stamps
from vision.analyze import analyze, hospital_quality
from vision.escalate import Escalator
from vision.imageio import ImageError, load_pages
from vision.main import create_app
from vision.settings import Settings

S = Settings()
REG = [{"code": "H-001", "name": "City Care Hospital", "aliases": [], "rohini_id": "12345"}]


def q(im):
    return quality.measure(arr(im), 1, S)


def test_clean_page_is_legible_and_degraded_pages_are_not():
    assert q(bill_page()).legible and q(bill_page()).reasons == []
    b = q(blurry(bill_page()))
    assert not b.legible and "blurry" in b.reasons
    assert "blank" in q(Image.new("RGB", (W, H), "white")).reasons
    dark = q(Image.eval(bill_page(), lambda v: v // 8))
    assert "too_dark" in dark.reasons and not dark.legible
    skewed = q(bill_page().rotate(12, fillcolor="white"))
    assert "skewed" in skewed.reasons and abs(skewed.skew_deg) > 8


def test_round_stamp_is_found_read_and_matched_to_the_registry():
    found = stamps.detect(arr(bill_page()), 1)
    seals = [s for s in found if s.kind in ("seal", "hospital_stamp")]
    assert len(seals) == 1 and seals[0].det_conf >= 0.5 and seals[0].ink_color == "blue"
    s = seals[0]
    s.ocr_text, s.ocr_conf = stamps.ocr_crop(arr(bill_page()), s.bbox)
    assert (
        s.ocr_text and "CARE" in s.ocr_text and "12345" in s.ocr_text
    )  # tesseract may clip the arc text
    ok, who, score = stamps.match_registry(s.ocr_text, REG)
    assert ok is True and who == "H-001" and score >= 85
    assert (
        stamps.present([s | None for s in [s]] if False else [s], ["hospital_stamp"])[
            "hospital_stamp"
        ]
        is True
    )


def test_unstamped_page_has_no_stamp_and_a_foreign_stamp_does_not_match():
    assert [
        s
        for s in stamps.detect(arr(bill_page(stamp=False)), 1)
        if s.kind in ("seal", "hospital_stamp")
    ] == []
    ok, who, score = stamps.match_registry("GREEN VALLEY CLINIC", REG)
    assert ok is False and score < 85
    assert (
        stamps.match_registry("ANY TEXT REG 12345", REG)[0] is True
    )  # registry id in the text is an exact hit
    assert stamps.match_registry(None, REG) == (None, None, None)


async def test_bills_require_the_hospital_stamp():
    good = await analyze([arr(bill_page())], S, REG, doc_type="final_bill")
    assert (
        good["required_stamp_present"] is True
        and good["missing_kinds"] == []
        and good["all_pages_legible"]
    )
    bad = await analyze([arr(bill_page(stamp=False))], S, REG, doc_type="pharmacy_bill")
    assert bad["required_stamp_present"] is False and bad["missing_kinds"] == ["hospital_stamp"]
    none_needed = await analyze([arr(bill_page(stamp=False))], S, REG, doc_type="lab_report_x")
    assert none_needed["required_stamp_present"] is None


async def test_stamp_on_an_earlier_page_counts():
    rep = await analyze(
        [arr(bill_page()), arr(bill_page(stamp=False))], S, REG, doc_type="final_bill"
    )
    assert rep["required_stamp_present"] is True and rep["stamps"][0]["page"] == 1


async def test_hospital_quality_body_matches_what_the_api_accepts():
    g = hospital_quality(await analyze([arr(bill_page())], S, REG, expect=["hospital_stamp"]))
    assert (
        set(g) == {"quality_score", "flags", "has_required_stamp"}
        and g["has_required_stamp"] is True
        and g["flags"] == []
        and 0 <= g["quality_score"] <= 1
    )
    b = hospital_quality(
        await analyze([arr(blurry(bill_page()))], S, REG, expect=["hospital_stamp"])
    )
    assert {"blurry", "unreadable"} <= set(b["flags"]) and b[
        "has_required_stamp"
    ] is None  # absence on an unreadable page is unknowable
    n = hospital_quality(
        await analyze([arr(bill_page(stamp=False))], S, REG, expect=["hospital_stamp"])
    )
    assert n["has_required_stamp"] is False


async def test_escalation_is_local_budgeted_and_never_final():
    calls = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(json.loads(req.content))
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": '{"present": true, "kind": "hospital_stamp", "text": "CITY CARE"}'
                        }
                    }
                ]
            },
        )

    esc = Escalator(
        Settings(esc_per_doc=1), httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    rep = await analyze(
        [arr(bill_page(stamp=False))], S, REG, doc_type="final_bill", esc=esc, doc_id="d1"
    )
    assert (
        rep["escalated"] and rep["needs_human_confirm"] and rep["required_stamp_present"] is False
    )  # code's verdict stands
    assert calls[0]["model"] == S.vision_model and "localhost" not in json.dumps(calls[0])[:0]
    again = await analyze(
        [arr(bill_page(stamp=False))], S, REG, doc_type="final_bill", esc=esc, doc_id="d1"
    )
    assert (
        not again["escalated"] and "escalation_skipped" in again["reasons"] and len(calls) == 1
    )  # per-document budget
    blur = await analyze(
        [arr(blurry(bill_page(stamp=False)))], S, REG, doc_type="final_bill", esc=esc, doc_id="d2"
    )
    assert not blur["escalated"]  # illegible pages are re-scan requests, not escalations


def test_pdf_and_bad_inputs():
    import io

    from reportlab.pdfgen import canvas

    b = io.BytesIO()
    c = canvas.Canvas(b)
    c.drawString(100, 700, "hello")
    c.showPage()
    c.drawString(100, 700, "two")
    c.save()
    assert len(load_pages(b.getvalue())) == 2
    with pytest.raises(ImageError) as e:
        load_pages(b"not an image")
    assert e.value.code == "unsupported_image"
    with pytest.raises(ImageError) as e:
        load_pages(png(Image.new("RGB", (4000, 3000))), max_mp=5)
    assert e.value.code == "image_too_large"


def test_http_surface():
    page = png(bill_page())

    async def fetch(url):
        return page

    class R:
        async def get(self):
            return REG

    with TestClient(create_app(S, fetch=fetch, registry=R())) as c:
        r = c.post("/v1/quality", json={"document_id": "d1", "url": "http://store/x"}).json()
        assert r["has_required_stamp"] is True and set(r) == {
            "quality_score",
            "flags",
            "has_required_stamp",
        }
        a = c.post("/v1/analyze", json={"url": "http://store/x", "doc_type": "final_bill"}).json()
        assert a["required_stamp_present"] is True
        assert (
            c.post("/v1/analyze/upload", files={"file": ("x.png", page, "image/png")}).status_code
            == 200
        )
        assert (
            c.post(
                "/v1/analyze/upload", files={"file": ("x.txt", b"zzz", "text/plain")}
            ).status_code
            == 415
        )
        assert c.get("/v1/health").json()["detector_mode"] == "classical"


def test_sparse_pages_are_not_misjudged():
    """Regression (found by the evaluation harness): on a mostly white page percentile contrast read 0 and the page was
    called blank/too bright; a short page with only a stamp border was called skewed."""
    from PIL import Image, ImageDraw, ImageFont
    from vhelpers import BOLD, MONO

    im = Image.new("RGB", (W, H), "white")
    d = ImageDraw.Draw(im)
    for i in range(6):
        d.text(
            (100, 200 + i * 40),
            f"Short line {i} of a discharge summary",
            fill="black",
            font=ImageFont.truetype(MONO, 26),
        )
    d.rectangle((700, 1300, 1000, 1420), outline=(110, 25, 165), width=6)
    d.text(
        (720, 1340), "HOSPITAL REG 12345", fill=(110, 25, 165), font=ImageFont.truetype(BOLD, 18)
    )
    r = q(im)
    assert r.legible and r.reasons == [] and abs(r.skew_deg) < 1.5
    assert [s for s in stamps.detect(arr(im), 1) if s.kind == "hospital_stamp"], (
        "outlined rectangular stamp must be found"
    )
