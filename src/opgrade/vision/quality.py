"""Image quality gate: a capture is rejected before analysis if it is unusable."""

from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np

# Thresholds calibrated on the simulator; must be re-tuned on the real optical bench.
MIN_SHARPNESS = 40.0
MIN_MEAN_BRIGHTNESS = 35.0
MAX_MEAN_BRIGHTNESS = 225.0
MAX_CLIPPED_FRACTION = 0.02


@dataclass
class QualityReport:
    sharpness: float
    mean_brightness: float
    clipped_fraction: float
    problems: list[str]

    @property
    def ok(self) -> bool:
        return not self.problems


def assess_quality(warped: np.ndarray) -> QualityReport:
    gray = cv2.cvtColor(warped, cv2.COLOR_BGR2GRAY)
    sharpness = float(cv2.Laplacian(gray, cv2.CV_64F).var())
    mean = float(gray.mean())
    clipped = float((gray >= 250).mean())

    problems: list[str] = []
    if sharpness < MIN_SHARPNESS:
        problems.append("Image floue : vérifiez la mise au point et que la carte est bien à plat.")
    if mean < MIN_MEAN_BRIGHTNESS:
        problems.append("Image trop sombre : vérifiez l'éclairage.")
    if mean > MAX_MEAN_BRIGHTNESS or clipped > MAX_CLIPPED_FRACTION:
        problems.append("Image surexposée ou reflets : vérifiez l'éclairage.")
    return QualityReport(sharpness, mean, clipped, problems)
