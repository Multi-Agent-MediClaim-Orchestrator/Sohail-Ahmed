"""Hard stamp pages with exact box labels, for measuring and training stamp detectors (doc 03 task 6, scaled down).

Each page is a synthetic bill-like page (text lines, rules, distractors) with 0-2 hospital stamps/seals and sometimes a
signature. Labels come from the alpha channel of the stamp layer, so boxes are exact after rotation. Difficulty:
  easy   upright, saturated ink, no damage
  medium rotation, translucent ink, noise, blur, two stamps
  hard   photocopy (grey ink), faint ink, overlap with text, heavy JPEG, partial crop, ink-coloured distractors
"""

from __future__ import annotations

import io
import random
from typing import Any

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

W, H = 1240, 1754
MONO = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"
BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
INKS = {
    "blue": (20, 40, 200),
    "purple": (110, 25, 165),
    "red": (200, 30, 30),
    "green": (20, 120, 60),
    "black": (30, 30, 30),
}
NAMES = [
    "CITY CARE HOSPITAL",
    "LAKEVIEW MULTISPECIALITY",
    "SUNRISE MEDICAL CENTRE",
    "GREEN VALLEY CLINIC",
    "APEX HEART INSTITUTE",
    "RIVERSIDE GENERAL HOSPITAL",
]
DIFFICULTIES = ("easy", "medium", "hard")


def _text_page(rng: random.Random) -> Image.Image:
    im = Image.new("RGB", (W, H), "white")
    d = ImageDraw.Draw(im)
    f = ImageFont.truetype(MONO, rng.choice([22, 24, 26, 28]))
    d.text(
        (80, 60),
        rng.choice(NAMES) + "  -  FINAL BILL",
        fill="black",
        font=ImageFont.truetype(BOLD, 32),
    )
    for i in range(rng.randrange(14, 38)):
        d.text(
            (80, 140 + i * 38),
            f"Item {i:02d} consultation and ward charges     {rng.randrange(1, 9)}  {rng.randrange(100, 90000):>9,}.00",
            fill="black",
            font=f,
        )
    return im


def _stamp_layer(
    rng: random.Random, kind: str, ink: tuple[int, int, int], scale: float
) -> Image.Image:
    size = int(300 * scale)
    layer = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    c = (*ink, 255)
    name = rng.choice(NAMES)
    sf = ImageFont.truetype(BOLD, max(12, int(20 * scale)))
    if kind == "round":
        m = int(10 * scale)
        d.ellipse((m, m, size - m, size - m), outline=c, width=max(3, int(7 * scale)))
        d.ellipse(
            (
                m + int(20 * scale),
                m + int(20 * scale),
                size - m - int(20 * scale),
                size - m - int(20 * scale),
            ),
            outline=c,
            width=max(2, int(3 * scale)),
        )
        lines = [name[:14], name[14:28] or "HOSPITAL", "REG " + str(rng.randrange(10000, 99999))]
    elif kind == "oval":
        d.ellipse(
            (int(10 * scale), int(60 * scale), size - int(10 * scale), size - int(60 * scale)),
            outline=c,
            width=max(3, int(6 * scale)),
        )
        lines = [name[:16], "REG " + str(rng.randrange(10000, 99999))]
    else:  # rect
        d.rectangle(
            (int(10 * scale), int(50 * scale), size - int(10 * scale), size - int(50 * scale)),
            outline=c,
            width=max(3, int(6 * scale)),
        )
        lines = [name[:16], name[16:32] or "CITY", "REG " + str(rng.randrange(10000, 99999))]
    y = size // 2 - len(lines) * int(14 * scale)
    for ln in lines:
        w = d.textlength(ln, font=sf)
        d.text(((size - w) / 2, y), ln, fill=c, font=sf)
        y += int(28 * scale)
    return layer


def _signature_layer(rng: random.Random, ink: tuple[int, int, int]) -> Image.Image:
    layer = Image.new("RGBA", (260, 110), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    pts = [(10, 55)]
    for i in range(1, 9):
        pts.append((10 + i * 28, 55 + rng.randrange(-35, 35)))
    d.line(pts, fill=(*ink, 255), width=4, joint="curve")
    return layer


def _paste(
    page: Image.Image,
    layer: Image.Image,
    rng: random.Random,
    angle: float,
    alpha: float,
    overlap_text: bool,
) -> list[int] | None:
    layer = layer.rotate(angle, expand=True, resample=Image.BICUBIC)
    a = layer.split()[3].point(lambda v: int(v * alpha))
    layer.putalpha(a)
    bbox = layer.split()[3].point(lambda v: 255 if v > 20 else 0).getbbox()
    if bbox is None:
        return None
    x = rng.randrange(40, W - layer.width - 40)
    y = (
        rng.randrange(120, H - layer.height - 60)
        if overlap_text
        else rng.randrange(int(H * 0.6), H - layer.height - 40)
    )
    page.paste(layer, (x, y), layer)
    return [x + bbox[0], y + bbox[1], x + bbox[2], y + bbox[3]]


def _distractors(page: Image.Image, rng: random.Random, hard: bool) -> None:
    d = ImageDraw.Draw(page, "RGBA")
    if hard or rng.random() < 0.4:
        d.rectangle((60, 100, W - 60, 118), fill=(20, 40, 200, 255))  # coloured header bar
    for _ in range(
        rng.randrange(0, 4 if hard else 2)
    ):  # ink-coloured text, underlines, scribbles that are not stamps
        x, y = rng.randrange(80, W - 400), rng.randrange(200, H - 200)
        d.text(
            (x, y),
            rng.choice(["PAID", "Dr. Verma", "Ref 8821", "Approved"]),
            fill=rng.choice([(20, 40, 200, 255), (200, 30, 30, 255)]),
            font=ImageFont.truetype(BOLD, rng.choice([24, 30])),
        )
        d.line((x, y + 34, x + 140, y + 34), fill=(20, 40, 200, 255), width=3)
    if hard and rng.random() < 0.6:  # a filled blue logo disc: round and inky, but not a stamp
        x, y = rng.randrange(80, W - 200), rng.randrange(140, 400)
        d.ellipse((x, y, x + 70, y + 70), fill=(20, 40, 200, 255))


def make_page(rng: random.Random, difficulty: str) -> tuple[Image.Image, list[dict[str, Any]]]:
    page = _text_page(rng)
    _distractors(page, rng, difficulty == "hard")
    labels: list[dict[str, Any]] = []
    n = rng.choices(
        [0, 1, 2], weights=[0.25, 0.55, 0.20] if difficulty != "easy" else [0.3, 0.7, 0.0]
    )[0]
    for _ in range(n):
        kind = rng.choice(["round", "rect", "oval"])
        ink = INKS[
            rng.choice(
                ["blue", "purple", "red"]
                if difficulty != "hard"
                else ["blue", "purple", "red", "green", "black"]
            )
        ]
        if difficulty == "easy":
            angle, alpha, scale = 0.0, rng.uniform(0.9, 1.0), rng.uniform(0.7, 1.0)
        elif difficulty == "medium":
            angle, alpha, scale = rng.uniform(-25, 25), rng.uniform(0.5, 0.9), rng.uniform(0.6, 1.2)
        else:
            angle, alpha, scale = (
                rng.uniform(-30, 30),
                rng.uniform(0.25, 0.7),
                rng.uniform(0.5, 1.2),
            )
        box = _paste(
            page,
            _stamp_layer(rng, kind, ink, scale),
            rng,
            angle,
            alpha,
            overlap_text=difficulty == "hard",
        )
        if box:
            labels.append({"kind": "hospital_stamp", "shape": kind, "bbox": box})
    if rng.random() < 0.35:
        box = _paste(
            page,
            _signature_layer(rng, INKS["blue"]),
            rng,
            rng.uniform(-8, 8),
            rng.uniform(0.7, 1.0),
            overlap_text=False,
        )
        if box:
            labels.append({"kind": "signature", "bbox": box})
    if difficulty != "easy":
        a = np.asarray(page).astype(np.float32)
        a += np.random.default_rng(rng.randrange(2**31)).normal(0, rng.uniform(2, 9), a.shape)
        page = Image.fromarray(np.clip(a, 0, 255).astype(np.uint8))
        page = page.filter(ImageFilter.GaussianBlur(rng.uniform(0.3, 1.1)))
    if difficulty == "hard":
        if rng.random() < 0.5:  # photocopy: grey scale, hard threshold, no colour left
            g = page.convert("L").point(lambda v: 0 if v < rng.randrange(150, 200) else 255)
            page = Image.merge("RGB", (g, g, g))
        b = io.BytesIO()
        page.save(b, "JPEG", quality=rng.randrange(25, 60))
        page = Image.open(io.BytesIO(b.getvalue())).convert("RGB")
    # a label for a stamp that photocopying or JPEG wiped out would be unfair: keep only stamps still visible on the final page
    gray = np.asarray(page.convert("L")).astype(np.float32)
    visible = []
    for lab in labels:
        x0, y0, x1, y1 = lab["bbox"]
        if (gray[y0:y1, x0:x1] < 200).mean() >= 0.012:
            visible.append(lab)
    return page, visible


def build_set(
    n: int, seed: int, difficulties: tuple[str, ...] = DIFFICULTIES
) -> list[tuple[Image.Image, list[dict[str, Any]], str]]:
    out = []
    for i in range(n):
        rng = random.Random(seed * 100003 + i)
        diff = difficulties[i % len(difficulties)]
        page, labels = make_page(rng, diff)
        out.append((page, labels, diff))
    return out
