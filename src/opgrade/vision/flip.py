"""Check that the operator actually flipped the card between front and back captures."""

from __future__ import annotations

import cv2
import numpy as np

SAME_SIDE_THRESHOLD = 0.90


def face_similarity(a: np.ndarray, b: np.ndarray) -> float:
    """Hue/saturation histogram correlation, insensitive to in-plane rotation (1 = identical)."""

    def hist(img: np.ndarray) -> np.ndarray:
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        h = cv2.calcHist([hsv], [0, 1], None, [30, 32], [0, 180, 0, 256])
        return cv2.normalize(h, h).flatten()

    return float(cv2.compareHist(hist(a), hist(b), cv2.HISTCMP_CORREL))


def looks_like_same_side(front: np.ndarray, back: np.ndarray) -> bool:
    return face_similarity(front, back) >= SAME_SIDE_THRESHOLD
