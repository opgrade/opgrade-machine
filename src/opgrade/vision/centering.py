"""Centering measurement on a rectified card image.

V1 approach: sample the border colour at the physical edge, then scan inward on each side
until pixels stop matching it (start of the printed frame). Works for bordered cards; full-art
cards yield low confidence and must be reviewed by the operator.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

EDGE_SKIP_PX = 3  # ignore rectification artefacts on the very edge
SAMPLE_DEPTH_PX = 6
MAX_SCAN_FRACTION = 0.25
COLOR_DISTANCE_THRESHOLD = 45.0
LINE_CHANGE_FRACTION = 0.5
SPAN_FRACTION = 0.6  # use the central part of each side, away from corners
MIN_BORDER_PX = 2


@dataclass
class CenteringResult:
    left_px: float
    right_px: float
    top_px: float
    bottom_px: float
    confidence: float

    @property
    def left_share(self) -> float:
        return 100.0 * self.left_px / (self.left_px + self.right_px)

    @property
    def top_share(self) -> float:
        return 100.0 * self.top_px / (self.top_px + self.bottom_px)

    @property
    def worst_share(self) -> float:
        """Larger share of the worst axis, e.g. 60 for a 60/40 card."""
        return max(self.left_share, 100 - self.left_share, self.top_share, 100 - self.top_share)

    def as_ratio_text(self) -> str:
        lr, tb = self.left_share, self.top_share
        return f"G/D {lr:.0f}/{100 - lr:.0f} · H/B {tb:.0f}/{100 - tb:.0f}"


def _border_width(strip: np.ndarray, border_color: np.ndarray, max_scan: int) -> int | None:
    """strip: (span, depth, 3) with depth axis going inward from the edge."""
    dist = np.linalg.norm(strip[:, :max_scan].astype(np.float32) - border_color, axis=2)
    changed = (dist > COLOR_DISTANCE_THRESHOLD).mean(axis=0) > LINE_CHANGE_FRACTION
    changed[:EDGE_SKIP_PX] = False
    idx = np.flatnonzero(changed)
    if idx.size == 0:
        return None
    return int(idx[0])


def measure_centering(warped: np.ndarray) -> CenteringResult:
    h, w = warped.shape[:2]
    img = warped.astype(np.float32)
    y0, y1 = int(h * (1 - SPAN_FRACTION) / 2), int(h * (1 + SPAN_FRACTION) / 2)
    x0, x1 = int(w * (1 - SPAN_FRACTION) / 2), int(w * (1 + SPAN_FRACTION) / 2)

    # Each strip is oriented (span, inward depth, channels).
    strips = {
        "left": img[y0:y1, :],
        "right": img[y0:y1, ::-1],
        "top": img[:, x0:x1].transpose(1, 0, 2),
        "bottom": img[::-1, x0:x1].transpose(1, 0, 2),
    }
    samples = np.concatenate(
        [s[:, EDGE_SKIP_PX : EDGE_SKIP_PX + SAMPLE_DEPTH_PX].reshape(-1, 3) for s in strips.values()]
    )
    border_color = np.median(samples, axis=0)
    # How uniform the border sample is: a busy (full-art) edge gives a large spread.
    spread = float(np.median(np.linalg.norm(samples - border_color, axis=1)))

    widths: dict[str, float] = {}
    found = 0
    for name, strip in strips.items():
        max_scan = int(strip.shape[1] * MAX_SCAN_FRACTION)
        width = _border_width(strip, border_color, max_scan)
        if width is None or width < MIN_BORDER_PX:
            widths[name] = float(max(width or 0, MIN_BORDER_PX))
        else:
            widths[name] = float(width)
            found += 1

    confidence = (found / 4.0) * float(np.clip(1.0 - spread / COLOR_DISTANCE_THRESHOLD, 0.0, 1.0))
    return CenteringResult(widths["left"], widths["right"], widths["top"], widths["bottom"], round(confidence, 3))
