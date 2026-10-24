"""Saving, activating and loading calibrations."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from ..data.models import Calibration
from .charuco import BoardSpec, IntrinsicsResult, ScaleResult
from .color import ColorResult


@dataclass
class ActiveCalibration:
    """What acquisition needs, as numpy arrays."""

    id: int
    camera_matrix: np.ndarray
    dist_coeffs: np.ndarray
    image_size: tuple[int, int]  # (width, height) the calibration was made at
    px_per_mm: float
    colour_gains: tuple[float, float, float] | None

    @classmethod
    def from_model(cls, cal: Calibration) -> "ActiveCalibration":
        return cls(
            id=cal.id,
            camera_matrix=np.array(cal.camera_matrix, np.float64),
            dist_coeffs=np.array(cal.dist_coeffs, np.float64),
            image_size=(cal.image_width, cal.image_height),
            px_per_mm=cal.px_per_mm,
            colour_gains=tuple(cal.colour_gains) if cal.colour_gains else None,
        )


def save_calibration(
    db: Session,
    spec: BoardSpec,
    intrinsics: IntrinsicsResult,
    scale: ScaleResult,
    color: ColorResult | None,
    machine_id: str,
    created_by: str | None,
) -> Calibration:
    warnings = intrinsics.problems() + scale.problems() + (color.problems() if color else [])
    cal = Calibration(
        created_by=created_by,
        machine_id=machine_id,
        active=False,
        board=spec.as_dict(),
        image_width=intrinsics.image_size[0],
        image_height=intrinsics.image_size[1],
        camera_matrix=intrinsics.camera_matrix.tolist(),
        dist_coeffs=intrinsics.dist_coeffs.ravel().tolist(),
        rms_px=round(intrinsics.rms_px, 4),
        views=intrinsics.views,
        px_per_mm=round(scale.px_per_mm, 4),
        scale_variation=round(scale.variation, 5),
        colour_gains=[round(g, 4) for g in color.gains_bgr] if color else None,
        uniformity=round(color.uniformity, 4) if color else None,
        warnings=warnings,
    )
    db.add(cal)
    db.flush()
    return cal


def activate(db: Session, cal: Calibration) -> None:
    db.execute(update(Calibration).where(Calibration.id != cal.id).values(active=False))
    cal.active = True


def load_active(db: Session) -> ActiveCalibration | None:
    cal = db.scalar(select(Calibration).where(Calibration.active.is_(True)))
    return ActiveCalibration.from_model(cal) if cal else None
