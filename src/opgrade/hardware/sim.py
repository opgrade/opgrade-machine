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
    # Surface relief, visible mainly under raking light (scratches, dents).
    defects: list[SimDefect] = field(default_factory=list)
    corner_radius_mm: float = 3.0  # die-cut rounded corners of standard cards
    # Corner and edge wear: whitening (ink chipped off) and dinged corners (material missing).
    wear: list[SimWear] = field(default_factory=list)


@dataclass
class SimWear:
    """Corner / edge wear, in the coordinates of the face given by `side`.

    - "edge_whitening": `edge` in top/bottom/left/right, from `start_mm` to `end_mm` along it,
      white specks up to `depth_mm` from the edge, coverage `density`;
    - "corner_whitening": `corner` in tl/tr/br/bl, within `size_mm` of the corner;
    - "corner_ding": material missing at `corner` (chamfer of `size_mm`); physical, so it is
      seen on both faces (mirrored on the back) and on the backlit silhouette. Front coordinates.
    """

    kind: str
    edge: str | None = None
    corner: str | None = None
    start_mm: float = 0.0
    end_mm: float = 0.0
    depth_mm: float = 0.6
    size_mm: float = 2.0
    density: float = 0.6
    side: Side = Side.FRONT
    seed: int = 0


PAPER_WHITE = (232, 236, 238)  # BGR of the exposed card stock
_MIRROR_CORNER = {"tl": "tr", "tr": "tl", "bl": "br", "br": "bl"}


def _card_alpha(spec: SimCardSpec, side: Side, shape: tuple[int, int]) -> np.ndarray:
    """255 inside the card outline: rounded corners minus dinged corners."""
    h, w = shape
    alpha = np.zeros(shape, np.uint8)
    r = int(round(spec.corner_radius_mm * SIM_PX_PER_MM))
    if r > 0:
        cv2.rectangle(alpha, (r, 0), (w - 1 - r, h - 1), 255, -1)
        cv2.rectangle(alpha, (0, r), (w - 1, h - 1 - r), 255, -1)
        for cx, cy in ((r, r), (w - 1 - r, r), (r, h - 1 - r), (w - 1 - r, h - 1 - r)):
            cv2.circle(alpha, (cx, cy), r, 255, -1, cv2.LINE_AA)
    else:
        alpha[:] = 255
    for wear in spec.wear:
        if wear.kind != "corner_ding":
            continue
        corner = wear.corner if side == Side.FRONT else _MIRROR_CORNER[wear.corner]
        # The cut bites into the ROUNDED corner: the arc is r·(√2 − 1) from the corner point along
        # the diagonal; the ding removes material 0.6·size beyond it (a chamfer at the corner
        # point itself would only cut where there is no card).
        depth_px = (spec.corner_radius_mm * (np.sqrt(2) - 1) + 0.6 * wear.size_mm) * SIM_PX_PER_MM
        leg = depth_px * np.sqrt(2)
        xs = {"tl": 0, "bl": 0, "tr": w - 1, "br": w - 1}[corner]
        ys = {"tl": 0, "tr": 0, "bl": h - 1, "br": h - 1}[corner]
        sx, sy = (1 if xs == 0 else -1), (1 if ys == 0 else -1)
        tri = np.array([[xs, ys], [xs + sx * leg, ys], [xs, ys + sy * leg]], np.int32)
        cv2.fillPoly(alpha, [tri], 0, cv2.LINE_AA)
    return alpha


def _apply_wear(face: np.ndarray, alpha: np.ndarray, spec: SimCardSpec, side: Side) -> np.ndarray:
    """Whitening: ink chipped off near the outline, exposing white stock (also around dings)."""
    items = [w for w in spec.wear if w.kind in ("edge_whitening", "corner_whitening") and w.side == side]
    dings = [w for w in spec.wear if w.kind == "corner_ding"]
    if not items and not dings:
        return face
    h, w = face.shape[:2]
    # Pad with background: distanceTransform does not treat outside the image as background.
    padded = cv2.copyMakeBorder((alpha > 127).astype(np.uint8), 1, 1, 1, 1, cv2.BORDER_CONSTANT, value=0)
    dist_mm = cv2.distanceTransform(padded, cv2.DIST_L2, 5)[1:-1, 1:-1] / SIM_PX_PER_MM
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32) / SIM_PX_PER_MM
    region = np.zeros((h, w), np.float32)
    rng = np.random.default_rng(97)
    for item in items:
        if item.kind == "edge_whitening":
            along = xx if item.edge in ("top", "bottom") else yy
            near = {"top": yy, "bottom": (h - 1) / SIM_PX_PER_MM - yy,
                    "left": xx, "right": (w - 1) / SIM_PX_PER_MM - xx}[item.edge]
            sel = (along >= item.start_mm) & (along <= item.end_mm) & (near <= item.depth_mm + 0.5)
        else:
            cx = 0 if item.corner in ("tl", "bl") else (w - 1) / SIM_PX_PER_MM
            cy = 0 if item.corner in ("tl", "tr") else (h - 1) / SIM_PX_PER_MM
            sel = np.hypot(xx - cx, yy - cy) <= item.size_mm + spec.corner_radius_mm * 0.3
        sel &= dist_mm <= item.depth_mm
        speckle = cv2.GaussianBlur(np.random.default_rng(item.seed).random((h, w)).astype(np.float32), (0, 0), 1.2)
        threshold = np.quantile(speckle, 1 - item.density)
        region = np.maximum(region, (sel & (speckle >= threshold)).astype(np.float32))
    for _ in dings:  # torn fibres along the new edge of a dinged corner
        expected = _card_alpha(SimCardSpec(corner_radius_mm=spec.corner_radius_mm), side, (h, w)) > 127
        lost = expected & (alpha <= 127)
        if lost.any():
            near_loss = cv2.dilate(lost.astype(np.uint8), np.ones((9, 9), np.uint8)) > 0
            region = np.maximum(region, (near_loss & (alpha > 127) & (rng.random((h, w)) < 0.7)).astype(np.float32))
        break
    if not region.any():
        return face
    region = cv2.GaussianBlur(region, (0, 0), 0.6)[..., None]
    return (face * (1 - region) + np.array(PAPER_WHITE, np.float32) * region).astype(np.uint8)


@dataclass
class SimDefect:
    """Relief on one face, in mm from that face's top-left corner.

    `slope` is the steepest slope of the relief: real hairline scratches are ~0.02–0.1.
    Under raking light (~14° elevation) a slope s changes brightness by about 4·s.
    """

    kind: str  # "scratch" (from x,y to x2,y2) | "dent" (centred on x,y, radius = width_mm)
    x_mm: float
    y_mm: float
    x2_mm: float = 0.0
    y2_mm: float = 0.0
    width_mm: float = 0.2
    slope: float = 0.08
    side: Side = Side.FRONT


def random_sim_wear(count: int, seed: int | None = None) -> list[SimWear]:
    """Random corner/edge wear: whitening on either face, dinged corners."""
    rng = np.random.default_rng(seed)
    wear = []
    for _ in range(count):
        side = Side.FRONT if rng.random() < 0.5 else Side.BACK
        kind = rng.choice(["edge_whitening", "corner_whitening", "corner_ding"], p=[0.45, 0.35, 0.2])
        corner = str(rng.choice(["tl", "tr", "br", "bl"]))
        if kind == "edge_whitening":
            edge = str(rng.choice(["top", "right", "bottom", "left"]))
            length = 63 if edge in ("top", "bottom") else 88
            start = float(rng.uniform(8, length - 20))
            wear.append(SimWear(kind, edge=edge, start_mm=start, end_mm=start + float(rng.uniform(3, 12)),
                                depth_mm=0.6, density=0.6, side=side, seed=int(rng.integers(1000))))
        elif kind == "corner_whitening":
            wear.append(SimWear(kind, corner=corner, size_mm=float(rng.uniform(1.5, 3)), depth_mm=0.8,
                                density=0.6, side=side, seed=int(rng.integers(1000))))
        else:
            wear.append(SimWear(kind, corner=corner, size_mm=float(rng.uniform(0.8, 2.0))))
    return wear


def random_sim_defects(count: int, seed: int | None = None) -> list[SimDefect]:
    """Random visible scratches and dents on the front, away from the edges."""
    rng = np.random.default_rng(seed)
    defects = []
    for _ in range(count):
        x, y = rng.uniform(10, 53), rng.uniform(10, 78)
        if rng.random() < 0.7:
            angle, length = rng.uniform(0, np.pi), rng.uniform(4, 20)
            defects.append(SimDefect("scratch", x, y, float(np.clip(x + length * np.cos(angle), 6, 57)),
                                     float(np.clip(y + length * np.sin(angle), 6, 82)),
                                     width_mm=float(rng.uniform(0.12, 0.25)), slope=float(rng.uniform(0.04, 0.08))))
        else:
            defects.append(SimDefect("dent", x, y, width_mm=float(rng.uniform(0.6, 1.5)), slope=float(rng.uniform(0.04, 0.07))))
    return defects


RAKING_DIRECTIONS = {  # where the light comes FROM, in image coordinates (y down)
    LightingMode.RAKING_NORTH: (0.0, -1.0),
    LightingMode.RAKING_SOUTH: (0.0, 1.0),
    LightingMode.RAKING_EAST: (1.0, 0.0),
    LightingMode.RAKING_WEST: (-1.0, 0.0),
}
RAKING_ELEVATION = 0.25  # light vector z component (≈ 14°): grazing
RAKING_LEVEL = 0.7  # raking images are darker than diffuse ones
RAKING_FALLOFF = 0.2  # the side nearer the light is brighter


def _height_map(spec: SimCardSpec, side: Side, shape: tuple[int, int]) -> np.ndarray | None:
    defects = [d for d in spec.defects if d.side == side]
    if not defects:
        return None
    h, w = shape
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    height = np.zeros(shape, np.float32)
    for d in defects:
        sigma = max(d.width_mm * SIM_PX_PER_MM / 2, 0.5)
        x1, y1 = d.x_mm * SIM_PX_PER_MM, d.y_mm * SIM_PX_PER_MM
        if d.kind == "scratch":
            x2, y2 = d.x2_mm * SIM_PX_PER_MM, d.y2_mm * SIM_PX_PER_MM
            vx, vy = x2 - x1, y2 - y1
            t = np.clip(((xx - x1) * vx + (yy - y1) * vy) / max(vx * vx + vy * vy, 1e-6), 0, 1)
            dist2 = (xx - (x1 + t * vx)) ** 2 + (yy - (y1 + t * vy)) ** 2
        else:
            dist2 = (xx - x1) ** 2 + (yy - y1) ** 2
        # Gaussian groove/pit of width sigma; max slope ≈ 0.6 · amplitude / sigma.
        height -= (d.slope * sigma / 0.6) * np.exp(-dist2 / (2 * sigma * sigma))
    return height


def _shade(face: np.ndarray, spec: SimCardSpec, side: Side, lighting: LightingMode) -> np.ndarray:
    raking = lighting in RAKING_DIRECTIONS
    height = _height_map(spec, side, face.shape[:2])
    if not raking and height is None:
        return face
    h, w = face.shape[:2]
    if height is None:
        gx = gy = np.zeros((h, w), np.float32)
    else:
        gy, gx = np.gradient(height)
    norm = np.sqrt(gx * gx + gy * gy + 1)
    if not raking:  # diffuse light from above: only the (tiny) tilt of the normal matters
        return np.clip(face * (1 / norm)[..., None], 0, 255).astype(np.uint8)
    # Light direction from canvas to face coordinates (the card is rotated on the tray).
    theta = np.radians(spec.rotation_deg)
    cx, cy = RAKING_DIRECTIONS[lighting]
    lx = np.cos(theta) * cx - np.sin(theta) * cy
    ly = np.sin(theta) * cx + np.cos(theta) * cy
    light = np.array([lx, ly, RAKING_ELEVATION]) / np.linalg.norm([lx, ly, RAKING_ELEVATION])
    lambert = (-gx * light[0] - gy * light[1] + light[2]) / norm / light[2]  # 1 on a flat surface
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    towards_light = ((xx / w - 0.5) * lx + (yy / h - 0.5) * ly) * 2  # -1 far side .. 1 near side
    factor = RAKING_LEVEL * np.clip(lambert, 0, None) * (1 + RAKING_FALLOFF * towards_light)
    return np.clip(face * factor[..., None], 0, 255).astype(np.uint8)


@dataclass
class SimTable:
    """State of the virtual tray, shared between the simulated camera and the sim API."""

    card: SimCardSpec | None = field(default_factory=SimCardSpec)
    side_up: Side = Side.FRONT
    # Calibration target lying on the tray instead of a card (ChArUco board or white sheet).
    target: SimBoardPose | SimWhiteSheet | None = None
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def place(self, card: SimCardSpec | None = None, side_up: Side = Side.FRONT) -> None:
        with self.lock:
            self.card = card or SimCardSpec()
            self.side_up = side_up
            self.target = None

    def place_target(self, target: SimBoardPose | SimWhiteSheet | None) -> None:
        with self.lock:
            self.target = target

    def flip(self) -> None:
        with self.lock:
            self.side_up = Side.BACK if self.side_up == Side.FRONT else Side.FRONT

    def remove(self) -> None:
        with self.lock:
            self.card = None


class SimulatedLighting(Lighting):
    def __init__(self, tint_bgr: tuple[float, float, float] = (1.0, 1.0, 1.0), vignetting: float = 0.0) -> None:
        self._mode = LightingMode.OFF
        self.tint_bgr = tint_bgr  # LED colour cast
        self.vignetting = vignetting  # brightness loss at the field corners (0.2 = -20 %)

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
        return cv2.resize(spec.front_image[..., :3], (w, h), interpolation=cv2.INTER_CUBIC)
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
        alpha = _card_alpha(spec, side, face.shape[:2])
        image = spec.front_image
        if side == Side.FRONT and image is not None and image.ndim == 3 and image.shape[2] == 4:
            # Catalog scan with transparency: its own outline is the card outline.
            # Only fully opaque pixels: the semi-transparent anti-aliased rim is not card.
            scan_alpha = cv2.resize(image[..., 3], (face.shape[1], face.shape[0]), interpolation=cv2.INTER_AREA)
            opaque = cv2.erode(np.where(scan_alpha >= 250, 255, 0).astype(np.uint8), np.ones((3, 3), np.uint8))
            alpha = np.minimum(alpha, opaque)
        face = _shade(_apply_wear(face, alpha, spec, side), spec, side, lighting)
        h, w = face.shape[:2]
        cx, cy = CANVAS_W / 2, CANVAS_H / 2
        m = cv2.getRotationMatrix2D((w / 2, h / 2), spec.rotation_deg, 1.0)
        m[0, 2] += cx - w / 2
        m[1, 2] += cy - h / 2
        mask = cv2.warpAffine(alpha, m, (CANVAS_W, CANVAS_H))
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
    def __init__(self, table: SimTable, lighting: SimulatedLighting, lens: SimLens | None = None) -> None:
        self.table = table
        self.lighting = lighting
        self.lens = lens
        self.colour_gains_bgr = (1.0, 1.0, 1.0)

    @property
    def colour_gains(self) -> tuple[float, float, float]:
        return self.colour_gains_bgr

    def set_colour_gains(self, gains_bgr: tuple[float, float, float]) -> None:
        self.colour_gains_bgr = tuple(gains_bgr)

    def capture(self) -> np.ndarray:
        with self.table.lock:
            spec, side, target = self.table.card, self.table.side_up, self.table.target
        mode = self.lighting.mode
        if isinstance(target, SimBoardPose):
            frame = render_board(target, mode)
        elif isinstance(target, SimWhiteSheet):
            frame = render_white_sheet(target, mode)
        else:
            frame = render_scene(spec, side, mode)
        if mode not in (LightingMode.OFF, LightingMode.BACKLIGHT):
            frame = _apply_light(frame, self.lighting, self.colour_gains_bgr)
        if self.lens is not None:
            frame = self.lens.distort(frame)
        return frame


def _apply_light(frame: np.ndarray, lighting: SimulatedLighting, gains: tuple[float, float, float]) -> np.ndarray:
    factor = np.array(lighting.tint_bgr, np.float32) * np.array(gains, np.float32)
    if np.allclose(factor, 1.0) and lighting.vignetting == 0:
        return frame
    out = frame.astype(np.float32) * factor
    if lighting.vignetting:
        yy, xx = np.mgrid[0:CANVAS_H, 0:CANVAS_W].astype(np.float32)
        r2 = ((xx - CANVAS_W / 2) ** 2 + (yy - CANVAS_H / 2) ** 2) / ((CANVAS_W / 2) ** 2 + (CANVAS_H / 2) ** 2)
        out *= (1 - lighting.vignetting * r2)[..., None]
    return np.clip(out, 0, 255).astype(np.uint8)


# -- optics: the canvas is the ideal pinhole image of the tray seen from SIM_DISTANCE_MM -----

SIM_DISTANCE_MM = 250.0
SIM_FOCAL_PX = SIM_PX_PER_MM * SIM_DISTANCE_MM  # gives 10 px/mm on the tray plane


def sim_camera_matrix() -> np.ndarray:
    return np.array([[SIM_FOCAL_PX, 0, CANVAS_W / 2], [0, SIM_FOCAL_PX, CANVAS_H / 2], [0, 0, 1]], np.float64)


@dataclass
class SimLens:
    """OpenCV distortion model applied to the ideal image. A short focal lens such as the 6 mm
    CS lens shows strong barrel distortion (k1 < 0)."""

    dist: tuple[float, float, float, float, float] = (-0.6, 0.2, 0.0, 0.0, 0.0)
    _maps: tuple | None = field(default=None, repr=False)

    def distort(self, ideal: np.ndarray) -> np.ndarray:
        if self._maps is None:
            k = sim_camera_matrix()
            yy, xx = np.mgrid[0:CANVAS_H, 0:CANVAS_W].astype(np.float32)
            pts = np.stack([xx.ravel(), yy.ravel()], axis=1).reshape(-1, 1, 2)
            # For each pixel of the distorted output: where it comes from in the ideal image.
            src = cv2.undistortPoints(pts, k, np.array(self.dist), P=k).reshape(CANVAS_H, CANVAS_W, 2)
            self._maps = (src[..., 0].astype(np.float32), src[..., 1].astype(np.float32))
        # Replicate edges: a real lens just sees a bit more of the tray, it adds no black frame.
        return cv2.remap(ideal, self._maps[0], self._maps[1], cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)


@dataclass
class SimBoardPose:
    """A printed ChArUco board on the tray: centre offset (mm) and tilt (degrees)."""

    spec: object  # calibration.BoardSpec (kept untyped so the HAL does not depend on it)
    x_mm: float = 0.0
    y_mm: float = 0.0
    tilt_x_deg: float = 0.0
    tilt_y_deg: float = 0.0
    rotation_deg: float = 0.0


@dataclass
class SimWhiteSheet:
    """White reference card covering the field, for white balance and uniformity."""

    level: float = 225.0


PAPER_MARGIN_MM = 8.0
BOARD_RASTER_PX_PER_MM = 12.0


def render_board(pose: SimBoardPose, lighting: LightingMode) -> np.ndarray:
    spec = pose.spec
    bw, bh = spec.squares_x * spec.square_mm, spec.squares_y * spec.square_mm
    pw, ph = bw + 2 * PAPER_MARGIN_MM, bh + 2 * PAPER_MARGIN_MM
    s = BOARD_RASTER_PX_PER_MM
    paper = np.full((int(ph * s), int(pw * s)), 245, np.uint8)
    board = spec.board().generateImage((int(bw * s), int(bh * s)), marginSize=0)
    m = int(PAPER_MARGIN_MM * s)
    paper[m : m + board.shape[0], m : m + board.shape[1]] = board

    # Paper point (u, v, 0) mm -> camera: X = R @ ((u, v, 0) - centre) + t, i.e. K [r1 r2 t'].
    rot = cv2.Rodrigues(np.radians([pose.tilt_x_deg, pose.tilt_y_deg, pose.rotation_deg]))[0]
    t = np.array([pose.x_mm, pose.y_mm, SIM_DISTANCE_MM])
    t_prime = t - rot @ np.array([pw / 2, ph / 2, 0.0])
    homography = sim_camera_matrix() @ np.column_stack([rot[:, 0], rot[:, 1], t_prime]) @ np.diag([1 / s, 1 / s, 1])
    canvas = np.full((CANVAS_H, CANVAS_W), 14, np.uint8)
    warped = cv2.warpPerspective(paper, homography, (CANVAS_W, CANVAS_H), flags=cv2.INTER_AREA)
    mask = cv2.warpPerspective(np.full(paper.shape, 255, np.uint8), homography, (CANVAS_W, CANVAS_H))
    canvas[mask > 127] = warped[mask > 127]
    if lighting == LightingMode.OFF:
        canvas = (canvas * 0.05).astype(np.uint8)
    return cv2.cvtColor(canvas, cv2.COLOR_GRAY2BGR)


def render_white_sheet(sheet: SimWhiteSheet, lighting: LightingMode) -> np.ndarray:
    rng = np.random.default_rng(3)
    frame = np.clip(rng.normal(sheet.level, 2, (CANVAS_H, CANVAS_W, 3)), 0, 255).astype(np.uint8)
    return (frame * 0.05).astype(np.uint8) if lighting == LightingMode.OFF else frame


def build_simulated_hardware(table: SimTable | None = None, lens: SimLens | None = None,
                             lighting: SimulatedLighting | None = None) -> Hardware:
    lighting = lighting or SimulatedLighting()
    camera = SimulatedCamera(table or SimTable(), lighting, lens)
    return Hardware(camera=camera, lighting=lighting, simulated=True, has_backlight=True)
