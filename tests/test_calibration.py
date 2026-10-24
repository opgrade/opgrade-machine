import random
import sys
import types

import cv2
import numpy as np
import pytest
from fastapi.testclient import TestClient

from helpers import create_client, create_order, login
from opgrade.api import create_app
from opgrade.calibration import BoardSpec, board_png, calibrate, detect, measure_reference, tray_scale, undistort
from opgrade.config import Settings
from opgrade.hardware import LightingMode, Side
from opgrade.hardware.sim import (
    SimBoardPose,
    SimCardSpec,
    SimLens,
    SimulatedLighting,
    render_board,
    render_scene,
    sim_camera_matrix,
)
from opgrade.vision import find_card_quad, measure_centering, rectify

SPEC = BoardSpec()
LENS = SimLens()


def views(n=16, seed=1):
    rng = random.Random(seed)
    flat = detect(LENS.distort(render_board(SimBoardPose(SPEC), LightingMode.DIFFUSE)), SPEC)
    out = [flat]
    for _ in range(n):
        pose = SimBoardPose(SPEC, rng.uniform(-25, 25), rng.uniform(-30, 30), rng.uniform(-20, 20),
                            rng.uniform(-20, 20), rng.uniform(-15, 15))
        out.append(detect(LENS.distort(render_board(pose, LightingMode.DIFFUSE)), SPEC))
    return flat, out


@pytest.fixture(scope="module")
def result():
    flat, all_views = views()
    return flat, calibrate(all_views, SPEC, (1000, 1200))


# -- ChArUco ---------------------------------------------------------------------------------


def test_board_detection_and_png_size():
    d = detect(render_board(SimBoardPose(SPEC), LightingMode.DIFFUSE), SPEC)
    assert d.count == SPEC.corner_count == 48
    png = cv2.imdecode(np.frombuffer(board_png(SPEC, px_per_mm=10), np.uint8), cv2.IMREAD_GRAYSCALE)
    assert png.shape == (9 * 12 * 10, 7 * 12 * 10)  # exact size: printable at scale


def test_calibration_recovers_the_simulated_lens(result):
    _, cal = result
    assert cal.rms_px < 0.3 and not cal.problems()
    assert cal.camera_matrix[0, 0] == pytest.approx(sim_camera_matrix()[0, 0], rel=0.01)
    assert cal.dist_coeffs[0, 0] == pytest.approx(LENS.dist[0], abs=0.05)
    # k2/k3 trade off against each other: compare the actual correction instead.
    pts = np.array([[[50.0, 60.0]], [[950.0, 1140.0]], [[500.0, 100.0]]], np.float32)
    ours = cv2.undistortPoints(pts, cal.camera_matrix, cal.dist_coeffs, P=cal.camera_matrix)
    truth = cv2.undistortPoints(pts, sim_camera_matrix(), np.array(LENS.dist), P=sim_camera_matrix())
    assert np.abs(ours - truth).max() < 1.0  # < 1 px anywhere, field corners included


def test_tray_scale_and_tilt_check(result):
    flat, cal = result
    scale = tray_scale(flat, SPEC, cal.camera_matrix, cal.dist_coeffs)
    assert scale.px_per_mm == pytest.approx(10.0, rel=0.005) and not scale.problems()
    tilted = detect(LENS.distort(render_board(SimBoardPose(SPEC, tilt_x_deg=8), LightingMode.DIFFUSE)), SPEC)
    assert tray_scale(tilted, SPEC, cal.camera_matrix, cal.dist_coeffs).problems()  # camera not square


def test_too_few_views_refused():
    _, few = views(n=5)
    with pytest.raises(ValueError, match="au moins"):
        calibrate(few, SPEC, (1000, 1200))


def test_lens_correction_restores_card_measurements(result):
    """Why calibrate: a short lens shrinks the card and shifts centering by ~1 point."""
    _, cal = result
    spec = SimCardSpec(front_left_share=58, front_top_share=50, rotation_deg=2)

    def measure(fix):
        sil = fix(LENS.distort(render_scene(spec, Side.FRONT, LightingMode.BACKLIGHT)))
        photo = fix(LENS.distort(render_scene(spec, Side.FRONT, LightingMode.DIFFUSE)))
        quad = find_card_quad(sil, 63, 88, silhouette=True)
        return np.linalg.norm(quad[3] - quad[0]) / 10, measure_centering(rectify(photo, quad, 63, 88).warped)

    height_raw, raw = measure(lambda im: im)
    height_fixed, fixed = measure(lambda im: undistort(im, cal.camera_matrix, cal.dist_coeffs))
    assert abs(height_raw - 88) > 1.0 and abs(height_fixed - 88) < 0.3
    assert abs(raw.top_share - 50) > abs(fixed.top_share - 50)
    assert fixed.top_share == pytest.approx(50, abs=0.5) and fixed.left_share == pytest.approx(58, abs=1.0)


# -- colour ----------------------------------------------------------------------------------


def test_white_balance_and_uniformity():
    tinted = np.full((300, 400, 3), 200, np.float32) * np.array([1.12, 1.0, 0.88], np.float32)
    result = measure_reference(np.clip(tinted, 0, 255).astype(np.uint8))
    corrected = np.clip(tinted * np.array(result.gains_bgr, np.float32), 0, 255)
    assert np.ptp(corrected.reshape(-1, 3).mean(axis=0)) < 1.5  # neutral grey after gains
    assert result.uniformity > 0.99 and not result.problems()

    yy, xx = np.mgrid[0:300, 0:400]
    vignette = (1 - 0.3 * (((xx - 200) / 200) ** 2 + ((yy - 150) / 150) ** 2) / 2)[..., None]
    dim = measure_reference((np.full((300, 400, 3), 200.0) * vignette).astype(np.uint8))
    assert dim.uniformity < 0.85 and any("uniforme" in p for p in dim.problems())


# -- wizard (API, simulation with realistic optics) -------------------------------------------


@pytest.fixture
def admin_api(tmp_path):
    settings = Settings(simulation=True, data_dir=tmp_path, sim_optics=True)
    with TestClient(create_app(settings)) as api:
        login(api)
        yield api


def test_calibration_wizard_end_to_end(admin_api):
    api = admin_api
    assert api.get("/api/calibration/state").json() == {"run": None, "active": None}
    assert api.get("/api/calibration/board.pdf").content.startswith(b"%PDF")
    api.post("/api/calibration/start", json={"square_mm": 12.0})
    assert api.post("/api/calibration/compute").status_code == 409  # flat view missing

    api.post("/api/calibration/sim-target", json={"kind": "flat"})
    assert api.get("/api/calibration/focus").json()["corners"] > 40
    assert api.post("/api/calibration/capture", json={"kind": "flat"}).json()["accepted"]
    accepted = 0
    for _ in range(20):
        api.post("/api/calibration/sim-target", json={"kind": "random"})
        accepted += api.post("/api/calibration/capture", json={"kind": "view"}).json()["accepted"]
    assert accepted >= 14

    api.post("/api/calibration/sim-target", json={"kind": "white"})
    white = api.post("/api/calibration/white").json()
    assert white["gains_bgr"][0] < 0.95 and white["gains_bgr"][2] > 1.05  # undoes the LED tint
    assert white["uniformity"] < 0.95  # simulated vignetting is measured

    cal = api.post("/api/calibration/compute").json()
    assert cal["rms_px"] < 0.5 and cal["px_per_mm"] == pytest.approx(10.0, rel=0.01)
    assert cal["active"] is False
    assert api.post(f"/api/calibrations/{cal['id']}/activate").json()["active"] is True
    assert api.get("/api/calibration/state").json()["active"]["id"] == cal["id"]

    # Following gradings are corrected and traced to this calibration.
    api.post("/api/calibration/sim-target", json={"kind": "none"})
    c = create_client(api)
    o = create_order(api, c["id"])
    ptype = next(t["id"] for t in api.get("/api/card-types").json() if t["code"] == "pokemon")
    card = api.post("/api/cards", json={"client_id": c["id"], "order_id": o["id"], "card_type_id": ptype, "name": "Mew"}).json()
    api.post("/api/sim/place", json={})
    s = api.post("/api/sessions", json={"card_id": card["id"]}).json()
    assert s["calibration_id"] == cal["id"]
    assert api.post(f"/api/sessions/{s['id']}/capture").json()["accepted"]
    actions = {e["action"] for e in api.get("/api/audit").json()}
    assert {"calibration_computed", "calibration_activated"} <= actions


def test_calibration_is_admin_only(tmp_path):
    with TestClient(create_app(Settings(simulation=True, data_dir=tmp_path))) as api:
        login(api, "operator", "marc")
        for path in ("/api/calibration/state", "/api/calibrations", "/api/calibration/focus"):
            assert api.get(path).status_code == 403


def test_active_calibration_reloaded_at_startup(tmp_path):
    settings = Settings(simulation=True, data_dir=tmp_path, sim_optics=True)
    with TestClient(create_app(settings)) as api:
        login(api)
        api.post("/api/calibration/start", json={"square_mm": 12.0})
        api.post("/api/calibration/sim-target", json={"kind": "flat"})
        api.post("/api/calibration/capture", json={"kind": "flat"})
        for _ in range(14):
            api.post("/api/calibration/sim-target", json={"kind": "random"})
            api.post("/api/calibration/capture", json={"kind": "view"})
        cal_id = api.post("/api/calibration/compute").json()["id"]
        api.post(f"/api/calibrations/{cal_id}/activate")
    app = create_app(settings)  # machine restart
    assert app.state.service.calibration.id == cal_id


# -- Raspberry Pi camera driver (picamera2 faked: checks what is sent to it) ---------------


class FakePicamera2:
    instances = []

    def __init__(self):
        self.controls, self.released, self.config = {}, 0, None
        FakePicamera2.instances.append(self)

    def create_still_configuration(self, main, buffer_count):
        return {"main": main, "buffer_count": buffer_count}

    def configure(self, config):
        self.config = config

    def set_controls(self, controls):
        self.controls.update(controls)

    def start(self):
        pass

    def capture_request(self):
        fake = self

        class Request:
            def release(self_inner):
                fake.released += 1

        return Request()

    def capture_array(self, name):
        return np.zeros((3040, 4056, 3), np.uint8)

    def capture_metadata(self):
        return {"ColourGains": self.controls.get("ColourGains", (2.0, 1.5))}

    @staticmethod
    def global_camera_info():
        return [{"Model": "imx477"}]


def test_pi_camera_driver(monkeypatch):
    monkeypatch.setitem(sys.modules, "picamera2", types.SimpleNamespace(Picamera2=FakePicamera2))
    from opgrade.hardware.pi import PiCamera

    cam = PiCamera()
    fake = FakePicamera2.instances[-1]
    # "RGB888" is BGR in memory (what OpenCV expects); "BGR888" would swap red and blue.
    assert fake.config["main"] == {"size": (4056, 3040), "format": "RGB888"}
    assert fake.controls["AeEnable"] is False and fake.controls["AwbEnable"] is False
    before = fake.released
    assert cam.capture().shape == (3040, 4056, 3)
    assert fake.released - before == cam.settings.settle_frames  # frames exposed before the light change
    assert cam.colour_gains == (1.5, 1.0, 2.0)  # B, G, R from ColourGains (red, blue)
    cam.set_colour_gains((1.2, 1.0, 1.8))
    assert fake.controls["ColourGains"] == (1.8, 1.2)
