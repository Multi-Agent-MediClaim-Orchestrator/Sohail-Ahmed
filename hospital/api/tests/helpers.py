"""Test data builders: files, EICAR, cases."""

import io
from typing import Any

import httpx
import pikepdf
from PIL import Image

EICAR = b"X5O!P%@AP[4\\PZX54(P^)7CC)7}$EICAR-STANDARD-ANTIVIRUS-TEST-FILE!$H+H*"  # built in source, never a file


def pdf_bytes(pages: int = 1, *, marker: str = "") -> bytes:
    pdf = pikepdf.new()
    for _ in range(pages):
        pdf.add_blank_page(page_size=(200, 200))
    if marker:
        pdf.docinfo["/Title"] = marker  # makes the bytes (and sha256) unique
    buf = io.BytesIO()
    pdf.save(buf)
    return buf.getvalue()


def encrypted_pdf() -> bytes:
    pdf = pikepdf.new()
    pdf.add_blank_page()
    buf = io.BytesIO()
    pdf.save(buf, encryption=pikepdf.Encryption(user="user-pw", owner="owner-pw"))
    return buf.getvalue()


def js_pdf() -> bytes:
    pdf = pikepdf.new()
    pdf.add_blank_page()
    pdf.Root.OpenAction = pikepdf.Dictionary(
        S=pikepdf.Name.JavaScript, JS=pikepdf.String("app.alert(1)")
    )
    buf = io.BytesIO()
    pdf.save(buf)
    return buf.getvalue()


def jpeg_bytes(
    *, gps: bool = False, size: int = 64, color: tuple[int, int, int] = (200, 10, 10)
) -> bytes:
    im = Image.new("RGB", (size, size), color)
    buf = io.BytesIO()
    if gps:
        exif = Image.Exif()
        exif[0x8825] = {1: "N", 2: (12.0, 58.0, 0.0)}
        im.save(buf, format="JPEG", exif=exif)
    else:
        im.save(buf, format="JPEG")
    return buf.getvalue()


def case_body(uhid: str = "UH-1", **over: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "patient": {
            "uhid": uhid,
            "full_name": "Ravi Kumar",
            "dob": "1984-03-12",
            "gender": "M",
            "phone": "+919800000000",
        },
        "policy": {
            "insurer_name": "Acme Health",
            "policy_number": "AH-993201",
            "member_id": "M-77123",
        },
        "claim_type": "cashless",
        "admission_type": "planned",
        "admitted_on": "2026-09-28",
        "discharged_on": "2026-10-02",
        "preauth_ref": "PA-2026-33001",
        "treating_doctor": "Dr. Rao",
    }
    body.update(over)
    return body


async def new_case(
    client: httpx.AsyncClient, headers: dict[str, str], uhid: str, **over: Any
) -> dict[str, Any]:
    r = await client.post("/v1/cases", json=case_body(uhid, **over), headers=headers)
    assert r.status_code == 201, r.text
    return r.json()  # type: ignore[no-any-return]


def files(*items: tuple[str, bytes, str]) -> list[tuple[str, tuple[str, bytes, str]]]:
    return [("files", it) for it in items]
