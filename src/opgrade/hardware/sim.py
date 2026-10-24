"""Simulated hardware.

The simulator renders a synthetic card lying on the black tray, with known centering, so that
the real vision pipeline can be exercised and measured. It does not bypass any step: the
operator still has to place and flip the (virtual) card, and lighting must be on to capture.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field

import cv2
import numpy as np

from .base import Camera, Hardware, Lighting, LightingMode, Side

SIM_PX_PER_MM = 10.0
CANVAS_W, CANVAS_H = 1000, 1200


@dataclass
class SimCardSpec:
    """Synthetic card. Shares are the left/top border share in percent (50 = perfect)."""

    width_mm: float = 63.0
    height_mm: float = 88.0
    border_mm: float = 3.0
    front_left_share: float = 50.0
    front_top_share: float = 50.0
    back_left_share: float = 50.0
    back_top_share: float = 50.0
    rotation_deg: float = 1.5
    blur: float = 0.0
    seed: int = 7
    # Optional real card picture (BGR) used as the front face, e.g. a catalog scan.
    front_image: np.ndarray | None = field(default=None, repr=False)
    label: str | None = None  # what the card really is, shown in the simulation panel
    brightness: float = 1.0  # lighting variation between captures


@dataclass
class SimTable:
    """State of the virtual tray, shared between the simulated camera and the sim API."""

    card: SimCardSpec | None = field(default_factory=SimCardSpec)
    side_up: Side = Side.FRONT
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def place(self, card: SimCardSpec | None = None, side_up: Side = Side.FRONT) -> None:
        with self.lock:
            self.card = card or SimCardSpec()
            self.side_up = side_up

    def flip(self) -> None:
        with self.lock:
            self.side_up = Side.BACK if self.side_up == Side.FRONT else Side.FRONT

    def remove(self) -> None:
        with self.lock:
            self.card = None


class SimulatedLighting(Lighting):
    def __init__(self) -> None:
        self._mode = LightingMode.OFF

    def set_mode(self, mode: LightingMode) -> None:
        self._mode = mode

    @property
    def mode(self) -> LightingMode:
        return self._mode


# BGR colours of the synthetic faces.
_FRONT_BORDER = (40, 205, 240)  # Pokémon-like yellow
_BACK_BORDER = (185, 95, 35)  # dark blue


def _render_face(spec: SimCardSpec, side: Side) -> np.ndarray:
    w = int(round(spec.width_mm * SIM_PX_PER_MM))
    h = int(round(spec.height_mm * SIM_PX_PER_MM))
    if side == Side.FRONT and spec.front_image is not None:
        return cv2.resize(spec.front_image, (w, h), interpolation=cv2.INTER_CUBIC)
    rng = np.random.default_rng(spec.seed + (0 if side == Side.FRONT else 1000))
    border_px = spec.border_mm * SIM_PX_PER_MM
    if side == Side.FRONT:
        border, left_share, top_share = _FRONT_BORDER, spec.front_left_share, spec.front_top_share
    else:
        border, left_share, top_share = _BACK_BORDER, spec.back_left_share, spec.back_top_share

    face = np.empty((h, w, 3), np.uint8)
    face[:] = border
    left = int(round(2 * border_px * left_share / 100.0))
    top = int(round(2 * border_px * top_share / 100.0))
    art_w = w - int(round(2 * border_px))
    art_h = h - int(round(2 * border_px))

    yy, xx = np.mgrid[0:art_h, 0:art_w].astype(np.float32)
    if side == Side.FRONT:
        art = np.stack(
            [120 + 80 * np.sin(xx / 40), 60 + 40 * np.cos(yy / 55), 90 + 60 * np.sin((xx + yy) / 70)],
            axis=-1,
        )
    else:
        r = np.hypot(xx - art_w / 2, yy - art_h / 2)
        art = np.stack([40 + 30 * np.sin(r / 25), 40 + 30 * np.cos(r / 30), 150 + 80 * np.sin(r / 20)], axis=-1)
    art += rng.normal(0, 6, art.shape)
    face[top : top + art_h, left : left + art_w] = np.clip(art, 0, 255).astype(np.uint8)
    return face


def render_scene(spec: SimCardSpec | None, side: Side, lighting: LightingMode) -> np.ndarray:
    rng = np.random.default_rng(1)
    if lighting == LightingMode.BACKLIGHT:
        canvas = np.clip(rng.normal(235, 4, (CANVAS_H, CANVAS_W, 3)), 0, 255).astype(np.uint8)
    else:
        canvas = np.clip(rng.normal(14, 3, (CANVAS_H, CANVAS_W, 3)), 0, 255).astype(np.uint8)
    if spec is not None:
        face = _render_face(spec, side)
        h, w = face.shape[:2]
        cx, cy = CANVAS_W / 2, CANVAS_H / 2
        m = cv2.getRotationMatrix2D((w / 2, h / 2), spec.rotation_deg, 1.0)
        m[0, 2] += cx - w / 2
        m[1, 2] += cy - h / 2
        mask = cv2.warpAffine(np.full((h, w), 255, np.uint8), m, (CANVAS_W, CANVAS_H))
        if lighting == LightingMode.BACKLIGHT:  # opaque card: dark silhouette, colours irrelevant
            canvas[mask > 127] = 25
            return canvas
        warped = cv2.warpAffine(face, m, (CANVAS_W, CANVAS_H), flags=cv2.INTER_LINEAR)
        canvas[mask > 127] = warped[mask > 127]
        if spec.brightness != 1.0:
            canvas = np.clip(canvas.astype(np.float32) * spec.brightness, 0, 255).astype(np.uint8)
        if spec.blur > 0:
            canvas = cv2.GaussianBlur(canvas, (0, 0), spec.blur)

    if lighting == LightingMode.OFF:
        canvas = (canvas * 0.05).astype(np.uint8)
    return canvas


class SimulatedCamera(Camera):
    def __init__(self, table: SimTable, lighting: SimulatedLighting) -> None:
        self.table = table
        self.lighting = lighting

    def capture(self) -> np.ndarray:
        with self.table.lock:
            spec, side = self.table.card, self.table.side_up
        return render_scene(spec, side, self.lighting.mode)


def build_simulated_hardware(table: SimTable | None = None) -> Hardware:
    lighting = SimulatedLighting()
    camera = SimulatedCamera(table or SimTable(), lighting)
    return Hardware(camera=camera, lighting=lighting, simulated=True, has_backlight=True)
