"""Card detection on the black tray and perspective rectification."""

from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np

# Canonical resolution of rectified images. Measurements are in this pixel space.
CANONICAL_PX_PER_MM = 10.0
ASPECT_TOLERANCE = 0.06
MIN_AREA_FRACTION = 0.05
MIN_CARD_CONTRAST = 25.0  # grey levels between card and black tray


@dataclass
class CardDetection:
    quad: np.ndarray  # 4x2 float32, ordered TL, TR, BR, BL in source image
    warped: np.ndarray  # rectified BGR image, portrait
    px_per_mm: float


class DetectionError(Exception):
    """The image does not contain a usable card. Message is operator-facing (French)."""


def _order_quad(pts: np.ndarray) -> np.ndarray:
    pts = pts.astype(np.float32)
    s = pts.sum(axis=1)
    d = np.diff(pts, axis=1).ravel()
    return np.array([pts[np.argmin(s)], pts[np.argmin(d)], pts[np.argmax(s)], pts[np.argmax(d)]], np.float32)


def detect_card(image: np.ndarray, width_mm: float, height_mm: float) -> CardDetection:
    """Find the card on the black tray and rectify it (no backlight available)."""
    return rectify(image, find_card_quad(image, width_mm, height_mm), width_mm, height_mm)


def find_card_quad(image: np.ndarray, width_mm: float, height_mm: float, silhouette: bool = False) -> np.ndarray:
    """Card corners (TL, TR, BR, BL). With silhouette=True the image is a backlit shot where the
    card is dark on a bright tray: this works whatever the card colours (black-bordered cards
    are invisible on a black tray)."""
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    if silhouette:
        gray = 255 - gray
    gray = cv2.GaussianBlur(gray, (5, 5), 0)
    _, mask = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    fg, bg = gray[mask > 0], gray[mask == 0]
    # Otsu always splits something: on an empty tray it only splits noise.
    if fg.size == 0 or bg.size == 0 or float(fg.mean()) - float(bg.mean()) < MIN_CARD_CONTRAST:
        raise DetectionError("Aucune carte détectée sur le plateau.")
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((7, 7), np.uint8))
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        raise DetectionError("Aucune carte détectée sur le plateau.")

    contour = max(contours, key=cv2.contourArea)
    if cv2.contourArea(contour) < MIN_AREA_FRACTION * image.shape[0] * image.shape[1]:
        raise DetectionError("Aucune carte détectée sur le plateau.")

    x, y, w, h = cv2.boundingRect(contour)
    if x <= 1 or y <= 1 or x + w >= image.shape[1] - 1 or y + h >= image.shape[0] - 1:
        raise DetectionError("La carte dépasse du champ de la caméra : replacez-la contre les butées.")

    rect = cv2.minAreaRect(contour)
    quad = _order_quad(cv2.boxPoints(rect))
    side_a = np.linalg.norm(quad[1] - quad[0])
    side_b = np.linalg.norm(quad[3] - quad[0])
    if side_a > side_b:  # card lies in landscape: rotate so the long side is vertical
        quad = np.roll(quad, -1, axis=0)
        side_a, side_b = side_b, side_a

    measured_aspect = side_a / side_b
    expected_aspect = width_mm / height_mm
    if abs(measured_aspect - expected_aspect) / expected_aspect > ASPECT_TOLERANCE:
        raise DetectionError(
            "Les proportions de la carte ne correspondent pas au type choisi "
            f"({measured_aspect:.3f} mesuré, {expected_aspect:.3f} attendu)."
        )
    return quad


def rectify(image: np.ndarray, quad: np.ndarray, width_mm: float, height_mm: float) -> CardDetection:
    out_w = int(round(width_mm * CANONICAL_PX_PER_MM))
    out_h = int(round(height_mm * CANONICAL_PX_PER_MM))
    dst = np.array([[0, 0], [out_w - 1, 0], [out_w - 1, out_h - 1], [0, out_h - 1]], np.float32)
    matrix = cv2.getPerspectiveTransform(quad, dst)
    warped = cv2.warpPerspective(image, matrix, (out_w, out_h), flags=cv2.INTER_CUBIC)
    return CardDetection(quad=quad, warped=warped, px_per_mm=CANONICAL_PX_PER_MM)
