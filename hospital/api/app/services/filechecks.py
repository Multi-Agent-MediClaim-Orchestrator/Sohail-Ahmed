"""File validation helpers: magic-byte sniffing, filename sanitising, PDF and image checks."""

from __future__ import annotations

import io
import re
import unicodedata

from app.core.errors import ApiError

ALLOWED = {
    "application/pdf": ("pdf",),
    "image/jpeg": ("jpg", "jpeg"),
    "image/png": ("png",),
    "image/tiff": ("tif", "tiff"),
}
EXT_FOR = {"application/pdf": "pdf", "image/jpeg": "jpg", "image/png": "png", "image/tiff": "tiff"}


def sniff(head: bytes) -> str | None:
    if head.startswith(b"%PDF-"):
        return "application/pdf"
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head[:4] in (b"II*\x00", b"MM\x00*"):
        return "image/tiff"
    return None


def ext_matches(mime: str, filename: str) -> bool:
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    return ext in ALLOWED.get(mime, ())


_BAD = re.compile(r"[\x00-\x1f\x7f‪-‮⁦-⁩]")


def sanitize_filename(name: str) -> str:
    """Display-only name: NFKC, no path parts, no control or bidi-override characters, max 120 chars."""
    n = unicodedata.normalize("NFKC", name or "")
    n = n.replace("\\", "/").rsplit("/", 1)[-1]
    n = _BAD.sub("", n).strip(" .")
    n = re.sub(r"\.{2,}", ".", n[:120])  # collapse dot runs ("..." must not leave ".." behind)
    return n.strip(" .") or "file"


def check_pdf(data: bytes, max_pages: int) -> int:
    """Return page count; raise ApiError for encrypted, active-content, corrupt or too-long PDFs."""
    import pikepdf

    try:
        pdf = pikepdf.open(io.BytesIO(data))
    except pikepdf.PasswordError:
        raise ApiError(
            "encrypted_pdf", "the PDF is password protected; please upload an unlocked copy"
        ) from None
    except pikepdf.PdfError:
        raise ApiError("corrupt_file", "the PDF could not be read") from None
    with pdf:
        root = pdf.Root
        names = root.get("/Names")
        if "/OpenAction" in root and "/JS" in repr(root.OpenAction) or "/AA" in root:
            raise ApiError("active_content", "PDF contains scripts")
        if names is not None and ("/JavaScript" in names or "/EmbeddedFiles" in names):
            raise ApiError("active_content", "PDF contains scripts or embedded files")
        for page in pdf.pages:
            for annot in page.get("/Annots", []):
                if "/JS" in annot or "/A" in annot and "/JS" in annot.A:
                    raise ApiError("active_content", "PDF annotation contains script")
        n = len(pdf.pages)
    if n > max_pages:
        raise ApiError("file_too_large", f"PDF has {n} pages (max {max_pages})")
    if n < 1:
        raise ApiError("corrupt_file", "PDF has no pages")
    return n


def normalise_image(data: bytes, mime: str) -> tuple[bytes, int]:
    """Reject decompression bombs/corrupt images; strip EXIF GPS when present (bytes untouched otherwise)."""
    from PIL import Image

    Image.MAX_IMAGE_PIXELS = 100_000_000
    try:
        with Image.open(io.BytesIO(data)) as im:
            im.verify()
        with Image.open(io.BytesIO(data)) as im:
            pages = getattr(im, "n_frames", 1)
            exif = im.getexif()
            if 0x8825 not in exif:  # no GPS IFD: keep original bytes (hash/identity preserved)
                return data, pages
            del exif[0x8825]
            out = io.BytesIO()
            fmt = {"image/jpeg": "JPEG", "image/png": "PNG", "image/tiff": "TIFF"}[mime]
            kw = {"quality": 95} if fmt == "JPEG" else {}
            im.save(out, format=fmt, exif=exif, **kw)
            return out.getvalue(), pages
    except Image.DecompressionBombError:
        raise ApiError("image_too_large", "image dimensions exceed the limit") from None
    except (OSError, SyntaxError, ValueError, Image.UnidentifiedImageError):
        raise ApiError("corrupt_file", "the image could not be read") from None
