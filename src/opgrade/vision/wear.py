"""Corner and edge wear: whitening (ink chipped off) and missing material (dings, nicks).

- Whitening, on the diffuse photo: in the outer band of the card (0–0.9 mm from the edge), pixels
  clearly BRIGHTER and LESS SATURATED than the border colour measured just inside (1.2–2.4 mm):
  the white card stock shows through. On a white/light border this is invisible: the corner or
  edge is then reported as not measurable instead of "no defect".
- Missing material, on the backlit silhouette: the real outline compared with the ideal outline
  (rectangle with rounded corners of the card type). A dinged corner or a nick is missing area.

Tuned on the simulator; to be checked on real cards (operator validates the corner/edge grades).
"""

from __future__ import annotations

from dataclasses import dataclass, field

import cv2
import numpy as np

OUTER_BAND_MM = (0.15, 0.9)  # where chipping shows (the very edge pixel blends with the tray)
EDGE_RATE_SIGMA_MM = 1.0  # local rate of whitish pixels along the edge
MIN_EDGE_RATE = 0.15  # wear covers at least 15 % of the edge band locally...
EXCESS_FACTOR = 3.0  # ... and 3 times the rate found just inside (print, foil glints)
TEXTURED_RATE = 0.25  # above this just inside the edge, the zone cannot be judged
REFERENCE_BAND_MM = (1.2, 2.4)  # local border colour, usually intact
REFERENCE_SIGMA_MM = 3.0
WHITENING_DV = 25  # brighter than the border by at least this (HSV value, 0–255)
WHITENING_SAT_RATIO = 0.6  # and less saturated than 60 % of the border saturation (+15)
WHITENING_MIN_V = 140
WHITENING_BRIGHT_CAP = 215  # card stock is at least this bright, whatever the border
LIGHT_BORDER_V, LIGHT_BORDER_S = 200, 50  # border too light for whitening to show
MIN_SPECK_PX = 2
CORNER_ZONE_MM = 5.0
LOSS_TOLERANCE_MM = 0.3  # outline differences thinner than this are ignored (rectification, radius)
MIN_NICK_MM2 = 0.05
UNMEASURABLE_FRACTION = 0.5
MAX_RADIUS_DEVIATION_MM = 1.5  # measured corner radius accepted within ± this of the nominal one

CORNERS = ("tl", "tr", "br", "bl")
EDGES = ("top", "right", "bottom", "left")
CORNER_LABELS = {"tl": "haut gauche", "tr": "haut droit", "br": "bas droit", "bl": "bas gauche"}
EDGE_LABELS = {"top": "haut", "right": "droite", "bottom": "bas", "left": "gauche"}


@dataclass
class CornerWear:
    name: str
    whitening_mm2: float
    missing_mm2: float
    measurable: bool  # False: light border, whitening cannot be seen

    def as_dict(self) -> dict:
        return {k: round(v, 3) if isinstance(v, float) else v for k, v in self.__dict__.items()}


@dataclass
class EdgeWear:
    name: str
    whitened_length_mm: float
    edge_length_mm: float
    nicks: int
    nick_area_mm2: float
    measurable: bool
    segments: list[tuple[float, float]] = field(default_factory=list)  # whitened spans, mm along the edge

    def as_dict(self) -> dict:
        return {k: (round(v, 3) if isinstance(v, float) else v) for k, v in self.__dict__.items() if k != "segments"}


@dataclass
class WearResult:
    corners: list[CornerWear]
    edges: list[EdgeWear]
    whitened: np.ndarray = field(repr=False)  # bool mask
    lost: np.ndarray = field(repr=False)  # bool mask (missing material)
    outline_checked: bool = True  # False without a backlit silhouette


def rounded_rect_mask(shape: tuple[int, int], radius_px: float) -> np.ndarray:
    h, w = shape
    r = int(round(radius_px))
    mask = np.zeros(shape, np.uint8)
    if r <= 0:
        mask[:] = 1
        return mask
    cv2.rectangle(mask, (r, 0), (w - 1 - r, h - 1), 1, -1)
    cv2.rectangle(mask, (0, r), (w - 1, h - 1 - r), 1, -1)
    for cx, cy in ((r, r), (w - 1 - r, r), (r, h - 1 - r), (w - 1 - r, h - 1 - r)):
        cv2.circle(mask, (cx, cy), r, 1, -1)
    return mask


def _measured_corner_radius(actual: np.ndarray, nominal_mm: float, px_per_mm: float) -> float:
    """The card's own corner radius. Real die cuts differ from the nominal value (a fixed radius
    made every corner of real cards look nibbled). Missing area in a corner square is
    (1 - π/4)·r²; the second smallest of the 4 corners is kept, so up to 2 damaged corners do not
    bias it. Falls back to the nominal radius if the measure is implausible."""
    h, w = actual.shape
    z = int(round(CORNER_ZONE_MM * px_per_mm))
    radii = []
    for zy, zx in ((slice(0, z), slice(0, z)), (slice(0, z), slice(w - z, w)),
                   (slice(h - z, h), slice(w - z, w)), (slice(h - z, h), slice(0, z))):
        missing_px = float((actual[zy, zx] == 0).sum())
        radii.append(np.sqrt(missing_px / (1 - np.pi / 4)) / px_per_mm)
    radius = sorted(radii)[1]
    return radius if abs(radius - nominal_mm) <= MAX_RADIUS_DEVIATION_MM else nominal_mm


def _distance_mm(mask: np.ndarray, px_per_mm: float) -> np.ndarray:
    # Pad with background: distanceTransform does not treat outside the image as background.
    padded = cv2.copyMakeBorder(mask.astype(np.uint8), 1, 1, 1, 1, cv2.BORDER_CONSTANT, value=0)
    return cv2.distanceTransform(padded, cv2.DIST_L2, 5)[1:-1, 1:-1] / px_per_mm


def _smooth(values: np.ndarray, weights: np.ndarray, sigma: float) -> np.ndarray:
    """Weighted Gaussian average (only pixels with weight 1 contribute)."""
    return cv2.GaussianBlur(values * weights, (0, 0), sigma) / (cv2.GaussianBlur(weights, (0, 0), sigma) + 1e-6)


def analyze_wear(diffuse: np.ndarray, silhouette: np.ndarray | None, corner_radius_mm: float = 3.0,
                 px_per_mm: float = 10.0) -> WearResult:
    h, w = diffuse.shape[:2]
    radius_mm = corner_radius_mm
    if silhouette is not None:
        gray = cv2.cvtColor(silhouette, cv2.COLOR_BGR2GRAY) if silhouette.ndim == 3 else silhouette
        actual = (gray < 128).astype(np.uint8)  # card is dark on the backlit tray
        actual = cv2.morphologyEx(actual, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
        radius_mm = _measured_corner_radius(actual, corner_radius_mm, px_per_mm)
        expected = rounded_rect_mask((h, w), radius_mm * px_per_mm)
    else:
        expected = rounded_rect_mask((h, w), corner_radius_mm * px_per_mm)
        actual = expected

    # -- whitening -----------------------------------------------------------------------
    dist = _distance_mm(actual, px_per_mm)
    outer = (dist > OUTER_BAND_MM[0]) & (dist <= OUTER_BAND_MM[1])
    reference = ((dist > REFERENCE_BAND_MM[0]) & (dist <= REFERENCE_BAND_MM[1])).astype(np.float32)
    hsv = cv2.cvtColor(diffuse, cv2.COLOR_BGR2HSV).astype(np.float32)
    sat, val = hsv[..., 1], hsv[..., 2]
    sigma = REFERENCE_SIGMA_MM * px_per_mm
    ref_s, ref_v = _smooth(sat, reference, sigma), _smooth(val, reference, sigma)
    light_border = (ref_v >= LIGHT_BORDER_V) & (ref_s <= LIGHT_BORDER_S)
    # Brighter than the border... capped: on an already bright, saturated border (Pokémon yellow)
    # the white stock is not brighter, it is the loss of colour that gives it away.
    min_value = np.maximum(np.minimum(ref_v + WHITENING_DV, WHITENING_BRIGHT_CAP), WHITENING_MIN_V)
    whitish = (val >= min_value) & (sat <= WHITENING_SAT_RATIO * ref_s + 15)
    # Control: the same test just inside the edge. Real wear sits AT the edge; foil glints,
    # silver borders and white print are found just as much further in -> not wear, and the
    # zone cannot be judged (reported as unmeasurable instead of a false defect).
    control_rate = _smooth((whitish & (reference > 0)).astype(np.float32), reference, sigma)
    outer_f = outer.astype(np.float32)
    edge_rate = _smooth((whitish & outer).astype(np.float32), outer_f, EDGE_RATE_SIGMA_MM * px_per_mm)
    textured = control_rate > TEXTURED_RATE
    light_border |= textured
    # Wear = clear excess of whitish pixels AT the edge compared with just inside.
    excess = edge_rate >= np.maximum(MIN_EDGE_RATE, EXCESS_FACTOR * control_rate)
    whitened = outer & ~light_border & whitish & excess
    count, labels, stats, _ = cv2.connectedComponentsWithStats(whitened.astype(np.uint8), connectivity=8)
    keep = stats[:, cv2.CC_STAT_AREA] >= MIN_SPECK_PX
    keep[0] = False
    whitened = keep[labels]

    # -- missing material ----------------------------------------------------------------
    tol = max(3, int(round(LOSS_TOLERANCE_MM * px_per_mm)) | 1)
    lost = cv2.morphologyEx(((expected > 0) & (actual == 0)).astype(np.uint8), cv2.MORPH_OPEN,
                            cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (tol, tol))) > 0

    # -- per corner -----------------------------------------------------------------------
    z = int(round(CORNER_ZONE_MM * px_per_mm))
    zones = {
        "tl": (slice(0, z), slice(0, z)), "tr": (slice(0, z), slice(w - z, w)),
        "br": (slice(h - z, h), slice(w - z, w)), "bl": (slice(h - z, h), slice(0, z)),
    }
    in_corner = np.zeros((h, w), bool)
    mm2 = px_per_mm * px_per_mm
    corners = []
    for name in CORNERS:
        zy, zx = zones[name]
        in_corner[zy, zx] = True
        band = outer[zy, zx]
        measurable = not (band.any() and light_border[zy, zx][band].mean() > UNMEASURABLE_FRACTION)
        corners.append(CornerWear(name, float(whitened[zy, zx].sum() / mm2), float(lost[zy, zx].sum() / mm2), measurable))

    # -- per edge (corner zones excluded) ------------------------------------------------
    yy, xx = np.mgrid[0:h, 0:w]
    nearest = np.argmin(np.stack([yy, w - 1 - xx, h - 1 - yy, xx]), axis=0)  # index into EDGES
    edges = []
    for i, name in enumerate(EDGES):
        region = (nearest == i) & ~in_corner
        band = outer & region
        along_axis = xx if name in ("top", "bottom") else yy
        size = w if name in ("top", "bottom") else h
        occupied = np.zeros(size, np.uint8)
        occupied[np.unique(along_axis[whitened & region])] = 1
        occupied = cv2.dilate(occupied.reshape(1, -1), np.ones((1, 3), np.uint8)).ravel()  # join specks
        spans, start = [], None
        for k, v in enumerate(np.append(occupied, 0)):
            if v and start is None:
                start = k
            elif not v and start is not None:
                spans.append((start / px_per_mm, k / px_per_mm))
                start = None
        n, nick_labels, nick_stats, _ = cv2.connectedComponentsWithStats((lost & region).astype(np.uint8))
        nick_areas = [nick_stats[j, cv2.CC_STAT_AREA] / mm2 for j in range(1, n)]
        nick_areas = [a for a in nick_areas if a >= MIN_NICK_MM2]
        measurable = not (band.any() and light_border[band].mean() > UNMEASURABLE_FRACTION)
        edges.append(EdgeWear(name, float(occupied.sum() / px_per_mm), (size - 2 * z) / px_per_mm,
                              len(nick_areas), float(sum(nick_areas)), measurable, spans))
    return WearResult(corners, edges, whitened, lost, silhouette is not None)


def annotate_wear(image: np.ndarray, result: WearResult, px_per_mm: float = 10.0) -> np.ndarray:
    """Whitening in magenta, missing material in red, worn corners boxed."""
    out = image.copy()
    out[result.whitened] = (255, 0, 255)
    out[result.lost] = (0, 0, 255)
    h, w = out.shape[:2]
    z = int(round(CORNER_ZONE_MM * px_per_mm))
    origin = {"tl": (0, 0), "tr": (w - z, 0), "br": (w - z, h - z), "bl": (0, h - z)}
    for c in result.corners:
        if c.whitening_mm2 > 0.05 or c.missing_mm2 > 0.02:
            x, y = origin[c.name]
            cv2.rectangle(out, (x + 2, y + 2), (x + z - 3, y + z - 3), (255, 0, 255), 2)
    return out
