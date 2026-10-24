"""Calibration wizard (admin only): focus, ChArUco views, white balance, compute, activate."""

from __future__ import annotations

import random
import threading
from collections.abc import Callable
from dataclasses import dataclass, field

import cv2
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..auth import audit
from ..calibration import (
    BoardSpec,
    ColorResult,
    Detection,
    annotate,
    board_pdf,
    calibrate,
    detect,
    measure_reference,
    tray_scale,
)
from ..calibration.charuco import MIN_CORNERS_PER_VIEW, MIN_VIEWS, RECOMMENDED_VIEWS
from ..calibration.store import ActiveCalibration, activate, save_calibration
from ..config import Settings
from ..data import Calibration
from ..hardware import LightingMode
from ..hardware.sim import SimBoardPose, SimulatedCamera, SimWhiteSheet
from ..orchestration import GradingService
from .auth import client_ip, current_user, require_admin


class StartIn(BaseModel):
    # Square size MEASURED on the print with a caliper (printers are rarely exactly 100 %).
    square_mm: float = Field(12.0, gt=3, lt=50)
    marker_mm: float | None = Field(default=None, gt=2, lt=50)


class CaptureIn(BaseModel):
    kind: str = Field(pattern="^(flat|view)$")


class SimTargetIn(BaseModel):
    kind: str = Field(pattern="^(flat|random|white|none)$")


@dataclass
class CalibrationRun:
    spec: BoardSpec
    started_by: str
    flat: Detection | None = None
    views: list[Detection] = field(default_factory=list)
    image_size: tuple[int, int] | None = None
    color: ColorResult | None = None
    absolute_gains: tuple[float, float, float] | None = None

    def state(self) -> dict:
        return {
            "board": self.spec.as_dict(),
            "flat_done": self.flat is not None,
            "views": len(self.views) + (1 if self.flat else 0),
            "min_views": MIN_VIEWS,
            "recommended_views": RECOMMENDED_VIEWS,
            "white_done": self.color is not None,
            "uniformity": self.color.uniformity if self.color else None,
        }


def calibration_out(cal: Calibration) -> dict:
    return {
        "id": cal.id, "created_at": cal.created_at, "created_by": cal.created_by, "active": cal.active,
        "rms_px": cal.rms_px, "views": cal.views, "px_per_mm": cal.px_per_mm,
        "scale_variation": cal.scale_variation, "colour_gains": cal.colour_gains, "uniformity": cal.uniformity,
        "image_size": [cal.image_width, cal.image_height], "dist_coeffs": cal.dist_coeffs,
        "focal_px": cal.camera_matrix[0][0], "warnings": cal.warnings or [], "board": cal.board,
    }


def build_router(get_db: Callable, settings: Settings, service: GradingService) -> APIRouter:
    router = APIRouter(dependencies=[Depends(require_admin)])
    hardware = service.hardware
    lock = threading.Lock()
    runs: dict[str, CalibrationRun] = {}  # a single run per machine
    preview_path = settings.data_dir / "calibration" / "preview.jpg"

    def shoot():
        """Raw frame (no lens correction) under the diffuse light."""
        hardware.lighting.set_mode(LightingMode.DIFFUSE)
        try:
            return hardware.camera.capture()
        finally:
            hardware.lighting.off()

    def current_run() -> CalibrationRun:
        run = runs.get("current")
        if run is None:
            raise HTTPException(409, "Démarrez d'abord une calibration.")
        return run

    def save_preview(image) -> None:
        preview_path.parent.mkdir(parents=True, exist_ok=True)
        h, w = image.shape[:2]
        scale = min(1.0, 1200 / max(h, w))
        small = cv2.resize(image, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
        cv2.imwrite(str(preview_path), small, [cv2.IMWRITE_JPEG_QUALITY, 85])

    @router.get("/api/calibration/board.pdf")
    def board(square_mm: float = 12.0, marker_mm: float = 9.0):
        spec = BoardSpec(square_mm=square_mm, marker_mm=marker_mm)
        return Response(board_pdf(spec), media_type="application/pdf",
                        headers={"Content-Disposition": 'inline; filename="opgrade-mire-charuco.pdf"'})

    @router.get("/api/calibration/focus")
    def focus() -> dict:
        """Focus aid: press repeatedly while turning the lens ring; maximise the value."""
        with lock:
            image = shoot()
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        h, w = gray.shape
        centre = gray[h // 3 : 2 * h // 3, w // 3 : 2 * w // 3]
        save_preview(image)
        return {"sharpness": round(float(cv2.Laplacian(centre, cv2.CV_64F).var()), 1),
                "corners": detect(image, BoardSpec()).count}

    @router.get("/api/calibration/preview.jpg")
    def preview():
        if not preview_path.exists():
            raise HTTPException(404, "Aucune image.")
        return FileResponse(preview_path, media_type="image/jpeg", headers={"Cache-Control": "no-store"})

    @router.post("/api/calibration/start")
    def start(body: StartIn, request: Request) -> dict:
        marker = body.marker_mm or round(body.square_mm * 0.75, 3)
        with lock:
            runs["current"] = CalibrationRun(BoardSpec(square_mm=body.square_mm, marker_mm=marker),
                                             current_user(request).username)
            return runs["current"].state()

    @router.get("/api/calibration/state")
    def state(db: Session = Depends(get_db)) -> dict:
        active = db.scalar(select(Calibration).where(Calibration.active.is_(True)))
        run = runs.get("current")
        return {"run": run.state() if run else None, "active": calibration_out(active) if active else None}

    @router.post("/api/calibration/capture")
    def capture(body: CaptureIn) -> dict:
        with lock:
            run = current_run()
            image = shoot()
            d = detect(image, run.spec)
            save_preview(annotate(image, d))
            size = (image.shape[1], image.shape[0])
            if run.image_size and size != run.image_size:
                raise HTTPException(409, "La résolution de la caméra a changé : recommencez la calibration.")
            accepted = d.count >= MIN_CORNERS_PER_VIEW
            if accepted:
                run.image_size = size
                if body.kind == "flat":
                    run.flat = d
                else:
                    run.views.append(d)
            message = (f"{d.count} coins détectés sur {run.spec.corner_count}."
                       if accepted else
                       f"Mire non reconnue ({d.count} coins, {MIN_CORNERS_PER_VIEW} minimum) : vérifiez la "
                       "netteté, l'éclairage et que la mire est entièrement dans le champ.")
            return {"accepted": accepted, "corners": d.count, "message": message, **run.state()}

    @router.post("/api/calibration/white")
    def white() -> dict:
        with lock:
            run = current_run()
            image = shoot()
            save_preview(image)
            color = measure_reference(image)
            current = hardware.camera.colour_gains
            absolute = [c * f for c, f in zip(current, color.gains_bgr)]
            run.color = color
            run.absolute_gains = tuple(g / absolute[1] for g in absolute)  # green = 1
            return {"uniformity": color.uniformity, "mean_level": color.mean_level,
                    "gains_bgr": run.absolute_gains, "problems": color.problems(), **run.state()}

    @router.post("/api/calibration/compute")
    def compute(request: Request, db: Session = Depends(get_db)) -> dict:
        with lock:
            run = current_run()
            if run.flat is None:
                raise HTTPException(409, "La capture à plat (échelle) est obligatoire.")
            try:
                intrinsics = calibrate([run.flat, *run.views], run.spec, run.image_size)
            except ValueError as exc:
                raise HTTPException(409, str(exc)) from exc
            scale = tray_scale(run.flat, run.spec, intrinsics.camera_matrix, intrinsics.dist_coeffs)
            color = run.color
            if color is not None:
                color = ColorResult(run.absolute_gains, color.uniformity, color.mean_level, color.cells)
            cal = save_calibration(db, run.spec, intrinsics, scale, color, settings.machine_id,
                                   current_user(request).username)
            audit(db, current_user(request).username, "calibration_computed", target=f"calibration:{cal.id}",
                  details={"rms_px": cal.rms_px, "px_per_mm": cal.px_per_mm, "views": cal.views},
                  ip=client_ip(request))
            db.commit()
            return calibration_out(cal)

    @router.get("/api/calibrations")
    def list_calibrations(db: Session = Depends(get_db)) -> list[dict]:
        return [calibration_out(c) for c in db.scalars(select(Calibration).order_by(Calibration.id.desc()).limit(20))]

    @router.post("/api/calibrations/{calibration_id}/activate")
    def activate_route(calibration_id: int, request: Request, db: Session = Depends(get_db)) -> dict:
        cal = db.get(Calibration, calibration_id)
        if cal is None:
            raise HTTPException(404, "Calibration introuvable.")
        activate(db, cal)
        audit(db, current_user(request).username, "calibration_activated", target=f"calibration:{cal.id}",
              ip=client_ip(request))
        db.commit()
        service.set_calibration(ActiveCalibration.from_model(cal))
        return calibration_out(cal)

    # -- simulation: put the board or the white sheet on the virtual tray ---------------------

    if hardware.simulated and isinstance(hardware.camera, SimulatedCamera):
        table = hardware.camera.table
        rng = random.Random()

        @router.post("/api/calibration/sim-target")
        def sim_target(body: SimTargetIn) -> dict:
            run = runs.get("current")
            spec = run.spec if run else BoardSpec()
            if body.kind == "flat":
                table.place_target(SimBoardPose(spec))
            elif body.kind == "random":
                table.place_target(SimBoardPose(spec, rng.uniform(-25, 25), rng.uniform(-30, 30),
                                                rng.uniform(-20, 20), rng.uniform(-20, 20), rng.uniform(-15, 15)))
            elif body.kind == "white":
                table.place_target(SimWhiteSheet())
            else:
                table.place_target(None)
            return {"target": body.kind}

    return router
