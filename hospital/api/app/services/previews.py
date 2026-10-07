"""Page previews for the viewer: PNG per page, rendered on first request (pdftoppm for PDFs, Pillow for images) and cached
in the object store next to the document."""

from __future__ import annotations

import asyncio
import io
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from PIL import Image

from app.core.errors import ApiError

DPI = 150


def _render(raw: bytes, mime: str, n: int) -> bytes:
    if mime == "application/pdf":
        with tempfile.TemporaryDirectory() as d:
            src = Path(d) / "in.pdf"
            src.write_bytes(raw)
            subprocess.run(  # noqa: S603
                [
                    "pdftoppm",
                    "-r",
                    str(DPI),
                    "-png",
                    "-f",
                    str(n),
                    "-l",
                    str(n),
                    str(src),
                    str(Path(d) / "pg"),
                ],  # noqa: S607
                check=True,
                capture_output=True,
                timeout=60,
            )
            files = sorted(Path(d).glob("pg*.png"))
            if not files:
                raise ValueError("page out of range")
            return files[0].read_bytes()
    if n != 1:
        raise ValueError("page out of range")
    buf = io.BytesIO()
    Image.open(io.BytesIO(raw)).convert("RGB").save(buf, "PNG")
    return buf.getvalue()


async def ensure(d: Any, row: Any, n: int, key: str) -> None:
    if n < 1 or (row.pages and n > row.pages):
        raise ApiError("not_found", "no such page")
    if row.lifecycle != "active" or row.scan_status != "clean" or not row.storage_key:
        raise ApiError("not_found", "document not available")
    if await d.store.exists(key):
        return
    raw = await d.store.get(row.storage_key)
    try:
        png = await asyncio.to_thread(_render, raw, row.mime_type, n)
    except (ValueError, subprocess.SubprocessError):
        raise ApiError("not_found", "no such page") from None
    await d.store.put(key, png, "image/png")
