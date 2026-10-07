"""Classical stamp/seal/signature detection (doc 03 task 5) + OCR of the crop (tesseract) + registry match.
No trained detector is shipped (see DECISIONS): this is the deterministic path with honest confidences."""

from __future__ import annotations

import math
import re
import shutil
import subprocess
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path

import cv2
import numpy as np
from rapidfuzz import fuzz


@dataclass
class Stamp:
    page: int
    bbox: tuple[int, int, int, int]
    kind: str  # hospital_stamp | doctor_stamp | signature | seal
    detector: str
    det_conf: float
    ocr_text: str | None = None
    ocr_conf: float | None = None
    matches_registry: bool | None = None
    registry_hospital: str | None = None
    registry_score: float | None = None
    ink_color: str | None = None

    def dict(self) -> dict:  # type: ignore[type-arg]
        return asdict(self)


def _ink_masks(rgb: np.ndarray) -> dict[str, np.ndarray]:
    hsv = cv2.cvtColor(rgb, cv2.COLOR_RGB2HSV)
    h, s, v = hsv[..., 0], hsv[..., 1], hsv[..., 2]
    return {
        "blue": ((h >= 95) & (h <= 135) & (s > 60) & (v > 50)).astype(np.uint8) * 255,
        "purple": ((h > 135) & (h <= 160) & (s > 60) & (v > 50)).astype(np.uint8) * 255,
        "red": (((h <= 10) | (h >= 170)) & (s > 80) & (v > 50)).astype(np.uint8) * 255,
    }


def detect_classical(rgb: np.ndarray, page: int) -> list[Stamp]:
    H, W = rgb.shape[:2]
    out: list[Stamp] = []
    for color, m in _ink_masks(rgb).items():
        m = cv2.morphologyEx(
            m, cv2.MORPH_CLOSE, np.ones((15, 15), np.uint8)
        )  # join letters of a stamp into one blob
        m = cv2.morphologyEx(m, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
        cnts, _ = cv2.findContours(m, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        for c in cnts:
            x, y, w, h = cv2.boundingRect(c)
            area_frac = w * h / (W * H)
            if not 0.002 <= area_frac <= 0.12:
                continue
            per = cv2.arcLength(c, True)
            circ = 4 * math.pi * cv2.contourArea(c) / (per * per) if per else 0
            aspect = max(w, h) / max(1, min(w, h))
            round_ = circ > 0.55 and aspect < 1.4
            extent = cv2.contourArea(c) / max(1, w * h)
            rect = (
                extent > 0.7 and 1.2 <= aspect <= 5
            )  # an outlined box: the joined blob fills its bounding box
            if not (round_ or rect):
                continue
            raw_ink = _raw_ink(rgb[y : y + h, x : x + w], color)
            if not 0.04 <= raw_ink <= 0.55:  # stamps are sparse ink, not solid blobs
                continue
            conf = (
                0.4 * (1.0 if round_ else 0.8)
                + 0.2 * min(1.0, raw_ink / 0.2)
                + 0.2
                + 0.2 * min(1.0, raw_ink * 4)
            )
            out.append(
                Stamp(
                    page,
                    (x, y, x + w, y + h),
                    "seal" if round_ else "hospital_stamp",
                    "classical",
                    round(min(conf, 0.95), 2),
                    ink_color=color,
                )
            )
    return _nms(out) + _signatures(rgb, page)


def _raw_ink(crop: np.ndarray, color: str) -> float:
    m = _ink_masks(crop)[color]
    return float((m > 0).mean())


def _nms(items: list[Stamp], iou: float = 0.4) -> list[Stamp]:
    items = sorted(items, key=lambda s: -s.det_conf)
    keep: list[Stamp] = []
    for s in items:
        if all(_iou(s.bbox, k.bbox) < iou for k in keep):
            keep.append(s)
    return keep


def _iou(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> float:
    ix = max(0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    u = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / u if u else 0.0


def _signatures(rgb: np.ndarray, page: int) -> list[Stamp]:
    """Thin-stroke ink (blue or dark) in the lower 40% of the page with a low fill ratio and no text baseline."""
    H, W = rgb.shape[:2]
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    hsv = cv2.cvtColor(rgb, cv2.COLOR_RGB2HSV)
    ink = (((hsv[..., 0] >= 95) & (hsv[..., 0] <= 135) & (hsv[..., 1] > 60)) | (gray < 70)).astype(
        np.uint8
    ) * 255
    ink[: int(H * 0.6)] = 0
    ink = cv2.morphologyEx(ink, cv2.MORPH_CLOSE, np.ones((9, 25), np.uint8))
    cnts, _ = cv2.findContours(ink, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    out = []
    for c in cnts:
        x, y, w, h = cv2.boundingRect(c)
        frac = w * h / (W * H)
        if not 0.001 <= frac <= 0.04 or h < 20 or w / h > 8:
            continue
        fill = float((ink[y : y + h, x : x + w] > 0).mean())
        strokes = float((cv2.Canny(gray[y : y + h, x : x + w], 50, 150) > 0).mean())
        if fill < 0.5 and strokes > 0.04 and h / w > 0.12:
            out.append(
                Stamp(page, (x, y, x + w, y + h), "signature", "classical", 0.5 + min(0.2, strokes))
            )
    return out[:3]


def ocr_crop(rgb: np.ndarray, bbox: tuple[int, int, int, int]) -> tuple[str | None, float | None]:
    if not shutil.which("tesseract"):
        return None, None
    x0, y0, x1, y1 = bbox
    crop = rgb[max(0, y0 - 8) : y1 + 8, max(0, x0 - 8) : x1 + 8]
    if min(crop.shape[:2]) < 120:
        crop = cv2.resize(crop, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
    gray = cv2.cvtColor(crop, cv2.COLOR_RGB2GRAY)
    best: tuple[str, float] = ("", 0.0)
    for rot in (None, cv2.ROTATE_90_CLOCKWISE, cv2.ROTATE_180, cv2.ROTATE_90_COUNTERCLOCKWISE):
        g = gray if rot is None else cv2.rotate(gray, rot)
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "c.png"
            cv2.imwrite(str(p), g)
            r = subprocess.run(
                ["tesseract", str(p), "stdout", "--psm", "6", "tsv"],
                capture_output=True,
                timeout=60,
                check=False,
            )  # noqa: S603, S607
        words, confs = [], []
        for row in r.stdout.decode(errors="ignore").splitlines()[1:]:
            c = row.split("\t")
            if len(c) >= 12 and c[11].strip() and float(c[10]) >= 0:
                words.append(c[11])
                confs.append(float(c[10]) / 100)
        if confs and sum(confs) / len(confs) > best[1]:
            best = (" ".join(words), sum(confs) / len(confs))
    text = re.sub(r"[^A-Z0-9 ./-]", "", best[0].upper())
    text = re.sub(r"\s+", " ", text).strip()
    return (text or None), (round(best[1], 2) if text else None)


def match_registry(
    text: str | None, registry: list[dict]
) -> tuple[bool | None, str | None, float | None]:  # type: ignore[type-arg]
    if not text or not registry:
        return None, None, None
    best, who = 0.0, None
    for h in registry:
        names = [h.get("name", ""), *(h.get("aliases") or [])]
        sc = max((fuzz.token_set_ratio(text, n.upper()) for n in names if n), default=0)
        for key in ("reg_no", "rohini_id", "code"):
            v = h.get(key)
            if v and len(str(v)) >= 5 and str(v).upper().replace(" ", "") in text.replace(" ", ""):
                sc = 100
        if sc > best:
            best, who = float(sc), h.get("code") or h.get("id")
    return best >= 85, who, round(best, 1)


def present(stamps: list[Stamp], kinds: list[str], det_min: float = 0.5) -> dict[str, bool]:
    """A kind counts as present when a detection of that kind has enough confidence. A hospital stamp also needs
    legible text or a registry match: an anonymous blob is not 'the hospital's stamp'. Seals count as stamps."""
    out = {}
    for k in kinds:
        ok = False
        for s in stamps:
            same = s.kind == k or (k == "hospital_stamp" and s.kind == "seal")
            if not same or s.det_conf < det_min:
                continue
            if k == "hospital_stamp":
                ok |= bool(s.matches_registry) or bool(s.ocr_text and (s.ocr_conf or 0) >= 0.5)
            else:
                ok = True
        out[k] = ok
    return out


def _dominant_ink(crop: np.ndarray) -> str:
    counts = {k: int((m > 0).sum()) for k, m in _ink_masks(crop).items()}
    best = max(counts, key=lambda k: counts[k])
    return (
        best if counts[best] > 0.01 * crop.shape[0] * crop.shape[1] else "black"
    )  # grey/black ink (photocopies)


_FOREST: object = "unset"


def forest() -> object:
    global _FOREST
    if _FOREST == "unset":
        from vision import stamp_model

        _FOREST = stamp_model.load()
    return _FOREST


def detect_learned(rgb: np.ndarray, page: int, model: object | None = None) -> list[Stamp]:
    """Colour-independent candidates scored by the trained forest (grey photocopies and faint ink included)."""
    from vision import stamp_candidates

    fo = model or forest()
    out: list[Stamp] = []
    for bbox, feat in stamp_candidates.candidates(rgb):
        p = fo.proba(feat)  # type: ignore[attr-defined]
        if p >= fo.threshold:  # type: ignore[attr-defined]
            f = dict(zip(stamp_candidates.FEATURES, feat, strict=True))
            round_ = f["ellipse_ratio"] > 0.8 and f["circularity"] > 0.55 and f["log_aspect"] < 0.3
            x0, y0, x1, y1 = bbox
            out.append(
                Stamp(
                    page,
                    bbox,
                    "seal" if round_ else "hospital_stamp",
                    "learned",
                    round(float(p), 2),
                    ink_color=_dominant_ink(rgb[y0:y1, x0:x1]),
                )
            )
    return _nms(out) + _signatures(rgb, page)


def detect(rgb: np.ndarray, page: int) -> list[Stamp]:
    """Learned detector when a model ships with the service, else the classical rule."""
    if forest() is not None:
        return detect_learned(rgb, page)
    return detect_classical(rgb, page)
