"""Page legibility metrics (doc 03 task 3). Classical CV only; deterministic."""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass

import cv2
import numpy as np

from vision.settings import Settings


@dataclass
class PageQuality:
    page: int
    width: int
    height: int
    blur_score: float
    skew_deg: float
    brightness: float
    contrast: float
    cropped_edges: bool
    text_density: float
    blank_page: bool
    legible: bool
    reasons: list[str]

    def dict(self) -> dict:  # type: ignore[type-arg]
        return asdict(self)


def _norm_gray(rgb: np.ndarray) -> np.ndarray:
    g = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    h, w = g.shape
    return (
        cv2.resize(g, (1600, max(1, int(h * 1600 / w))), interpolation=cv2.INTER_AREA)
        if w != 1600
        else g
    )


def skew_angle(gray: np.ndarray) -> float:
    edges = cv2.Canny(gray, 50, 150)
    lines = cv2.HoughLinesP(
        edges, 1, math.pi / 720, threshold=80, minLineLength=int(gray.shape[1] * 0.3), maxLineGap=20
    )
    angles = []
    if lines is not None:
        for x1, y1, x2, y2 in lines[:, 0]:
            a = math.degrees(math.atan2(y2 - y1, x2 - x1))
            if abs(a) < 20:
                angles.append(a)
    if angles:
        return float(np.clip(np.median(angles), -45, 45))
    ys, xs = np.where(gray < 128)
    if len(xs) < 500:
        return 0.0
    rect = cv2.minAreaRect(np.column_stack([xs, ys]).astype(np.float32))
    a = rect[2]
    a = a - 90 if a > 45 else a
    return float(np.clip(a, -45, 45)) if abs(a) < 20 else 0.0


def measure(rgb: np.ndarray, page: int, s: Settings) -> PageQuality:
    gray = _norm_gray(rgb)
    h0, w0 = rgb.shape[:2]
    blur = float(cv2.Laplacian(gray, cv2.CV_64F).var())
    p5, p95 = np.percentile(gray, [5, 95])
    contrast, bright = float((p95 - p5) / 255), float(gray.mean())
    binary = cv2.adaptiveThreshold(
        gray, 255, cv2.ADAPTIVE_THRESH_MEAN_C, cv2.THRESH_BINARY_INV, 31, 15
    )
    n, _, stats, _ = cv2.connectedComponentsWithStats(binary, connectivity=8)
    text_like = sum(
        1
        for i in range(1, n)
        if 4 <= stats[i, cv2.CC_STAT_HEIGHT] <= 60
        and 2 <= stats[i, cv2.CC_STAT_WIDTH] <= 80
        and stats[i, cv2.CC_STAT_AREA] >= 6
    )
    density = float(
        text_like * 100 / max(1, gray.size) * 100
    )  # text-like components per 10k pixels, scaled
    blank = density < 0.2 or (contrast < 0.05)
    skew = skew_angle(gray)
    h, w = gray.shape
    bw, bh = int(w * 0.015), int(h * 0.015)
    strips = [binary[:, :bw], binary[:, -bw:], binary[:bh, :], binary[-bh:, :]]
    cropped = (
        any((st > 0).mean() > 0.02 for st in strips)
        and (binary[:, int(w * 0.015) : int(w * 0.065)] > 0).mean() > 0.05
    )
    dpi = w0 / 8.27 if 1.3 < max(h0, w0) / max(1, min(h0, w0)) < 1.55 else None
    reasons: list[str] = []
    if blur < s.blur_min:
        reasons.append("blurry")
    if contrast < s.contrast_min:
        reasons.append("low_contrast")
    if bright < s.bright_min:
        reasons.append("too_dark")
    if (
        bright > s.bright_max and p5 > 150
    ):  # a mostly-white page is normal; washed-out text (no dark pixels) is not
        reasons.append("too_bright")
    if abs(skew) > s.skew_max:
        reasons.append("skewed")
    if cropped:
        reasons.append("cropped")
    if dpi is not None and dpi < 140:
        reasons.append("low_resolution")
    if blank:
        reasons.append("blank")
    hard = {"blurry", "low_contrast", "too_dark", "too_bright", "skewed", "blank"} & set(reasons)
    soft_bad = bool({"cropped", "low_resolution"} & set(reasons)) and blur < 120
    return PageQuality(
        page,
        w0,
        h0,
        round(blur, 1),
        round(skew, 2),
        round(bright, 1),
        round(contrast, 3),
        cropped,
        round(density, 3),
        blank,
        not hard and not soft_bad,
        reasons,
    )


def score(qs: list[PageQuality]) -> float:
    """0..1 summary for hospital-api (display only; the flags are what gates act on)."""
    if not qs:
        return 0.0
    per = [
        min(1.0, q.blur_score / 200) * 0.5
        + min(1.0, q.contrast / 0.5) * 0.3
        + (0.2 if q.legible else 0.0)
        for q in qs
    ]
    return round(max(0.0, min(per)), 3)
