"""Grading workflow: drives hardware, vision and grading through the session state machine."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import cv2
import numpy as np
from sqlalchemy.orm import Session

from ..calibration.charuco import undistort
from ..calibration.store import ActiveCalibration
from ..config import Settings
from ..customers.orders import refresh_status
from ..data.models import Capture, Card, CardType, Defect, GradingSession, Measurement, Subgrade
from ..grading import (
    GradingRules,
    centering_grade,
    compute_final_grade,
    corner_points,
    edge_points,
    suggest_corners_grade,
    suggest_edges_grade,
    suggest_surface_grade,
)
from ..hardware.base import Hardware, LightingMode, Side
from ..reports.certificates import assign_certificate
from ..vision import (
    CardDetection,
    DetectionError,
    QualityReport,
    assess_quality,
    detect_card,
    find_card_quad,
    looks_like_same_side,
    measure_centering,
    rectify,
)
from ..vision.surface import analyze_surface, annotate_surface
from ..vision.wear import CORNER_ZONE_MM, analyze_wear, annotate_wear
from .state_machine import CAPTURE_SIDE, INSTRUCTIONS, SessionState, check_transition

log = logging.getLogger(__name__)
DEFAULT_CORNER_RADIUS_MM = 3.0
OPERATOR_CRITERIA = ("corners", "edges", "surface")


class WorkflowError(Exception):
    """Operation not allowed in the current state. Message is operator-facing (French)."""


class AcquisitionError(Exception):
    """No usable card image (no card, wrong format, blur…). Message is operator-facing (French)."""


RAKING_LIGHTS = (LightingMode.RAKING_NORTH, LightingMode.RAKING_SOUTH, LightingMode.RAKING_EAST, LightingMode.RAKING_WEST)


@dataclass
class Acquisition:
    raw: np.ndarray  # main (diffuse) photo, lens-corrected
    detection: CardDetection  # card outline + rectified main photo
    quality: QualityReport
    # Same card position under other lights, rectified with the SAME outline: pixel-aligned.
    extra: dict[LightingMode, np.ndarray] = field(default_factory=dict)
    silhouette: np.ndarray | None = None  # rectified backlit outline (corner/edge shape check)


@dataclass
class CaptureOutcome:
    accepted: bool
    message: str
    capture: Capture | None = None


def analysis_image_path(gs: GradingSession, side: Side) -> str:
    """Annotated analysis image (surface relief + numbered defects, corner/edge wear), relative path."""
    return (Path("images") / gs.uuid / f"{side.value}_analysis.png").as_posix()


def _severity(points: int) -> str:
    return "minor" if points <= 1 else "moderate" if points <= 3 else "major"


class GradingService:
    def __init__(self, settings: Settings, hardware: Hardware, rules: GradingRules) -> None:
        self.settings = settings
        self.hardware = hardware
        self.rules = rules
        self.calibration: ActiveCalibration | None = None

    def set_calibration(self, calibration: ActiveCalibration | None) -> None:
        """Use this calibration for every following capture (lens correction, white balance)."""
        self.calibration = calibration
        if calibration and calibration.colour_gains:
            self.hardware.camera.set_colour_gains(calibration.colour_gains)

    def _correct_lens(self, image):
        cal = self.calibration
        if cal is None:
            return image
        if (image.shape[1], image.shape[0]) != cal.image_size:
            log.warning("Calibration faite en %s, image %s : correction de l'objectif ignorée.",
                        cal.image_size, (image.shape[1], image.shape[0]))
            return image
        return undistort(image, cal.camera_matrix, cal.dist_coeffs)

    # -- helpers ---------------------------------------------------------------------------

    def _get(self, db: Session, session_id: int) -> GradingSession:
        gs = db.get(GradingSession, session_id)
        if gs is None:
            raise WorkflowError("Session introuvable.")
        return gs

    def _move(self, gs: GradingSession, target: SessionState, message: str | None = None) -> None:
        check_transition(SessionState(gs.state), target)
        gs.state = target.value
        gs.last_message = message or INSTRUCTIONS[target]

    def _abs(self, rel: str) -> Path:
        return self.settings.data_dir / rel

    def _load_warped(self, gs: GradingSession, side: Side, lighting: LightingMode = LightingMode.DIFFUSE):
        cap = next((c for c in gs.captures if c.side == side.value and c.lighting == lighting.value), None)
        if cap is None:
            raise WorkflowError(f"Face manquante : {side.value}.")
        img = cv2.imread(str(self._abs(cap.warped_path)))
        if img is None:
            raise WorkflowError(f"Image illisible : {cap.warped_path}.")
        return img

    def acquire(self, card_type: CardType, lighting: LightingMode = LightingMode.DIFFUSE,
                extra_lights: tuple[LightingMode, ...] = ()) -> Acquisition:
        """Light, shoot, find and rectify the card, check image quality, then (optionally) shoot
        the same position under `extra_lights`. Raises AcquisitionError with the operator message.
        """
        silhouette = None
        extra_raw: dict[LightingMode, np.ndarray] = {}
        try:
            if self.hardware.has_backlight:
                # Silhouette first: locates any card, even black-bordered ones on the black tray.
                self.hardware.lighting.set_mode(LightingMode.BACKLIGHT)
                silhouette = self.hardware.camera.capture()
            self.hardware.lighting.set_mode(lighting)
            raw = self.hardware.camera.capture()
            for light in extra_lights:
                self.hardware.lighting.set_mode(light)
                extra_raw[light] = self.hardware.camera.capture()
        finally:
            self.hardware.lighting.off()
        # Straight edges and true proportions before any measurement (6 mm lens: strong barrel).
        raw = self._correct_lens(raw)
        if silhouette is not None:
            silhouette = self._correct_lens(silhouette)
        w, h = card_type.width_mm, card_type.height_mm
        try:
            if silhouette is not None:
                detection = rectify(raw, find_card_quad(silhouette, w, h, silhouette=True), w, h)
            else:
                detection = detect_card(raw, w, h)
        except DetectionError as exc:
            raise AcquisitionError(str(exc)) from exc
        quality = assess_quality(detection.warped)
        if not quality.ok:
            raise AcquisitionError(" ".join(quality.problems))
        extra = {light: rectify(self._correct_lens(image), detection.quad, w, h).warped
                 for light, image in extra_raw.items()}
        rectified_silhouette = rectify(silhouette, detection.quad, w, h).warped if silhouette is not None else None
        return Acquisition(raw, detection, quality, extra, rectified_silhouette)

    # -- workflow --------------------------------------------------------------------------

    def start_session(self, db: Session, card_id: int, operator: str | None = None) -> GradingSession:
        card = db.get(Card, card_id)
        if card is None:
            raise WorkflowError("Carte introuvable.")
        if card.order is not None and card.order.status in ("returned", "cancelled"):
            raise WorkflowError("La commande de cette carte est retournée ou annulée.")
        gs = GradingSession(
            card=card,
            machine_id=self.settings.machine_id,
            state=SessionState.AWAIT_FRONT.value,
            simulated=self.hardware.simulated,
            operator=operator,
            rules_version=self.rules.version,
            calibration_id=self.calibration.id if self.calibration else None,
            model_version=None,  # no AI model in V1
            last_message=INSTRUCTIONS[SessionState.AWAIT_FRONT],
        )
        db.add(gs)
        refresh_status(card.order)
        db.commit()
        return gs

    def capture(self, db: Session, session_id: int) -> CaptureOutcome:
        gs = self._get(db, session_id)
        state = SessionState(gs.state)
        side = CAPTURE_SIDE.get(state)
        if side is None:
            raise WorkflowError("Aucune capture n'est attendue à cette étape.")

        lighting = LightingMode.DIFFUSE
        extra_lights = RAKING_LIGHTS if self.settings.raking_capture else ()
        try:
            acq = self.acquire(gs.card.card_type, lighting, extra_lights)
        except AcquisitionError as exc:
            return self._reject(db, gs, str(exc))
        raw, detection, quality = acq.raw, acq.detection, acq.quality

        if side == Side.BACK and looks_like_same_side(self._load_warped(gs, Side.FRONT), detection.warped):
            return self._reject(db, gs, "La carte ne semble pas avoir été retournée. RETOURNEZ la carte puis recommencez.")

        rel_dir = Path("images") / gs.uuid
        self._abs(str(rel_dir)).mkdir(parents=True, exist_ok=True)
        raw_rel = (rel_dir / f"{side.value}_{lighting.value}_raw.png").as_posix()
        warped_rel = (rel_dir / f"{side.value}_{lighting.value}.png").as_posix()
        cv2.imwrite(str(self._abs(raw_rel)), raw)
        cv2.imwrite(str(self._abs(warped_rel)), detection.warped)

        cap = Capture(
            side=side.value,
            lighting=lighting.value,
            raw_path=raw_rel,
            warped_path=warped_rel,
            width_px=detection.warped.shape[1],
            height_px=detection.warped.shape[0],
            sharpness=quality.sharpness,
        )
        gs.captures.append(cap)
        images = dict(acq.extra)
        if acq.silhouette is not None:
            images[LightingMode.BACKLIGHT] = acq.silhouette
        for light, warped in images.items():
            rel = (rel_dir / f"{side.value}_{light.value}.png").as_posix()
            cv2.imwrite(str(self._abs(rel)), warped)
            # Raking: only the rectified image is kept (a raw 12 MP PNG per light is too slow on the Pi).
            gs.captures.append(Capture(side=side.value, lighting=light.value, raw_path=rel, warped_path=rel,
                                       width_px=warped.shape[1], height_px=warped.shape[0],
                                       sharpness=quality.sharpness))
        self._move(gs, SessionState.AWAIT_BACK if side == Side.FRONT else SessionState.READY_TO_ANALYZE)
        db.commit()
        return CaptureOutcome(True, gs.last_message or "", cap)

    def _reject(self, db: Session, gs: GradingSession, reason: str) -> CaptureOutcome:
        gs.last_message = f"Capture refusée : {reason}"
        db.commit()
        return CaptureOutcome(False, gs.last_message)

    def analyze(self, db: Session, session_id: int) -> GradingSession:
        gs = self._get(db, session_id)
        if SessionState(gs.state) != SessionState.READY_TO_ANALYZE:
            raise WorkflowError("Les deux faces doivent être capturées avant l'analyse.")

        results = {side: measure_centering(self._load_warped(gs, side)) for side in (Side.FRONT, Side.BACK)}
        for side, res in results.items():
            for name, value, unit in (
                ("centering_left_share", res.left_share, "%"),
                ("centering_top_share", res.top_share, "%"),
                ("centering_worst_share", res.worst_share, "%"),
                ("centering_confidence", res.confidence, "ratio"),
            ):
                gs.measurements.append(Measurement(side=side.value, name=name, value=round(value, 3), unit=unit))

        front, back = results[Side.FRONT], results[Side.BACK]
        confidence = min(front.confidence, back.confidence)
        gs.subgrades.append(
            Subgrade(
                criterion="centering",
                value=centering_grade(self.rules, front.worst_share, back.worst_share),
                source="auto",
                confidence=confidence,
                details={"front": front.as_ratio_text(), "back": back.as_ratio_text()},
            )
        )
        gs.confidence = confidence
        gs.needs_review = True  # corners/edges/surface come from the operator in V1
        message = INSTRUCTIONS[SessionState.AWAIT_REVIEW]
        annotated = {side: self._load_warped(gs, side) for side in (Side.FRONT, Side.BACK)}
        surface = self._analyze_surface(gs, annotated)
        if surface is not None:
            message += f" Surface : {surface} défaut(s) détecté(s) sous éclairage rasant."
        wear, unmeasurable = self._analyze_wear(gs, annotated)
        message += f" Coins et bordures : {wear} usure(s) détectée(s)."
        if unmeasurable:
            message += f" {unmeasurable} coin(s)/bordure(s) à bordure claire : blanchiment non mesurable, vérifiez à l'œil."
        for side, image in annotated.items():
            cv2.imwrite(str(self._abs(analysis_image_path(gs, side))), image)
        if confidence < self.rules.review_confidence_threshold:
            message += " ATTENTION : centrage mesuré avec une faible confiance, vérifiez-le."
        self._move(gs, SessionState.AWAIT_REVIEW, message)
        db.commit()
        return gs

    def _analyze_surface(self, gs: GradingSession, annotated: dict) -> int | None:
        """Raking-light defects on both faces; stores them and a SUGGESTED surface grade (the
        operator still decides). Returns the number of defects, or None without raking captures."""
        scores = {}
        for side in (Side.FRONT, Side.BACK):
            raking = {
                light.value: self._load_warped(gs, side, light)
                for light in RAKING_LIGHTS
                if any(c.side == side.value and c.lighting == light.value for c in gs.captures)
            }
            if not raking:
                return None
            diffuse = self._load_warped(gs, side)
            result = analyze_surface(diffuse, raking)
            for d in result.defects:
                gs.defects.append(Defect(side=side.value, criterion="surface", source="auto", **{
                    k: getattr(d, k) for k in ("kind", "x_mm", "y_mm", "w_mm", "h_mm", "length_mm", "area_mm2",
                                               "contrast", "severity", "lighting")}))
            annotated[side] = annotate_surface(annotated[side], result)
            scores[side] = result.score
            gs.measurements.append(Measurement(side=side.value, name="surface_defect_score",
                                               value=result.score, unit="points"))
        suggestion = suggest_surface_grade(self.rules, scores[Side.FRONT], scores[Side.BACK])
        if suggestion is not None:
            gs.measurements.append(Measurement(side=None, name="surface_suggested_grade",
                                               value=suggestion, unit="grade"))
        return sum(1 for d in gs.defects if d.criterion == "surface")

    def _analyze_wear(self, gs: GradingSession, annotated: dict) -> tuple[int, int]:
        """Corner and edge wear on both faces: stores defects and SUGGESTED corner/edge grades.
        Returns (number of wear defects, number of corners/edges that could not be judged)."""
        radius = gs.card.card_type.corner_radius_mm or DEFAULT_CORNER_RADIUS_MM
        corner_total = edge_total = 0
        count = unmeasurable = 0
        for side in (Side.FRONT, Side.BACK):
            diffuse = self._load_warped(gs, side)
            has_silhouette = any(c.side == side.value and c.lighting == LightingMode.BACKLIGHT.value for c in gs.captures)
            silhouette = self._load_warped(gs, side, LightingMode.BACKLIGHT) if has_silhouette else None
            result = analyze_wear(diffuse, silhouette, radius)
            annotated[side] = annotate_wear(annotated[side], result)
            side_points = {"corners": 0, "edges": 0}
            z = CORNER_ZONE_MM
            fw, fh = diffuse.shape[1] / 10, diffuse.shape[0] / 10  # face size, mm
            origin = {"tl": (0, 0), "tr": (fw - z, 0), "br": (fw - z, fh - z), "bl": (0, fh - z)}
            for c in result.corners:
                unmeasurable += not c.measurable
                x, y = origin[c.name]
                for kind, value, pts in (
                    ("whitening", c.whitening_mm2, corner_points(self.rules, c.whitening_mm2, 0)),
                    ("missing", c.missing_mm2, corner_points(self.rules, 0, c.missing_mm2)),
                ):
                    if pts:
                        side_points["corners"] += pts
                        gs.defects.append(Defect(side=side.value, criterion="corners", kind=kind, x_mm=x, y_mm=y,
                                                 w_mm=z, h_mm=z, length_mm=0.0, area_mm2=round(value, 3), contrast=0.0,
                                                 severity=_severity(pts), lighting="backlight" if kind == "missing" else "diffuse",
                                                 location=c.name, source="auto"))
            for e in result.edges:
                unmeasurable += not e.measurable
                for kind, pts, length, area in (
                    ("whitening", edge_points(self.rules, e.whitened_length_mm, 0), e.whitened_length_mm, 0.0),
                    ("nick", edge_points(self.rules, 0, e.nicks), 0.0, e.nick_area_mm2),
                ):
                    if pts:
                        side_points["edges"] += pts
                        gs.defects.append(Defect(side=side.value, criterion="edges", kind=kind, x_mm=0.0, y_mm=0.0,
                                                 w_mm=0.0, h_mm=0.0, length_mm=round(length, 2), area_mm2=round(area, 3),
                                                 contrast=0.0, severity=_severity(pts),
                                                 lighting="backlight" if kind == "nick" else "diffuse",
                                                 location=e.name, source="auto"))
            for criterion, value in side_points.items():
                gs.measurements.append(Measurement(side=side.value, name=f"{criterion}_wear_score", value=value, unit="points"))
            corner_total += side_points["corners"]
            edge_total += side_points["edges"]
            count += sum(1 for d in gs.defects if d.side == side.value and d.criterion in ("corners", "edges"))
        for name, grade in (("corners_suggested_grade", suggest_corners_grade(self.rules, corner_total)),
                            ("edges_suggested_grade", suggest_edges_grade(self.rules, edge_total))):
            if grade is not None:
                gs.measurements.append(Measurement(side=None, name=name, value=grade, unit="grade"))
        if unmeasurable:
            gs.measurements.append(Measurement(side=None, name="wear_unmeasurable", value=unmeasurable, unit="count"))
        return count, unmeasurable

    def submit_review(
        self,
        db: Session,
        session_id: int,
        operator_grades: dict[str, float],
        centering_override: float | None = None,
        operator: str | None = None,
    ) -> GradingSession:
        gs = self._get(db, session_id)
        if SessionState(gs.state) != SessionState.AWAIT_REVIEW:
            raise WorkflowError("La session n'attend pas de révision.")
        missing = [c for c in OPERATOR_CRITERIA if c not in operator_grades]
        if missing:
            raise WorkflowError(f"Notes manquantes : {', '.join(missing)}.")

        centering = next(s for s in gs.subgrades if s.criterion == "centering")
        if centering_override is not None and centering_override != centering.value:
            centering.details = {**(centering.details or {}), "auto_value": centering.value}
            centering.value = centering_override
            centering.source = "operator"

        for criterion in OPERATOR_CRITERIA:
            gs.subgrades.append(Subgrade(criterion=criterion, value=operator_grades[criterion], source="operator"))

        try:
            final = compute_final_grade(self.rules, {s.criterion: s.value for s in gs.subgrades})
        except ValueError as exc:
            raise WorkflowError(f"Notes invalides : {exc}") from exc

        gs.final_grade = final.grade
        gs.explanation = final.explanation
        gs.needs_review = False
        if operator:
            gs.operator = operator
        gs.completed_at = datetime.now(timezone.utc)
        assign_certificate(db, gs)
        self._move(gs, SessionState.COMPLETED, f"Note finale : {final.grade}/10. {INSTRUCTIONS[SessionState.COMPLETED]}")
        refresh_status(gs.card.order)
        db.commit()
        return gs

    def cancel(self, db: Session, session_id: int) -> GradingSession:
        gs = self._get(db, session_id)
        self._move(gs, SessionState.CANCELLED)
        refresh_status(gs.card.order)
        db.commit()
        return gs
