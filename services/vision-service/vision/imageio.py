"""Load page images from bytes (PNG/JPEG/TIFF, or the pages of a PDF via pdftoppm)."""

from __future__ import annotations

import io
import subprocess
import tempfile
from pathlib import Path

import numpy as np
from PIL import Image, ImageOps


class ImageError(Exception):
    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}")
        self.code, self.detail = code, detail


def load_pages(raw: bytes, max_pages: int = 20, max_mp: int = 40) -> list[np.ndarray]:
    """RGB uint8 arrays, one per page."""
    if raw[:5] == b"%PDF-":
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "in.pdf"
            p.write_bytes(raw)
            r = subprocess.run(
                [
                    "pdftoppm",
                    "-r",
                    "150",
                    "-png",
                    "-l",
                    str(max_pages),
                    str(p),
                    str(Path(d) / "pg"),
                ],
                capture_output=True,
                timeout=120,
                check=False,
            )  # noqa: S603, S607
            files = sorted(Path(d).glob("pg*.png"))
            if r.returncode != 0 or not files:
                raise ImageError("unsupported_image", "cannot render the PDF")
            return [_open(f.read_bytes(), max_mp) for f in files]
    return [_open(raw, max_mp)]


def _open(b: bytes, max_mp: int) -> np.ndarray:
    try:
        im: Image.Image = Image.open(io.BytesIO(b))
        im = ImageOps.exif_transpose(im).convert("RGB")
    except Exception as e:  # noqa: BLE001
        raise ImageError("unsupported_image", type(e).__name__) from e
    if im.width * im.height > max_mp * 1_000_000:
        raise ImageError("image_too_large", f"{im.width}x{im.height}")
    return np.asarray(im)
