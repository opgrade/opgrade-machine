import io

import numpy as np
import pytest
from fastapi.testclient import TestClient
from pypdf import PdfReader

from helpers import create_client, create_order, login
from opgrade.api import create_app
from opgrade.config import DEFAULT_RULES_PATH, Settings
from opgrade.grading import corner_points, edge_points, load_rules, suggest_corners_grade, suggest_edges_grade
from opgrade.hardware import LightingMode, Side
from opgrade.hardware.sim import SimCardSpec, SimWear, render_scene
from opgrade.vision import find_card_quad, rectify
from opgrade.vision.wear import analyze_wear

WEAR = [
    SimWear("edge_whitening", edge="right", start_mm=20, end_mm=45, depth_mm=0.7, density=0.6, side=Side.BACK),
    SimWear("corner_whitening", corner="tl", size_mm=2.5, depth_mm=1.0, density=0.7, side=Side.BACK),
    SimWear("corner_ding", corner="br", size_mm=1.5),  # front br = back bl (card flipped)
]


def shoot(spec, side, noise=0.0, with_silhouette=True):
    rng = np.random.default_rng(0)

    def capture(mode):
        image = render_scene(spec, side, mode).astype(np.float32)
        image += rng.normal(0, noise, image.shape) if noise else 0
        return np.clip(image, 0, 255).astype(np.uint8)

    silhouette = capture(LightingMode.BACKLIGHT)
    quad = find_card_quad(silhouette, 63, 88, silhouette=True)
    diffuse = rectify(capture(LightingMode.DIFFUSE), quad, 63, 88).warped
    return diffuse, (rectify(silhouette, quad, 63, 88).warped if with_silhouette else None)


def worn(result):
    corners = {c.name: c for c in result.corners if c.whitening_mm2 > 0 or c.missing_mm2 > 0}
    edges = {e.name: e for e in result.edges if e.whitened_length_mm > 0 or e.nicks}
    return corners, edges


@pytest.mark.parametrize("side", [Side.FRONT, Side.BACK])
@pytest.mark.parametrize("noise", [0.0, 2.0])
def test_clean_card_has_no_wear(side, noise):
    assert worn(analyze_wear(*shoot(SimCardSpec(), side, noise))) == ({}, {})


@pytest.mark.parametrize("noise", [0.0, 2.0])
def test_wear_found_where_it_is(noise):
    corners, edges = worn(analyze_wear(*shoot(SimCardSpec(wear=WEAR), Side.BACK, noise)))
    assert set(corners) == {"tl", "bl"} and set(edges) == {"right"}
    assert corners["tl"].whitening_mm2 > 1.0 and corners["tl"].missing_mm2 == 0
    assert corners["bl"].missing_mm2 > 1.0  # the dinged corner, seen from the back
    assert edges["right"].whitened_length_mm == pytest.approx(25, abs=1.5)  # whitened from 20 to 45 mm
    front_corners, _ = worn(analyze_wear(*shoot(SimCardSpec(wear=WEAR), Side.FRONT, noise)))
    assert set(front_corners) == {"br"}  # same ding, front side


def test_yellow_border_whitening_is_seen():
    """On a bright saturated border the white stock is not brighter: the loss of colour shows it."""
    spec = SimCardSpec(wear=[SimWear("corner_whitening", corner="tr", size_mm=2.5, depth_mm=1.0, density=0.7)])
    corners, _ = worn(analyze_wear(*shoot(spec, Side.FRONT)))
    assert corners["tr"].whitening_mm2 > 1.0


def test_without_silhouette_missing_material_is_not_checked():
    result = analyze_wear(*shoot(SimCardSpec(wear=WEAR), Side.FRONT, with_silhouette=False))
    assert not result.outline_checked
    assert all(c.missing_mm2 == 0 for c in result.corners)


def test_light_border_is_reported_unmeasurable():
    face = np.full((880, 630, 3), 225, np.uint8)  # white-bordered card: whitening cannot show
    face[60:820, 50:580] = (120, 60, 40)
    spec = SimCardSpec(front_image=face)
    result = analyze_wear(*shoot(spec, Side.FRONT))
    assert not any(c.measurable for c in result.corners)


def test_points_and_suggestions():
    rules = load_rules(DEFAULT_RULES_PATH)
    assert corner_points(rules, 0.0, 0.0) == 0
    assert corner_points(rules, 0.6, 0.0) == 2
    assert corner_points(rules, 0.0, 1.3) == 8
    assert edge_points(rules, 25, 0) == 4 and edge_points(rules, 0, 2) == 4
    assert suggest_corners_grade(rules, 0) == 10 and suggest_corners_grade(rules, 12) == 5
    assert suggest_edges_grade(rules, 1) == 9


# -- through the API --------------------------------------------------------------------------


@pytest.fixture
def ctx(tmp_path):
    app = create_app(Settings(simulation=True, data_dir=tmp_path, raking_capture=False))
    with TestClient(app) as api:
        login(api)
        yield api, app


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


def suggestion(s, name):
    return next(m["value"] for m in s["measurements"] if m["name"] == name)


def test_worn_card_end_to_end(ctx):
    api, app = ctx
    s = run_grading(api, app, SimCardSpec(wear=WEAR))
    wear = [d for d in s["defects"] if d["criterion"] in ("corners", "edges")]
    assert {(d["side"], d["criterion"], d["location"], d["kind"]) for d in wear} >= {
        ("front", "corners", "br", "missing"),
        ("back", "corners", "bl", "missing"),
        ("back", "corners", "tl", "whitening"),
        ("back", "edges", "right", "whitening"),
    }
    assert suggestion(s, "corners_suggested_grade") <= 5  # two corners lost material
    assert suggestion(s, "edges_suggested_grade") <= 7
    assert "Coins et bordures" in s["last_message"]
    assert api.get(f"/api/sessions/{s['id']}/analysis/back.png").status_code == 200

    api.post(f"/api/sessions/{s['id']}/review", json={"corners": 5, "edges": 7, "surface": 9})
    text = "\n".join(p.extract_text() for p in PdfReader(io.BytesIO(api.get(f"/api/sessions/{s['id']}/report.pdf").content)).pages)
    assert "Défauts détectés" in text and "matière manquante" in text and "blanchiment" in text


def test_clean_card_suggests_10_for_corners_and_edges(ctx):
    api, app = ctx
    s = run_grading(api, app, SimCardSpec())
    assert suggestion(s, "corners_suggested_grade") == 10 and suggestion(s, "edges_suggested_grade") == 10
    assert not [d for d in s["defects"] if d["criterion"] in ("corners", "edges")]


def test_card_type_corner_radius_is_configurable(ctx):
    api, _ = ctx
    r = api.post("/api/card-types", json={"code": "square", "name": "Coins carrés", "width_mm": 63, "height_mm": 88,
                                          "corner_radius_mm": 0})
    assert r.status_code == 201 and r.json()["corner_radius_mm"] == 0
