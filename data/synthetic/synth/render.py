"""ReportLab renderers. Every drawn field emits a label {field, value, page, bbox[x0,y0,x1,y1] in PDF points}; labels
come from the renderer (it knows where it drew), never from OCR."""

from __future__ import annotations

import datetime as dt
import io
import random
from decimal import Decimal
from typing import Any

from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

from synth.entities import Case

W, H = A4
INK = {"blue": (0.08, 0.16, 0.8), "purple": (0.45, 0.1, 0.65), "red": (0.8, 0.1, 0.1)}
FS, LH = 9, 14


def money(v: Decimal) -> str:
    """Indian grouping: 1,25,000.50"""
    i, f = f"{v:.2f}".split(".")
    head, tail = i[:-3], i[-3:]
    groups: list[str] = []
    while len(head) > 2:
        groups.insert(0, head[-2:])
        head = head[:-2]
    if head:
        groups.insert(0, head)
    return ",".join([*groups, tail]) + "." + f


def fdate(x: dt.date) -> str:
    return x.strftime("%d/%m/%Y")


class Doc:
    def __init__(self, title: str, hospital: dict[str, Any]) -> None:
        self.buf = io.BytesIO()
        self.c = canvas.Canvas(
            self.buf, pagesize=A4, invariant=1
        )  # invariant=1: byte-identical output for the same input
        self.title, self.hospital = title, hospital
        self.page, self.y = 1, H - 50
        self.labels: list[dict[str, Any]] = []
        self.stamps: list[dict[str, Any]] = []
        self._header()

    def _header(self) -> None:
        self.c.setFont("Courier-Bold", 12)
        self.c.drawString(40, H - 40, self.hospital["name"].upper())
        self.c.setFont("Courier", FS)
        self.c.drawString(40, H - 54, f"{self.hospital['city']}  Reg No {self.hospital['reg_no']}")
        self.c.setFont("Courier-Bold", 11)
        self.c.drawString(40, H - 74, self.title)
        self.c.setFont("Courier", FS)
        self.y = H - 96

    def text(self, s: str, field: str | None = None, value: Any = None, x: float = 40) -> None:
        if self.y < 120:
            self.new_page()
        self.c.drawString(x, self.y, s)
        if field:
            w = self.c.stringWidth(s, "Courier", FS)
            self.labels.append(
                {
                    "field": field,
                    "value": str(value if value is not None else s),
                    "page": self.page,
                    "bbox": [x, self.y - 2, x + w, self.y + 9],
                    "source_text": s,
                }
            )
        self.y -= LH

    def gap(self, n: int = 1) -> None:
        self.y -= LH * n

    def new_page(self) -> None:
        self.c.showPage()
        self.page += 1
        self.c.setFont("Courier", FS)
        self._header()

    def stamp(self, style: dict[str, str], x: float = 400, y: float = 130) -> None:
        r, g, b = INK[style["ink"]]
        self.c.setStrokeColorRGB(r, g, b)
        self.c.setFillColorRGB(r, g, b)
        self.c.setLineWidth(2)
        name = self.hospital["name"].upper()[:26]
        if style["shape"] == "round":
            self.c.circle(x, y, 45)
            self.c.circle(x, y, 38)
            box = [x - 45, y - 45, x + 45, y + 45]
        else:
            self.c.rect(x - 60, y - 30, 120, 60)
            box = [x - 60, y - 30, x + 60, y + 30]
        self.c.setFont("Courier-Bold", 7)
        self.c.drawCentredString(x, y + 6, name[:20])
        self.c.drawCentredString(x, y - 4, name[20:] or self.hospital["city"].upper())
        self.c.drawCentredString(x, y - 14, "REG " + self.hospital["reg_no"][-5:])
        self.c.setStrokeColorRGB(0, 0, 0)
        self.c.setFillColorRGB(0, 0, 0)
        self.c.setFont("Courier", FS)
        self.stamps.append(
            {"kind": "hospital_stamp", "page": self.page, "bbox": box, "text": name, "style": style}
        )

    def signature(self, x: float = 80, y: float = 120, rng: random.Random | None = None) -> None:
        rng = rng or random.Random(1)  # noqa: S311
        p = self.c.beginPath()
        p.moveTo(x, y)
        for i in range(1, 8):
            p.curveTo(
                x + i * 9 - 4,
                y + rng.randrange(-14, 14),
                x + i * 9,
                y + rng.randrange(-14, 14),
                x + i * 10,
                y + rng.randrange(-6, 6),
            )
        self.c.setStrokeColorRGB(0.1, 0.1, 0.5)
        self.c.drawPath(p, stroke=1, fill=0)
        self.c.setStrokeColorRGB(0, 0, 0)

    def done(self) -> bytes:
        self.c.save()
        return self.buf.getvalue()


def _table(doc: Doc, lines: list[dict[str, Any]]) -> None:
    doc.text(f"{'Description':<34}{'Qty':>5}{'Rate':>13}{'Amount':>14}")
    for ln in lines:
        s = f"{ln['description'][:33]:<34}{ln['qty']:>5}{money(ln['unit_price']):>13}{money(ln['amount']):>14}"
        doc.text(s, "bill_line", f"{ln['description']}|{ln['amount']}")


def final_bill(
    case: Case,
    lines: list[dict[str, Any]],
    totals: dict[str, Decimal],
    *,
    stamp: bool,
    printed_total: Decimal | None = None,
    bill_no: str = "",
) -> tuple[bytes, dict[str, Any]]:
    doc = Doc("FINAL BILL", case.hospital)
    bill_no = bill_no or f"FB-2026-{case.seed % 100000:05d}"
    doc.text(f"Bill No: {bill_no}        Date: {fdate(case.discharged_on)}", "bill_number", bill_no)
    doc.text(
        f"Patient Name: {case.member.full_name}     UHID: {case.member.uhid}",
        "patient_name",
        case.member.full_name,
    )
    doc.text(
        f"Date of Admission: {fdate(case.admitted_on)}    Date of Discharge: {fdate(case.discharged_on)}",
        "admitted_on",
        fdate(case.admitted_on),
    )
    doc.text(f"Treating Doctor: {case.doctor}")
    doc.gap()
    _table(doc, lines)
    pt = printed_total if printed_total is not None else totals["gross"]
    doc.text(f"{'Total':<52}{money(pt):>14}", "total", money(pt))
    doc.text(
        f"{'Discount':<52}{money(totals['discounts']):>14}", "discounts", money(totals["discounts"])
    )
    doc.text(f"{'Net amount':<52}{money(totals['claimed']):>14}")
    if stamp:
        doc.stamp(case.hospital["stamp"])
    doc.signature()
    return doc.done(), {"labels": doc.labels, "stamps": doc.stamps, "pages": doc.page}


def pharmacy_bill(
    case: Case,
    lines: list[dict[str, Any]],
    total: Decimal,
    *,
    stamp: bool,
    printed_total: Decimal | None = None,
) -> tuple[bytes, dict[str, Any]]:
    doc = Doc("PHARMACY BILL", case.hospital)
    doc.text(
        f"Bill No: PH-{case.seed % 100000:05d}        Date: {fdate(case.discharged_on)}",
        "bill_number",
        f"PH-{case.seed % 100000:05d}",
    )
    doc.text(f"Patient Name: {case.member.full_name}", "patient_name", case.member.full_name)
    doc.text("Drug Licence No: DL-FAKE-12345   GST: 27FAKE0000A1Z5")
    doc.gap()
    _table(doc, lines)
    pt = printed_total if printed_total is not None else total
    doc.text(f"{'Total':<52}{money(pt):>14}", "total", money(pt))
    if stamp:
        doc.stamp(case.hospital["stamp"])
    return doc.done(), {"labels": doc.labels, "stamps": doc.stamps, "pages": doc.page}


def prescription(case: Case, meds: list[str], free_text: str = "") -> tuple[bytes, dict[str, Any]]:
    doc = Doc("PRESCRIPTION", case.hospital)
    doc.text(f"Date: {fdate(case.admitted_on)}", "date", fdate(case.admitted_on))
    doc.text(
        f"Patient Name: {case.member.full_name}   Age/Sex: {case.admitted_on.year - case.member.dob.year}/{case.member.gender}",
        "patient_name",
        case.member.full_name,
    )
    doc.text("Rx")
    for i, m in enumerate(meds):
        doc.text(f"{i + 1}. {m}   1-0-1   5 days")
    if free_text:
        doc.gap()
        for ln in free_text.splitlines():
            doc.text(ln)
    doc.gap(2)
    doc.text(f"{case.doctor}", "doctor_name", case.doctor)
    doc.signature(x=300, y=doc.y + 10)
    return doc.done(), {"labels": doc.labels, "stamps": doc.stamps, "pages": doc.page}


def discharge_summary(case: Case, *, stamp: bool) -> tuple[bytes, dict[str, Any]]:
    doc = Doc("DISCHARGE SUMMARY", case.hospital)
    doc.text(
        f"Patient Name: {case.member.full_name}     UHID: {case.member.uhid}",
        "patient_name",
        case.member.full_name,
    )
    doc.text(
        f"Date of Admission: {fdate(case.admitted_on)}", "admitted_on", fdate(case.admitted_on)
    )
    doc.text(
        f"Date of Discharge: {fdate(case.discharged_on)}",
        "discharged_on",
        fdate(case.discharged_on),
    )
    doc.text(
        f"Diagnosis: {case.procedure['code'].replace('_', ' ')}  ICD-10: {', '.join(case.procedure['dx'])}",
        "icd_codes",
        ",".join(case.procedure["dx"]),
    )
    doc.text("Condition at discharge: stable")
    doc.text("Advice on discharge: rest, review after 7 days")
    doc.text(f"Treating Doctor: {case.doctor}", "doctor_name", case.doctor)
    if stamp:
        doc.stamp(case.hospital["stamp"])
    return doc.done(), {"labels": doc.labels, "stamps": doc.stamps, "pages": doc.page}


def implant_sticker(case: Case) -> tuple[bytes, dict[str, Any]]:
    doc = Doc("IMPLANT STICKER", case.hospital)
    doc.text(
        f"Implant: {case.procedure['code'].replace('_', ' ')} system   Manufacturer: FAKEMED Ltd"
    )
    doc.text(
        f"Serial No: SN{case.seed % 10**8:08d}   Lot No: LT{case.seed % 10**6:06d}   MRP: Rs. 45,000.00",
        "serial_no",
        f"SN{case.seed % 10**8:08d}",
    )
    doc.text(f"Procedure date: {fdate(case.admitted_on)}")
    return doc.done(), {"labels": doc.labels, "stamps": doc.stamps, "pages": doc.page}


INSURER = {"name": "Niveshak Health Insurance (synthetic)", "city": "Mumbai", "reg_no": "IRDAI-SYN-000"}


def policy_card(case: Case) -> tuple[bytes, dict[str, Any]]:
    """The member's health card: what the desk copies at admission. Product and sum insured feed the hospital's estimate."""
    pol = case.policy
    doc = Doc("HEALTH POLICY CARD", INSURER)
    doc.text(f"Name: {case.member.full_name}", "name", case.member.full_name)
    doc.text(f"Member ID: {case.member.member_id}", "member_id", case.member.member_id)
    doc.text(f"Policy No: {case.member.policy_number}", "policy_number", case.member.policy_number)
    doc.text(f"Insurer: {INSURER['name']}", "insurer_name", INSURER["name"])
    doc.text(f"Product: {pol['product_code']}", "product_name", pol["product_code"])
    doc.text(f"Sum Insured: Rs. {money(pol['sum_insured'])}", "sum_insured", money(pol["sum_insured"]))
    doc.text(f"Valid from: {fdate(pol['valid_from'])}", "valid_from", fdate(pol["valid_from"]))
    doc.text(f"Valid upto: {fdate(pol['valid_to'])}", "valid_to", fdate(pol["valid_to"]))
    return doc.done(), {"labels": doc.labels, "stamps": doc.stamps, "pages": doc.page}
