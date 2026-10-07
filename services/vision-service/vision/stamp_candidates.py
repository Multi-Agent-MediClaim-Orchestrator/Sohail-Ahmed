"""Colour-independent stamp candidates and their features. A stamp is a ring or a box outline with text inside: it shows up
as a mid-sized connected ink component with a hollow interior, whatever the ink colour (photocopies are grey)."""

from __future__ import annotations

import math

import cv2
import numpy as np

FEATURES = [
    "w_frac", "h_frac", "log_aspect", "extent", "solidity", "circularity", "ellipse_ratio", "rect_fill", "n_corners", "hollow",
    "inner_contours", "inner_letters", "border_ratio", "interior_ink", "row_regularity", "sat", "val", "colored", "y_center", "x_center",
]  # fmt: skip


def ink_mask(rgb: np.ndarray) -> np.ndarray:
    """Anything darker than paper or noticeably coloured (faint ink included)."""
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    hsv = cv2.cvtColor(rgb, cv2.COLOR_RGB2HSV)
    paper = float(np.percentile(gray, 90))
    m = (gray < paper - 45) | ((hsv[..., 1] > 55) & (hsv[..., 2] > 60))
    return (m.astype(np.uint8)) * 255


def candidates(
    rgb: np.ndarray, max_n: int = 40
) -> list[tuple[tuple[int, int, int, int], np.ndarray]]:
    """(bbox, feature vector) for mid-sized hollow-ish components."""
    H, W = rgb.shape[:2]
    m = ink_mask(rgb)
    m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8))
    n, labels, stats, _ = cv2.connectedComponentsWithStats(m, connectivity=8)
    hsv = cv2.cvtColor(rgb, cv2.COLOR_RGB2HSV)
    out = []
    for i in range(1, n):
        x, y, w, h, area = stats[i]
        if not (50 <= w <= 0.55 * W and 50 <= h <= 0.35 * H):
            continue
        aspect = max(w, h) / max(1, min(w, h))
        if aspect > 6 or area < 0.02 * w * h:
            continue
        comp = (labels[y : y + h, x : x + w] == i).astype(np.uint8)
        out.append(
            (
                (int(x), int(y), int(x + w), int(y + h)),
                _features(
                    comp, m[y : y + h, x : x + w], hsv[y : y + h, x : x + w], x, y, w, h, W, H
                ),
            )
        )
    out.sort(key=lambda t: -(t[0][2] - t[0][0]) * (t[0][3] - t[0][1]))
    return out[:max_n]


def _features(
    comp: np.ndarray,
    ink: np.ndarray,
    hsv: np.ndarray,
    x: int,
    y: int,
    w: int,
    h: int,
    W: int,
    H: int,
) -> np.ndarray:
    cnts, hier = cv2.findContours(comp, cv2.RETR_CCOMP, cv2.CHAIN_APPROX_SIMPLE)
    big = max(cnts, key=cv2.contourArea) if cnts else None
    area_px = float(comp.sum())
    extent = area_px / (w * h)
    carea = cv2.contourArea(big) if big is not None else 0.0
    per = cv2.arcLength(big, True) if big is not None else 0.0
    hull = cv2.contourArea(cv2.convexHull(big)) if big is not None and len(big) >= 3 else 0.0
    solidity = carea / hull if hull else 0.0
    circ = 4 * math.pi * carea / (per * per) if per else 0.0
    ell = 0.0
    if big is not None and len(big) >= 5:
        (_, (ma, mi), _) = cv2.fitEllipse(big)
        ell = min(ma, mi) / max(ma, mi) if max(ma, mi) else 0.0
    rect_fill = 0.0
    corners = 0
    if big is not None:
        r = cv2.minAreaRect(big)
        ra = r[1][0] * r[1][1]
        rect_fill = carea / ra if ra else 0.0
        corners = len(cv2.approxPolyDP(big, 0.02 * per, True)) if per else 0
    inner = 0 if hier is None else int(sum(1 for hh in hier[0] if hh[3] >= 0))  # holes
    hollow = 1.0 - (
        carea and (area_px / carea) or 1.0
    )  # share of the outer contour that is empty (ring-like)
    # letters inside the box: small components of the ink mask that are not the big outline
    k, _, st, _ = cv2.connectedComponentsWithStats((ink > 0).astype(np.uint8), connectivity=8)
    letters = int(sum(1 for j in range(1, k) if 4 <= st[j, 3] <= 40 and 2 <= st[j, 2] <= 40))
    bw, bh = max(2, int(w * 0.12)), max(2, int(h * 0.12))
    band = np.zeros_like(comp)
    band[:bh], band[-bh:], band[:, :bw], band[:, -bw:] = 1, 1, 1, 1
    border_ratio = float((comp * band).sum()) / max(1.0, area_px)
    interior = float((ink[bh:-bh, bw:-bw] > 0).mean()) if h > 2 * bh and w > 2 * bw else 0.0
    rows = (ink > 0).sum(axis=1).astype(np.float32)
    reg = float(rows.std() / rows.mean()) if rows.mean() > 0 else 0.0
    sel = comp > 0
    sat = float(hsv[..., 1][sel].mean()) / 255 if sel.any() else 0.0
    val = float(hsv[..., 2][sel].mean()) / 255 if sel.any() else 0.0
    colored = float((hsv[..., 1][sel] > 55).mean()) if sel.any() else 0.0
    return np.array([w / W, h / H, math.log(max(w, h) / max(1, min(w, h))), extent, solidity, circ, ell, rect_fill, corners, hollow, inner, letters, border_ratio, interior, reg, sat, val, colored, (y + h / 2) / H, (x + w / 2) / W], dtype=np.float32)  # fmt: skip
