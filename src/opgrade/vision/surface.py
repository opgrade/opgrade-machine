"""Surface defects (scratches, dents, creases) from raking-light captures.

Principle (classic, explainable, no training data needed):
1. raking / diffuse cancels the printed artwork (same albedo in both) and keeps the relief;
2. dividing by a heavy blur removes the light falloff across the card;
3. the 4 directions are combined (a scratch is seen best by the light perpendicular to it);
4. pixels far above the card's own noise level form defects (connected components).

Limits: tuned on the simulator. Holographic / glossy foil reflects differently under each light
and will raise false alarms; to be tuned on real cards (operator validates the surface grade).
"""

from __future__ import annotations

from dataclasses import dataclass, field

import cv2
import numpy as np

EDGE_MARGIN_MM = 2.5  # card edges and corners are judged by the edges/corners criteria
BACKGROUND_SIGMA_MM = 1.5  # removes light falloff and broad shading, keeps fine relief
NOISE_K = 6.0  # a defect must exceed K × the measured noise (grey levels, per raking image)
MIN_RELATIVE = 0.035  # ... and change brightness by at least 3.5 % (relative)
MIN_NOISE_GL = 0.05
MIN_AREA_MM2 = 0.06
SCRATCH_ELONGATION = 3.0
MERGE_GAP_MM = 0.5
GROUP_DISTANCE_MM = 1.5
MIN_PIECE_MM2 = 0.05  # smaller isolated specks are noise
MIN_LEVEL = 25.0  # diffuse grey level below which the ratio is meaningless (black ink)


@dataclass
class SurfaceDefect:
    kind: str  # "scratch" | "mark"
    x_mm: float  # bounding box, mm from the top-left corner of the rectified face
    y_mm: float
    w_mm: float
    h_mm: float
    length_mm: float
    area_mm2: float
    contrast: float  # mean anomaly / threshold (≥ 1)
    severity: str  # "minor" | "moderate" | "major"
    lighting: str  # raking light that shows it best

    def as_dict(self) -> dict:
        return {k: round(v, 2) if isinstance(v, float) else v for k, v in self.__dict__.items()}


@dataclass
class SurfaceResult:
    defects: list[SurfaceDefect]
    threshold: float
    noise: float
    anomaly: np.ndarray = field(repr=False)  # float32 map, same size as the face

    @property
    def score(self) -> float:
        return sum(SEVERITY_POINTS[d.severity] for d in self.defects)


SEVERITY_POINTS = {"minor": 1, "moderate": 3, "major": 6}
SEVERITY_LABELS = {"minor": "mineur", "moderate": "moyen", "major": "majeur"}
KIND_LABELS = {"scratch": "rayure", "mark": "marque / creux"}


def _severity(length_mm: float, area_mm2: float, contrast: float) -> str:
    if length_mm >= 10 or area_mm2 >= 6 or contrast >= 4:
        return "major"
    if length_mm >= 4 or area_mm2 >= 1.5 or contrast >= 2:
        return "moderate"
    return "minor"


def analyze_surface(diffuse: np.ndarray, raking: dict[str, np.ndarray], px_per_mm: float = 10.0) -> SurfaceResult:
    """diffuse and raking images: same rectified face, pixel-aligned (card did not move)."""
    base = cv2.cvtColor(diffuse, cv2.COLOR_BGR2GRAY).astype(np.float32)
    # The ratio only cancels the print where there is enough light: very dark ink is excluded,
    # dark areas count less (their ratio is noisier).
    usable = (base >= MIN_LEVEL).astype(np.float32)
    sigma = BACKGROUND_SIGMA_MM * px_per_mm
    norm = cv2.GaussianBlur(usable, (0, 0), sigma) + 1e-6
    margin = int(EDGE_MARGIN_MM * px_per_mm)
    valid = np.zeros(base.shape, bool)
    valid[margin:-margin, margin:-margin] = True
    valid &= usable > 0
    if not raking or not valid.any():
        return SurfaceResult([], 0.0, 0.0, np.zeros(base.shape, np.float32))

    rel_maps, flags, names, noises = [], [], [], []
    for name, image in raking.items():
        ratio = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY).astype(np.float32) / np.maximum(base, 1.0) * usable
        background = cv2.GaussianBlur(ratio, (0, 0), sigma) / norm  # ignores excluded pixels
        relief = cv2.GaussianBlur((ratio / (background + 1e-6) - 1.0) * usable, (0, 0), 0.8)
        # Two tests per pixel: a real defect changes brightness by a RELATIVE amount whatever the
        # ink colour; camera noise is constant in GREY LEVELS, so it looks large in dark areas.
        deviation_gl = relief * background * base  # brightness change in the raking image
        noise_gl = max(float(1.4826 * np.median(np.abs(deviation_gl[valid]))), MIN_NOISE_GL)
        rel = np.abs(relief)
        rel_maps.append(rel)
        flags.append((rel > MIN_RELATIVE) & (np.abs(deviation_gl) > NOISE_K * noise_gl))
        names.append(name)
        noises.append(noise_gl)
    stack = np.stack(rel_maps)
    flagged = np.stack(flags)
    anomaly = (stack * flagged).max(axis=0)  # relative relief where significant
    best_light = (stack * flagged).argmax(axis=0)
    threshold = MIN_RELATIVE
    noise = float(np.median(noises))

    mask = (flagged.any(axis=0) & valid).astype(np.uint8)
    # Rejoin pieces of the same scratch broken by noise (gaps up to ~0.5 mm).
    gap = max(3, int(round(MERGE_GAP_MM * px_per_mm)) | 1)
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (gap, gap)))
    # Drop noise specks first: grouping must join real fragments, not scattered noise pixels.
    pieces, piece_labels, piece_stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
    keep = piece_stats[:, cv2.CC_STAT_AREA] >= MIN_PIECE_MM2 * px_per_mm * px_per_mm
    keep[0] = False
    mask = keep[piece_labels].astype(np.uint8)
    # Group pieces within GROUP_DISTANCE_MM (a scratch interrupted by black ink stays one defect),
    # but measure only the pixels actually flagged.
    reach = int(round(GROUP_DISTANCE_MM * px_per_mm)) * 2 + 1
    groups = cv2.dilate(mask, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (reach, reach)))
    count, labels = cv2.connectedComponents(groups, connectivity=8)
    defects = []
    mm2 = px_per_mm * px_per_mm
    for i in range(1, count):
        ys, xs = np.nonzero((labels == i) & (mask > 0))
        area_mm2 = len(xs) / mm2
        if area_mm2 < MIN_AREA_MM2:
            continue
        pts = np.stack([xs, ys], axis=1).astype(np.float32)
        # Principal axes. A uniform segment of length L has variance L²/12 along it.
        centred = pts - pts.mean(axis=0)
        eigvals = np.sort(np.linalg.eigvalsh(np.cov(centred.T) if len(pts) > 2 else np.eye(2)))[::-1]
        length_mm = (np.sqrt(12 * max(eigvals[0], 0)) + 1) / px_per_mm
        width_mm = (np.sqrt(12 * max(eigvals[1], 0)) + 1) / px_per_mm
        contrast = float(anomaly[ys, xs].mean() / threshold)
        kind = "scratch" if length_mm >= SCRATCH_ELONGATION * width_mm and length_mm >= 1.5 else "mark"
        light = names[int(np.bincount(best_light[ys, xs]).argmax())]
        x, y = int(xs.min()), int(ys.min())
        w, h = int(xs.max()) - x + 1, int(ys.max()) - y + 1
        defects.append(SurfaceDefect(
            kind=kind, x_mm=x / px_per_mm, y_mm=y / px_per_mm, w_mm=w / px_per_mm, h_mm=h / px_per_mm,
            length_mm=float(length_mm), area_mm2=float(area_mm2), contrast=contrast,
            severity=_severity(length_mm, area_mm2, contrast), lighting=light,
        ))
    defects.sort(key=lambda d: SEVERITY_POINTS[d.severity] * 100 + d.length_mm, reverse=True)
    return SurfaceResult(defects, float(threshold), noise, anomaly)


def annotate_surface(diffuse: np.ndarray, result: SurfaceResult, px_per_mm: float = 10.0) -> np.ndarray:
    """Diffuse image with the relief map in red and numbered boxes around each defect."""
    out = diffuse.copy()
    if result.threshold > 0:
        heat = np.clip(result.anomaly / (result.threshold * 2), 0, 1)
        red = np.zeros_like(out)
        red[..., 2] = 255
        alpha = (heat * 0.6)[..., None]
        out = (out * (1 - alpha) + red * alpha).astype(np.uint8)
    colours = {"minor": (0, 200, 255), "moderate": (0, 140, 255), "major": (0, 0, 255)}
    for n, d in enumerate(result.defects, 1):
        x0, y0 = int(d.x_mm * px_per_mm) - 4, int(d.y_mm * px_per_mm) - 4
        x1, y1 = int((d.x_mm + d.w_mm) * px_per_mm) + 4, int((d.y_mm + d.h_mm) * px_per_mm) + 4
        cv2.rectangle(out, (x0, y0), (x1, y1), colours[d.severity], 2)
        cv2.putText(out, str(n), (x0, max(y0 - 6, 14)), cv2.FONT_HERSHEY_SIMPLEX, 0.6, colours[d.severity], 2)
    return out
