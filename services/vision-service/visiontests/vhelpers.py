import io

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

MONO = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"
BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
W, H = 1240, 1754  # A4 at 150 dpi


def bill_page(
    stamp: bool = True, text: str = "CITY CARE HOSPITAL", extra: str = "REG NO 12345"
) -> Image.Image:
    im = Image.new("RGB", (W, H), "white")
    d = ImageDraw.Draw(im)
    f = ImageFont.truetype(MONO, 24)
    d.text(
        (80, 60),
        "CITY CARE HOSPITAL  -  FINAL BILL",
        fill="black",
        font=ImageFont.truetype(BOLD, 34),
    )
    for i in range(34):
        d.text(
            (80, 140 + i * 36),
            f"Item {i:02d} consultation and ward charges        {i + 1}   {250 * (i + 1):>9,}.00",
            fill="black",
            font=f,
        )
    d.text((80, 1400), "Authorised signatory", fill="black", font=f)
    if stamp:
        cx, cy, r = 900, 1500, 120
        d.ellipse((cx - r, cy - r, cx + r, cy + r), outline=(20, 40, 200), width=7)
        d.ellipse(
            (cx - r + 18, cy - r + 18, cx + r - 18, cy + r - 18), outline=(20, 40, 200), width=3
        )
        sf = ImageFont.truetype(BOLD, 22)
        for k, ln in enumerate([text, extra]):
            w = d.textlength(ln, font=sf)
            d.text((cx - w / 2, cy - 22 + k * 30), ln, fill=(20, 40, 200), font=sf)
    return im


def arr(im: Image.Image) -> np.ndarray:
    return np.asarray(im.convert("RGB"))


def blurry(im: Image.Image) -> Image.Image:
    return im.filter(ImageFilter.GaussianBlur(9))


def png(im: Image.Image) -> bytes:
    b = io.BytesIO()
    im.save(b, "PNG")
    return b.getvalue()
