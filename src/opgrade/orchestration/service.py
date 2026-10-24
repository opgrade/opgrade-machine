"""Grading workflow: drives hardware, vision and grading through the session state machine."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import cv2
from sqlalchemy.orm import Session

from ..config import Settings
from ..data.models import Capture, Card, GradingSession, Measurement, Subgrade
from ..grading import GradingRules, centering_grade, compute_final_grade
from ..hardware.base import Hardware, LightingMode, Side
from ..vision import DetectionError, assess_quality, detect_card, looks_like_same_side, measure_centering
from .state_machine import CAPTURE_SIDE, INSTRUCTIONS, SessionState, check_transition

OPERATOR_CRITERIA = ("corners", "edges", "surface")


class WorkflowError(Exception):
    """Operation not allowed in the current state. Message is operator-facing (French)."""


@dataclass
class CaptureOutcome:
    accepted: bool
    message: str
    capture: Capture | None = None


class GradingService:
    def __init__(self, settings: Settings, hardware: Hardware, rules: GradingRules) -> None:
        self.settings = settings
        self.hardware = hardware
        self.rules = rules

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

    def _load_warped(self, gs: GradingSession, side: Side):
        cap = next((c for c in gs.captures if c.side == side.value), None)
        if cap is None:
            raise WorkflowError(f"Face manquante : {side.value}.")
        img = cv2.imread(str(self._abs(cap.warped_path)))
        if img is None:
            raise WorkflowError(f"Image illisible : {cap.warped_path}.")
        return img

    # -- workflow --------------------------------------------------------------------------

    def start_session(self, db: Session, card_id: int, operator: str | None = None) -> GradingSession:
        if db.get(Card, card_id) is None:
            raise WorkflowError("Carte introuvable.")
        gs = GradingSession(
            card_id=card_id,
            machine_id=self.settings.machine_id,
            state=SessionState.AWAIT_FRONT.value,
            simulated=self.hardware.simulated,
            operator=operator,
            rules_version=self.rules.version,
            model_version=None,  # no AI model in V1
            last_message=INSTRUCTIONS[SessionState.AWAIT_FRONT],
        )
        db.add(gs)
        db.commit()
        return gs

    def capture(self, db: Session, session_id: int) -> CaptureOutcome:
        gs = self._get(db, session_id)
        state = SessionState(gs.state)
        side = CAPTURE_SIDE.get(state)
        if side is None:
            raise WorkflowError("Aucune capture n'est attendue à cette étape.")

        lighting = LightingMode.DIFFUSE
        self.hardware.lighting.set_mode(lighting)
        try:
            raw = self.hardware.camera.capture()
        finally:
            self.hardware.lighting.off()

        card_type = gs.card.card_type
        try:
            detection = detect_card(raw, card_type.width_mm, card_type.height_mm)
        except DetectionError as exc:
            return self._reject(db, gs, str(exc))

        quality = assess_quality(detection.warped)
        if not quality.ok:
            return self._reject(db, gs, " ".join(quality.problems))

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
        if confidence < self.rules.review_confidence_threshold:
            message += " ATTENTION : centrage mesuré avec une faible confiance, vérifiez-le."
        self._move(gs, SessionState.AWAIT_REVIEW, message)
        db.commit()
        return gs

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
        self._move(gs, SessionState.COMPLETED, f"Note finale : {final.grade}/10. {INSTRUCTIONS[SessionState.COMPLETED]}")
        db.commit()
        return gs

    def cancel(self, db: Session, session_id: int) -> GradingSession:
        gs = self._get(db, session_id)
        self._move(gs, SessionState.CANCELLED)
        db.commit()
        return gs
