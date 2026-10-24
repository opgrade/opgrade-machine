"""ChArUco camera calibration: lens intrinsics/distortion, then scale on the tray plane.

Procedure (see docs/RASPBERRY_PI.md):
1. Print the board at 100 % (PDF from `board_pdf`), check the 50 mm control bar with a caliper,
   glue it on a rigid flat support (glass, aluminium, 3 mm acrylic).
2. First capture: board FLAT on the tray, in the card position -> scale (px/mm) and tilt check.
3. 14+ more captures: board moved to every area of the field and tilted 10–20° (wedge) in
   different directions -> focal length and distortion.
"""

from __future__ import annotations

import io
from dataclasses import asdict, dataclass

import cv2
import numpy as np

MIN_CORNERS_PER_VIEW = 12
MIN_VIEWS = 10
RECOMMENDED_VIEWS = 15
GOOD_RMS_PX = 0.5  # reprojection error considered good for a 12 MP sensor
MAX_SCALE_VARIATION = 0.01  # > 1 % across the tray: camera not perpendicular to the tray


@dataclass(frozen=True)
class BoardSpec:
    """Default fits the card area (84 x 108 mm). Square size must be MEASURED on the print."""

    squares_x: int = 7
    squares_y: int = 9
    square_mm: float = 12.0
    marker_mm: float = 9.0
    dictionary: str = "DICT_4X4_50"

    def board(self) -> cv2.aruco.CharucoBoard:
        dictionary = cv2.aruco.getPredefinedDictionary(getattr(cv2.aruco, self.dictionary))
        return cv2.aruco.CharucoBoard((self.squares_x, self.squares_y), self.square_mm, self.marker_mm, dictionary)

    @property
    def corner_count(self) -> int:
        return (self.squares_x - 1) * (self.squares_y - 1)

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class Detection:
    corners: np.ndarray  # (N, 1, 2) float32, image pixels
    ids: np.ndarray  # (N, 1) int32
    markers: int

    @property
    def count(self) -> int:
        return 0 if self.ids is None else len(self.ids)


def detect(image: np.ndarray, spec: BoardSpec) -> Detection:
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if image.ndim == 3 else image
    corners, ids, _, marker_ids = cv2.aruco.CharucoDetector(spec.board()).detectBoard(gray)
    if ids is None or corners is None or len(ids) == 0:
        return Detection(np.empty((0, 1, 2), np.float32), np.empty((0, 1), np.int32), 0)
    # OpenCV 5 returns (N, 2) / (N,), OpenCV 4 (N, 1, 2) / (N, 1): use the classic shapes.
    corners = np.asarray(corners, np.float32).reshape(-1, 1, 2)
    ids = np.asarray(ids, np.int32).reshape(-1, 1)
    return Detection(corners, ids, 0 if marker_ids is None else len(marker_ids))


def annotate(image: np.ndarray, detection: Detection) -> np.ndarray:
    out = image.copy() if image.ndim == 3 else cv2.cvtColor(image, cv2.COLOR_GRAY2BGR)
    if detection.count:
        cv2.aruco.drawDetectedCornersCharuco(out, detection.corners, detection.ids, (0, 200, 0))
    return out


@dataclass
class IntrinsicsResult:
    camera_matrix: np.ndarray  # 3x3
    dist_coeffs: np.ndarray  # (1, 5): k1 k2 p1 p2 k3
    rms_px: float
    per_view_px: list[float]
    image_size: tuple[int, int]  # (width, height)
    views: int

    def problems(self) -> list[str]:
        out = []
        if self.views < RECOMMENDED_VIEWS:
            out.append(f"Seulement {self.views} vues : {RECOMMENDED_VIEWS} ou plus recommandées.")
        if self.rms_px > GOOD_RMS_PX:
            out.append(f"Erreur de reprojection {self.rms_px:.2f} px (> {GOOD_RMS_PX}) : refaire avec une mire bien "
                       "plane, nette et des vues plus variées.")
        return out


def calibrate(detections: list[Detection], spec: BoardSpec, image_size: tuple[int, int]) -> IntrinsicsResult:
    board = spec.board()
    obj_points, img_points = [], []
    for d in detections:
        if d.count < MIN_CORNERS_PER_VIEW:
            continue
        obj, img = board.matchImagePoints(d.corners, d.ids)
        obj_points.append(obj.astype(np.float32))
        img_points.append(img.astype(np.float32))
    if len(obj_points) < MIN_VIEWS:
        raise ValueError(f"Il faut au moins {MIN_VIEWS} vues valides (≥ {MIN_CORNERS_PER_VIEW} coins) ; "
                         f"{len(obj_points)} disponibles.")
    rms, matrix, dist, rvecs, tvecs = cv2.calibrateCamera(obj_points, img_points, image_size, None, None)
    per_view = []
    for obj, img, r, t in zip(obj_points, img_points, rvecs, tvecs):
        projected, _ = cv2.projectPoints(obj, r, t, matrix, dist)
        per_view.append(float(np.sqrt(np.mean(np.sum((projected - img) ** 2, axis=2)))))
    return IntrinsicsResult(matrix, dist.reshape(1, -1)[:, :5], float(rms), per_view, image_size, len(obj_points))


@dataclass
class ScaleResult:
    px_per_mm: float
    variation: float  # relative spread of the local scale across the board (tilt / perspective)
    corners: int

    def problems(self) -> list[str]:
        if self.variation > MAX_SCALE_VARIATION:
            return [f"L'échelle varie de {self.variation * 100:.1f} % sur le plateau : la caméra n'est pas "
                    "perpendiculaire au plateau ou la mire n'est pas à plat."]
        return []


def tray_scale(detection: Detection, spec: BoardSpec, camera_matrix: np.ndarray, dist: np.ndarray) -> ScaleResult:
    """px/mm on the tray plane, from the FLAT view, after removing lens distortion."""
    if detection.count < MIN_CORNERS_PER_VIEW:
        raise ValueError("La vue à plat ne contient pas assez de coins de la mire.")
    pts = cv2.undistortPoints(detection.corners, camera_matrix, dist, P=camera_matrix).reshape(-1, 2)
    nx = spec.squares_x - 1  # inner corners per row; ids are row-major
    position = {int(i): p for i, p in zip(detection.ids.ravel(), pts)}
    scales, midpoints = [], []
    for cid, p in position.items():
        right = cid + 1 if (cid + 1) % nx else None  # no right neighbour on the last column
        for neighbour in (right, cid + nx):
            if neighbour in position:
                q = position[neighbour]
                scales.append(np.linalg.norm(q - p) / spec.square_mm)
                midpoints.append((p + q) / 2)
    scales, midpoints = np.array(scales), np.array(midpoints)
    median = float(np.median(scales))
    # Tilt makes one side of the board look bigger than the other: compare halves (robust to noise).
    gaps = []
    for axis in (0, 1):
        split = np.median(midpoints[:, axis])
        low, high = scales[midpoints[:, axis] < split], scales[midpoints[:, axis] >= split]
        if len(low) and len(high):
            gaps.append(abs(np.median(low) - np.median(high)) / median)
    return ScaleResult(median, float(max(gaps, default=0.0)), detection.count)


def undistort(image: np.ndarray, camera_matrix: np.ndarray, dist: np.ndarray) -> np.ndarray:
    return cv2.undistort(image, camera_matrix, dist)


def board_png(spec: BoardSpec, px_per_mm: float = 24.0) -> bytes:
    """Board image (~600 dpi by default) for printing or for the simulator."""
    w = int(round(spec.squares_x * spec.square_mm * px_per_mm))
    h = int(round(spec.squares_y * spec.square_mm * px_per_mm))
    ok, png = cv2.imencode(".png", spec.board().generateImage((w, h), marginSize=0))
    return png.tobytes()


def board_pdf(spec: BoardSpec) -> bytes:
    """Printable board at exact size with a 50 mm control bar (print at 100 %, no 'fit to page')."""
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.units import mm
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfgen import canvas

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    page_w, page_h = letter
    bw, bh = spec.squares_x * spec.square_mm * mm, spec.squares_y * spec.square_mm * mm
    x, y = (page_w - bw) / 2, page_h - 60 - bh
    c.drawImage(ImageReader(io.BytesIO(board_png(spec))), x, y, bw, bh)
    c.setFont("Helvetica-Bold", 12)
    c.drawString(40, page_h - 36, "OpGrade X - mire de calibration ChArUco")
    c.setFont("Helvetica", 9)
    c.drawString(40, page_h - 50, f"{spec.squares_x} x {spec.squares_y} cases de {spec.square_mm:g} mm, "
                                  f"marqueurs {spec.marker_mm:g} mm, {spec.dictionary}")
    bar_y = y - 30
    c.setLineWidth(2)
    c.line(x, bar_y, x + 50 * mm, bar_y)
    for tick in range(0, 51, 10):
        c.line(x + tick * mm, bar_y - 4, x + tick * mm, bar_y + 4)
    c.drawString(x + 52 * mm, bar_y - 3, "50 mm : vérifier au pied à coulisse")
    lines = [
        "1. Imprimer à 100 % (désactiver « ajuster à la page »), papier mat.",
        "2. Mesurer la barre de 50 mm et une case : saisir la taille réelle de case si elle diffère.",
        "3. Coller bien à plat sur un support rigide (verre, aluminium, acrylique 3 mm).",
        "4. Première capture : mire À PLAT sur le plateau, à l'emplacement de la carte.",
        "5. Puis 14 captures ou plus : mire déplacée dans tout le champ et inclinée de 10 à 20°.",
    ]
    ty = bar_y - 30
    for line in lines:
        c.drawString(40, ty, line)
        ty -= 13
    c.showPage()
    c.save()
    return buf.getvalue()
