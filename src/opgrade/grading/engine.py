"""Grading engine: turns measurements and subgrades into a final 1–10 grade, with explanations."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from pydantic import BaseModel, model_validator


class CenteringStep(BaseModel):
    max_share: float
    grade: float


class CenteringRules(BaseModel):
    front: list[CenteringStep]
    back: list[CenteringStep]
    fallback_grade: float = 1


class SurfaceStep(BaseModel):
    max_score: float
    grade: float


class SurfaceSuggestion(BaseModel):
    back_weight: float = 0.5
    steps: list[SurfaceStep]
    fallback_grade: float = 1


class PointStep(BaseModel):
    min: float
    points: int


class CornersSuggestion(BaseModel):
    whitening_mm2: list[PointStep]
    missing_mm2: list[PointStep]
    steps: list[SurfaceStep]
    fallback_grade: float = 1


class EdgesSuggestion(BaseModel):
    whitening_mm: list[PointStep]
    nick_points: int = 2
    steps: list[SurfaceStep]
    fallback_grade: float = 1


class Scale(BaseModel):
    min: float = 1
    max: float = 10


class GradingRules(BaseModel):
    version: str
    scale: Scale = Scale()
    criteria: list[str]
    weights: dict[str, float]
    cap_above_min_subgrade: float = 1
    centering: CenteringRules
    review_confidence_threshold: float = 0.8
    surface_suggestion: SurfaceSuggestion | None = None
    corners_suggestion: CornersSuggestion | None = None
    edges_suggestion: EdgesSuggestion | None = None

    @model_validator(mode="after")
    def _check(self) -> "GradingRules":
        if set(self.weights) != set(self.criteria):
            raise ValueError("weights must define every criterion")
        if not math.isclose(sum(self.weights.values()), 1.0, abs_tol=1e-6):
            raise ValueError("weights must sum to 1")
        return self


def load_rules(path: Path) -> GradingRules:
    with open(path, encoding="utf-8") as f:
        return GradingRules.model_validate(yaml.safe_load(f))


def _grade_from_table(share: float, table: list[CenteringStep], fallback: float) -> float:
    for step in sorted(table, key=lambda s: s.max_share):
        if share <= step.max_share:
            return step.grade
    return fallback


def centering_grade(rules: GradingRules, front_worst_share: float, back_worst_share: float) -> float:
    c = rules.centering
    return min(
        _grade_from_table(front_worst_share, c.front, c.fallback_grade),
        _grade_from_table(back_worst_share, c.back, c.fallback_grade),
    )


def _grade_for_score(steps: list[SurfaceStep], fallback: float, score: float) -> float:
    for step in sorted(steps, key=lambda st: st.max_score):
        if score <= step.max_score:
            return step.grade
    return fallback


def _points(steps: list[PointStep], value: float) -> int:
    """Points of the highest threshold reached (0 below the first one)."""
    return max((s.points for s in steps if value >= s.min), default=0)


def suggest_surface_grade(rules: GradingRules, front_score: float, back_score: float) -> float | None:
    """Surface grade proposed to the operator from raking-light defect points (None if no rule)."""
    s = rules.surface_suggestion
    if s is None:
        return None
    return _grade_for_score(s.steps, s.fallback_grade, front_score + s.back_weight * back_score)


def corner_points(rules: GradingRules, whitening_mm2: float, missing_mm2: float) -> int:
    s = rules.corners_suggestion
    return 0 if s is None else _points(s.whitening_mm2, whitening_mm2) + _points(s.missing_mm2, missing_mm2)


def edge_points(rules: GradingRules, whitened_length_mm: float, nicks: int) -> int:
    s = rules.edges_suggestion
    return 0 if s is None else _points(s.whitening_mm, whitened_length_mm) + s.nick_points * nicks


def suggest_corners_grade(rules: GradingRules, total_points: float) -> float | None:
    s = rules.corners_suggestion
    return None if s is None else _grade_for_score(s.steps, s.fallback_grade, total_points)


def suggest_edges_grade(rules: GradingRules, total_points: float) -> float | None:
    s = rules.edges_suggestion
    return None if s is None else _grade_for_score(s.steps, s.fallback_grade, total_points)


@dataclass
class FinalGrade:
    grade: int
    weighted_mean: float
    cap: float
    explanation: list[str] = field(default_factory=list)


def compute_final_grade(rules: GradingRules, subgrades: dict[str, float]) -> FinalGrade:
    missing = [c for c in rules.criteria if c not in subgrades]
    if missing:
        raise ValueError(f"missing subgrades: {', '.join(missing)}")
    for name, value in subgrades.items():
        if not rules.scale.min <= value <= rules.scale.max:
            raise ValueError(f"subgrade {name}={value} out of scale")

    weighted = sum(rules.weights[c] * subgrades[c] for c in rules.criteria)
    lowest_name = min(rules.criteria, key=lambda c: subgrades[c])
    cap = subgrades[lowest_name] + rules.cap_above_min_subgrade
    # Conservative: never round up. Epsilon guards against float error (e.g. 8.999999).
    grade = math.floor(min(weighted, cap) + 1e-9)
    grade = int(max(rules.scale.min, min(rules.scale.max, grade)))

    explanation = [f"Moyenne pondérée des sous-notes : {weighted:.2f}."]
    if cap < weighted:
        explanation.append(
            f"Note plafonnée à {cap:g} : la sous-note la plus faible ({lowest_name} = "
            f"{subgrades[lowest_name]:g}) limite la note finale."
        )
    explanation.append(f"Note finale (arrondie à l'inférieur) : {grade}. Règles version {rules.version}.")
    return FinalGrade(grade=grade, weighted_mean=round(weighted, 3), cap=cap, explanation=explanation)
