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
