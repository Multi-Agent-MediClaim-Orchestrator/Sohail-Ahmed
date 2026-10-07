"""Degradation pass: rasterise, damage, re-wrap as image-only PDF (no text layer, forcing OCR)."""

from __future__ import annotations

import io
import subprocess
import tempfile
from pathlib import Path

import cv2
import numpy as np
from PIL import Image


def rasterise(pdf: bytes, dpi: int = 150) -> list[Image.Image]:
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "x.pdf"
        p.write_bytes(pdf)
        subprocess.run(
            ["pdftoppm", "-r", str(dpi), "-png", str(p), str(Path(d) / "pg")],
            check=True,
            capture_output=True,
        )  # noqa: S603, S607
        return [Image.open(f).convert("RGB") for f in sorted(Path(d).glob("pg*.png"))]


def apply(
    img: Image.Image,
    *,
    blur: float = 0,
    dpi: int | None = None,
    skew: float = 0,
    jpeg_q: int | None = None,
    crop_pct: float = 0,
) -> Image.Image:
    a = np.asarray(img)
    if dpi and dpi < 150:  # simulate a low-resolution scan
        f = dpi / 150
        a = cv2.resize(
            cv2.resize(a, None, fx=f, fy=f, interpolation=cv2.INTER_AREA),
            (a.shape[1], a.shape[0]),
            interpolation=cv2.INTER_LINEAR,
        )
    if blur:
        a = cv2.GaussianBlur(a, (0, 0), blur)
    if skew:
        h, w = a.shape[:2]
        m = cv2.getRotationMatrix2D((w / 2, h / 2), skew, 1.0)
        a = cv2.warpAffine(a, m, (w, h), borderValue=(255, 255, 255))
    if crop_pct:
        w = a.shape[1]
        a = a[:, int(w * crop_pct) :]
    out = Image.fromarray(a)
    if jpeg_q:
        b = io.BytesIO()
        out.save(b, "JPEG", quality=jpeg_q)
        out = Image.open(io.BytesIO(b.getvalue())).convert("RGB")
    return out


def quality_class(
    *, blur: float = 0, dpi: int | None = None, skew: float = 0, crop_pct: float = 0
) -> str:
    """Deterministic rule (doc 05-01 §4.4): the label evaluation scores against."""
    if crop_pct >= 0.05:
        return "unreadable"
    if blur > 2.2 or (dpi is not None and dpi < 90) or abs(skew) > 8:
        return "poor"
    if blur > 0 or (dpi is not None and dpi < 150) or skew:
        return "acceptable"
    return "good"


def to_pdf(images: list[Image.Image]) -> bytes:
    b = io.BytesIO()
    images[0].save(b, "PDF", save_all=True, append_images=images[1:], resolution=150.0)
    return b.getvalue()
