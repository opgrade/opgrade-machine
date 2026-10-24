"""States of a grading session (V1: manual flip guided by the machine)."""

from __future__ import annotations

from enum import Enum

from ..hardware.base import Side


class SessionState(str, Enum):
    AWAIT_FRONT = "await_front"
    AWAIT_BACK = "await_back"
    READY_TO_ANALYZE = "ready_to_analyze"
    AWAIT_REVIEW = "await_review"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    ERROR = "error"


TERMINAL_STATES = {SessionState.COMPLETED, SessionState.CANCELLED, SessionState.ERROR}

_ALLOWED: dict[SessionState, set[SessionState]] = {
    SessionState.AWAIT_FRONT: {SessionState.AWAIT_BACK},
    SessionState.AWAIT_BACK: {SessionState.READY_TO_ANALYZE},
    SessionState.READY_TO_ANALYZE: {SessionState.AWAIT_REVIEW},
    SessionState.AWAIT_REVIEW: {SessionState.COMPLETED},
}

# Operator-facing instruction for each state.
INSTRUCTIONS: dict[SessionState, str] = {
    SessionState.AWAIT_FRONT: "Placez la carte RECTO vers le haut contre les butées, puis appuyez sur CAPTURER.",
    SessionState.AWAIT_BACK: "RETOURNEZ la carte (verso vers le haut) contre les butées, puis appuyez sur CAPTURER.",
    SessionState.READY_TO_ANALYZE: "Les deux faces sont capturées. Appuyez sur ANALYSER.",
    SessionState.AWAIT_REVIEW: "Vérifiez le centrage et saisissez les notes coins, bordures et surface.",
    SessionState.COMPLETED: "Gradation terminée. Vous pouvez retirer la carte.",
    SessionState.CANCELLED: "Gradation annulée. Vous pouvez retirer la carte.",
    SessionState.ERROR: "Erreur : intervention requise.",
}

CAPTURE_SIDE: dict[SessionState, Side] = {
    SessionState.AWAIT_FRONT: Side.FRONT,
    SessionState.AWAIT_BACK: Side.BACK,
}


class InvalidTransition(Exception):
    pass


def check_transition(current: SessionState, target: SessionState) -> None:
    if target in (SessionState.CANCELLED, SessionState.ERROR) and current not in TERMINAL_STATES:
        return
    if target not in _ALLOWED.get(current, set()):
        raise InvalidTransition(f"Transition interdite : {current.value} → {target.value}")
