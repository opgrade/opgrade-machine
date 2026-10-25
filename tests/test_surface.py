import io

import numpy as np
import pytest
from fastapi.testclient import TestClient
from pypdf import PdfReader

from helpers import create_client, create_order, login
from opgrade.api import create_app
from opgrade.config import DEFAULT_RULES_PATH, Settings
from opgrade.grading import load_rules, suggest_surface_grade
from opgrade.hardware import LightingMode, Side
from opgrade.hardware.sim import SimCardSpec, SimDefect, render_scene
from opgrade.vision import find_card_quad, rectify
from opgrade.vision.surface import analyze_surface

RAKING = [LightingMode.RAKING_NORTH, LightingMode.RAKING_SOUTH, LightingMode.RAKING_EAST, LightingMode.RAKING_WEST]
DEFECTS = [
    SimDefect("scratch", 15, 30, 40, 45, width_mm=0.2, slope=0.06),  # diagonal, 29.2 mm
    SimDefect("scratch", 20, 60, 50, 60, width_mm=0.15, slope=0.05),  # horizontal, 30 mm
    SimDefect("dent", 45, 20, width_mm=1.0, slope=0.06),
]


def shoot(spec: SimCardSpec, lights=RAKING, noise: float = 0.0, side=Side.FRONT):
    rng = np.random.default_rng(0)

    def capture(mode):
        image = render_scene(spec, side, mode).astype(np.float32)
        image += rng.normal(0, noise, image.shape) if noise else 0
        return np.clip(image, 0, 255).astype(np.uint8)

    quad = find_card_quad(capture(LightingMode.BACKLIGHT), 63, 88, silhouette=True)
    diffuse = rectify(capture(LightingMode.DIFFUSE), quad, 63, 88).warped
    return diffuse, {m.value: rectify(capture(m), quad, 63, 88).warped for m in lights}


@pytest.mark.parametrize("noise", [0.0, 2.0])
def test_clean_card_has_no_defect(noise):
    assert analyze_surface(*shoot(SimCardSpec(), noise=noise)).defects == []


@pytest.mark.parametrize("noise", [0.0, 2.0])
def test_scratches_and_dent_found(noise):
    result = analyze_surface(*shoot(SimCardSpec(defects=DEFECTS), noise=noise))
    assert len(result.defects) == 3
    scratches = sorted((d for d in result.defects if d.kind == "scratch"), key=lambda d: d.y_mm)
    assert len(scratches) == 2
    assert scratches[0].length_mm == pytest.approx(29.2, abs=1.5)  # diagonal scratch
    assert all(d.severity == "major" for d in scratches)  # ~30 mm long
    dent = next(d for d in result.defects if d.kind == "mark")
    assert (dent.x_mm + dent.w_mm / 2, dent.y_mm + dent.h_mm / 2) == pytest.approx((45, 20), abs=1.5)


def test_diffuse_light_alone_does_not_show_relief():
    spec = SimCardSpec(defects=DEFECTS)
    clean = render_scene(SimCardSpec(), Side.FRONT, LightingMode.DIFFUSE).astype(int)
    damaged = render_scene(spec, Side.FRONT, LightingMode.DIFFUSE).astype(int)
    assert np.abs(damaged - clean).max() <= 2  # why raking light is needed


def test_a_scratch_parallel_to_the_light_is_missed():
    """Why 4 directions: east/west light runs along a horizontal scratch and does not show it."""
    spec = SimCardSpec(defects=[SimDefect("scratch", 15, 60, 45, 60, width_mm=0.15, slope=0.05)])
    side_lights = analyze_surface(*shoot(spec, [LightingMode.RAKING_EAST, LightingMode.RAKING_WEST]))
    all_lights = analyze_surface(*shoot(spec))
    # At most the scratch's tips show up as dots (the groove ends slope along the light).
    assert not any(d.kind == "scratch" for d in side_lights.defects)
    assert all(d.length_mm < 1.0 for d in side_lights.defects)
    assert [(d.kind, round(d.length_mm)) for d in all_lights.defects] == [("scratch", 30)]


def test_surface_grade_suggestion():
    rules = load_rules(DEFAULT_RULES_PATH)
    assert suggest_surface_grade(rules, 0, 0) == 10
    assert suggest_surface_grade(rules, 1, 0) == 9
    assert suggest_surface_grade(rules, 0, 2) == 9  # back counts half
    assert suggest_surface_grade(rules, 15, 0) == 4
    assert suggest_surface_grade(rules, 100, 0) == 3


# -- through the API --------------------------------------------------------------------------


def run_grading(api, app, spec):
    c = create_client(api)
    o = create_order(api, c["id"])
    ptype = next(t["id"] for t in api.get("/api/card-types").json() if t["code"] == "pokemon")
    card = api.post("/api/cards", json={"client_id": c["id"], "order_id": o["id"], "card_type_id": ptype, "name": "Mew"}).json()
    app.state.hardware.camera.table.place(spec)
    sid = api.post("/api/sessions", json={"card_id": card["id"]}).json()["id"]
    api.post(f"/api/sessions/{sid}/capture")
    api.post("/api/sim/flip")
    api.post(f"/api/sessions/{sid}/capture")
    return api.post(f"/api/sessions/{sid}/analyze").json()


@pytest.fixture
def ctx(tmp_path):
    app = create_app(Settings(simulation=True, data_dir=tmp_path))
    with TestClient(app) as api:
        login(api)
        yield api, app


def test_multi_light_grading_end_to_end(ctx):
    api, app = ctx
    s = run_grading(api, app, SimCardSpec(defects=DEFECTS))
    lights = {(c["side"], c["lighting"]) for c in s["captures"]}
    assert len(lights) == 12  # backlit outline + diffuse + 4 raking, both faces
    surface = [d for d in s["defects"] if d["criterion"] == "surface"]
    assert len([d for d in surface if d["side"] == "front"]) == 3 and not [d for d in surface if d["side"] == "back"]
    suggested = next(m["value"] for m in s["measurements"] if m["name"] == "surface_suggested_grade")
    assert suggested == 4  # 2 major + 1 moderate on the front
    assert "Surface : 3 défaut(s)" in s["last_message"]
    image = api.get(f"/api/sessions/{s['id']}/analysis/front.png")
    assert image.status_code == 200 and image.headers["content-type"] == "image/png"

    done = api.post(f"/api/sessions/{s['id']}/review", json={"corners": 9, "edges": 9, "surface": suggested}).json()
    text = "\n".join(p.extract_text() for p in PdfReader(io.BytesIO(api.get(f"/api/sessions/{s['id']}/report.pdf").content)).pages)
    assert "Défauts détectés" in text and "Surface recto" in text and "rayure" in text and "majeur" in text
    assert done["final_grade"] <= 5  # surface 4 caps the final grade at 5


def test_clean_card_suggests_10(ctx):
    api, app = ctx
    s = run_grading(api, app, SimCardSpec())
    assert not [d for d in s["defects"] if d["criterion"] == "surface"]
    assert next(m["value"] for m in s["measurements"] if m["name"] == "surface_suggested_grade") == 10


def test_raking_capture_can_be_disabled(tmp_path):
    app = create_app(Settings(simulation=True, data_dir=tmp_path, raking_capture=False))
    with TestClient(app) as api:
        login(api)
        s = run_grading(api, app, SimCardSpec(defects=DEFECTS))
    assert {c["lighting"] for c in s["captures"]} == {"diffuse", "backlight"}  # outline still kept
    assert not any(m["name"] == "surface_suggested_grade" for m in s["measurements"])
