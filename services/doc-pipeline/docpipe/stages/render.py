"""S1-S2: turn bytes into per-page text with a confidence. Text layer first (deterministic, 0.99); OCR for scans.
Backends: pdftotext (text layer), tesseract (OCR), MinerU (layout-aware, optional: used when `mineru` is installed
and DOCPIPE_PARSER=mineru|auto)."""

from __future__ import annotations

import io
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image, ImageOps


class ParseError(Exception):
    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}")
        self.code, self.detail = code, detail


@dataclass
class Page:
    n: int
    text: str
    conf: float
    source: str  # textlayer | tesseract | mineru
    png: bytes | None = None


@dataclass
class Rendered:
    pages: list[Page]
    kind: str
    engine: str
    engine_version: str = ""
    notes: list[str] = field(default_factory=list)


def sniff(raw: bytes) -> str:
    if raw[:5] == b"%PDF-":
        return "pdf"
    if raw[:3] == b"\xff\xd8\xff":
        return "jpeg"
    if raw[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if raw[:4] in (b"II*\x00", b"MM\x00*"):
        return "tiff"
    raise ParseError("unsupported_media_type", "not a PDF, JPEG, PNG or TIFF")


def _run(cmd: list[str], timeout: int = 120) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(cmd, capture_output=True, timeout=timeout, check=False)  # noqa: S603


def printable_ratio(t: str) -> float:
    return sum(c.isprintable() or c in "\n\t" for c in t) / len(t) if t else 0.0


def good_text_layer(t: str, min_chars: int) -> bool:
    words = re.findall(r"[A-Za-z]{3,}", t)
    return len(t.strip()) >= min_chars and printable_ratio(t) >= 0.95 and len(words) >= 20


def _tesseract(img_path: Path) -> tuple[str, float]:
    r = _run(["tesseract", str(img_path), "stdout", "--psm", "6", "tsv"], 180)
    if r.returncode != 0:
        raise ParseError("ocr_failed", r.stderr.decode(errors="ignore")[:200])
    lines: dict[tuple[str, str, str], list[str]] = {}
    confs: list[float] = []
    for row in r.stdout.decode(errors="ignore").splitlines()[1:]:
        c = row.split("\t")
        if len(c) < 12 or not c[11].strip() or float(c[10]) < 0:
            continue
        lines.setdefault((c[2], c[3], c[4]), []).append(c[11])
        confs.append(float(c[10]) / 100)
    text = "\n".join(
        " ".join(w) for _, w in sorted(lines.items(), key=lambda kv: tuple(int(x) for x in kv[0]))
    )
    return text, (sum(confs) / len(confs) if confs else 0.0)


def _png(path: Path) -> bytes:
    return path.read_bytes()


def _mineru(raw: bytes, suffix: str) -> Rendered | None:
    exe = shutil.which("mineru")
    if not exe:
        return None
    with tempfile.TemporaryDirectory() as d:
        src = Path(d) / f"in{suffix}"
        src.write_bytes(raw)
        r = _run([exe, "-p", str(src), "-o", d, "-b", "pipeline"], 900)
        mds = sorted(Path(d).rglob("*.md"))
        if r.returncode != 0 or not mds:
            return None
        text = mds[0].read_text()
    parts = [p for p in re.split(r"\n-{3,}\n|\f", text) if p.strip()] or [text]
    return Rendered(
        [Page(i + 1, p, 0.93, "mineru") for i, p in enumerate(parts)],
        "pdf" if suffix == ".pdf" else "image",
        "mineru",
    )


def render(raw: bytes, parser: str = "auto", max_pages: int = 30, min_chars: int = 200) -> Rendered:
    kind = sniff(raw)
    if parser in ("auto", "mineru"):
        m = _mineru(raw, ".pdf" if kind == "pdf" else ".png")
        if m is not None:
            return m
        if parser == "mineru":
            raise ParseError("parser_unavailable", "MinerU is not installed")
    with tempfile.TemporaryDirectory() as d:
        dd = Path(d)
        if kind == "pdf":
            src = dd / "in.pdf"
            src.write_bytes(raw)
            info = _run(["pdfinfo", str(src)]).stdout.decode(errors="ignore")
            if "Encrypted:       yes" in info:
                raise ParseError("encrypted_pdf", "document is password protected")
            m = re.search(r"Pages:\s+(\d+)", info)
            n = int(m.group(1)) if m else 0
            if n == 0:
                raise ParseError("corrupt_file", "no pages")
            if n > max_pages:
                raise ParseError("too_many_pages", f"{n} pages")
            texts: list[str] = []
            for i in range(1, n + 1):
                t = _run(
                    ["pdftotext", "-layout", "-f", str(i), "-l", str(i), str(src), "-"]
                ).stdout.decode(errors="ignore")
                texts.append(t)
            pages: list[Page] = []
            for i, t in enumerate(texts, 1):
                if parser != "tesseract" and good_text_layer(t, min_chars):
                    pages.append(Page(i, t, 0.99, "textlayer"))
                    continue
                _run(
                    [
                        "pdftoppm",
                        "-r",
                        "200",
                        "-png",
                        "-f",
                        str(i),
                        "-l",
                        str(i),
                        str(src),
                        str(dd / f"p{i}"),
                    ]
                )
                img = next(iter(sorted(dd.glob(f"p{i}*.png"))), None)
                if img is None:
                    raise ParseError("corrupt_file", f"cannot render page {i}")
                ot, conf = _tesseract(img)
                pages.append(Page(i, ot, round(conf, 3), "tesseract", _png(img)))
            eng = "pdftotext" if all(p.source == "textlayer" for p in pages) else "tesseract"
            return Rendered(pages, "pdf", eng)
        try:
            im = ImageOps.exif_transpose(Image.open(io.BytesIO(raw))).convert("RGB")
        except Exception as e:  # noqa: BLE001
            raise ParseError("corrupt_file", type(e).__name__) from e
        path = dd / "in.png"
        im.save(path)
        ot, conf = _tesseract(path)
        return Rendered(
            [Page(1, ot, round(conf, 3), "tesseract", _png(path))], "image", "tesseract"
        )
