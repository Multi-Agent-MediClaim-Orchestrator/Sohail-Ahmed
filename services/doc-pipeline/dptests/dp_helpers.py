import io

import pytest

BILL = [
    "CITY CARE HOSPITAL",
    "FINAL BILL",
    "Bill No: FB-2026-0042        Date: 02/10/2026",
    "Patient Name: Ravi Kumar     Mobile: 9876543210",
    "Date of Admission: 28/09/2026    Date of Discharge: 02/10/2026",
    "Treating Doctor: Dr. Anil Rao",
    "",
    "Description                    Qty     Rate      Amount",
    "Room rent general ward           4   2,500.00   10,000.00",
    "ICU charges                      1   5,000.00    5,000.00",
    "Consultation fees                2     500.00    1,000.00",
    "Total                                           16,000.00",
    "Discount                                           500.00",
]


def make_pdf(lines: list[str], pages: int = 1) -> bytes:
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    for _ in range(pages):
        y = 800
        c.setFont("Courier", 10)
        for ln in lines:
            c.drawString(40, y, ln)
            y -= 16
        c.showPage()
    c.save()
    return buf.getvalue()


def make_png(lines: list[str]) -> bytes:
    from PIL import Image, ImageDraw, ImageFont

    img = Image.new("RGB", (1500, 60 + 46 * len(lines)), "white")
    d = ImageDraw.Draw(img)
    font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf", 30)
    for i, ln in enumerate(lines):
        d.text((30, 30 + 46 * i), ln, fill="black", font=font)
    b = io.BytesIO()
    img.save(b, "PNG")
    return b.getvalue()


@pytest.fixture
def bill_pdf() -> bytes:
    return make_pdf(BILL)
