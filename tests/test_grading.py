import pytest

from opgrade.config import DEFAULT_RULES_PATH
from opgrade.grading import centering_grade, compute_final_grade, load_rules

RULES = load_rules(DEFAULT_RULES_PATH)


@pytest.mark.parametrize(
    "front,back,expected",
    [(50, 50, 10), (55, 70, 10), (58, 50, 9), (64, 50, 8), (50, 78, 9), (97, 50, 1)],
)
def test_centering_grade(front, back, expected):
    assert centering_grade(RULES, front, back) == expected


def test_perfect_card_is_10():
    assert compute_final_grade(RULES, dict(centering=10, corners=10, edges=10, surface=10)).grade == 10


def test_weighted_mean_rounds_down():
    # 0.15*10 + 0.30*9 + 0.25*9 + 0.30*9 = 9.15 -> 9
    assert compute_final_grade(RULES, dict(centering=10, corners=9, edges=9, surface=9)).grade == 9


def test_weakest_subgrade_caps_final():
    final = compute_final_grade(RULES, dict(centering=10, corners=10, edges=10, surface=4))
    assert final.weighted_mean > 8
    assert final.grade == 5
    assert any("plafonnée" in line for line in final.explanation)


def test_missing_or_out_of_scale_subgrade_rejected():
    with pytest.raises(ValueError):
        compute_final_grade(RULES, dict(centering=10, corners=10, edges=10))
    with pytest.raises(ValueError):
        compute_final_grade(RULES, dict(centering=10, corners=10, edges=10, surface=11))
