import pytest

from opgrade.hardware import LightingMode, Side
from opgrade.hardware.sim import SimCardSpec, render_scene
from opgrade.vision import DetectionError, assess_quality, detect_card, looks_like_same_side, measure_centering


def _warped(spec: SimCardSpec, side: Side = Side.FRONT):
    img = render_scene(spec, side, LightingMode.DIFFUSE)
    return detect_card(img, spec.width_mm, spec.height_mm).warped


@pytest.mark.parametrize(
    "left,top,rotation",
    [(50, 50, 0.0), (60, 45, 1.5), (35, 65, -3.0), (70, 50, 8.0)],
)
def test_centering_matches_simulated_card(left, top, rotation):
    spec = SimCardSpec(front_left_share=left, front_top_share=top, rotation_deg=rotation)
    result = measure_centering(_warped(spec))
    # V1 acceptance target is < 1 point; simulator rendering is the reference here.
    assert result.left_share == pytest.approx(left, abs=1.5)
    assert result.top_share == pytest.approx(top, abs=1.5)
    assert result.confidence > 0.8


def test_back_centering_measured():
    spec = SimCardSpec(back_left_share=40, back_top_share=58)
    result = measure_centering(_warped(spec, Side.BACK))
    assert result.left_share == pytest.approx(40, abs=1.5)
    assert result.top_share == pytest.approx(58, abs=1.5)


def test_no_card_rejected():
    with pytest.raises(DetectionError):
        detect_card(render_scene(None, Side.FRONT, LightingMode.DIFFUSE), 63, 88)


def test_wrong_profile_rejected():
    img = render_scene(SimCardSpec(), Side.FRONT, LightingMode.DIFFUSE)
    with pytest.raises(DetectionError):
        detect_card(img, 70, 70)


def test_blurred_image_fails_quality():
    assert assess_quality(_warped(SimCardSpec())).ok
    assert not assess_quality(_warped(SimCardSpec(blur=6))).ok


def test_dark_image_fails_quality():
    img = render_scene(SimCardSpec(), Side.FRONT, LightingMode.OFF)
    try:
        warped = detect_card(img, 63, 88).warped
    except DetectionError:
        return  # also acceptable: nothing usable detected
    assert not assess_quality(warped).ok


def test_flip_detection():
    spec = SimCardSpec()
    front = _warped(spec, Side.FRONT)
    assert looks_like_same_side(front, _warped(SimCardSpec(rotation_deg=-2), Side.FRONT))
    assert not looks_like_same_side(front, _warped(spec, Side.BACK))
