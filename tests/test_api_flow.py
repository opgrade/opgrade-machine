import pytest
from fastapi.testclient import TestClient

from opgrade.api import create_app
from opgrade.config import Settings
from helpers import create_client, login


@pytest.fixture
def client(tmp_path):
    app = create_app(Settings(simulation=True, data_dir=tmp_path))
    with TestClient(app) as c:
        login(c)
        yield c


def _new_session(client: TestClient) -> int:
    cust = create_client(client)
    pokemon = next(t for t in client.get("/api/card-types").json() if t["code"] == "pokemon")
    card = client.post(
        "/api/cards", json={"client_id": cust["id"], "card_type_id": pokemon["id"], "name": "Pikachu", "year": 1999}
    ).json()
    return client.post("/api/sessions", json={"card_id": card["id"]}).json()["id"]


def test_full_grading_flow_with_manual_flip(client):
    client.post("/api/sim/place", json={"front_left_share": 58, "front_top_share": 50})
    sid = _new_session(client)

    # Analysis is impossible before both faces are captured.
    assert client.post(f"/api/sessions/{sid}/analyze").status_code == 409

    r = client.post(f"/api/sessions/{sid}/capture").json()
    assert r["accepted"] and r["session"]["state"] == "await_back"
    assert "RETOURNEZ" in r["message"]

    # Operator forgot to flip: back capture must be refused.
    r = client.post(f"/api/sessions/{sid}/capture").json()
    assert not r["accepted"] and r["session"]["state"] == "await_back"

    client.post("/api/sim/flip")
    r = client.post(f"/api/sessions/{sid}/capture").json()
    assert r["accepted"] and r["session"]["state"] == "ready_to_analyze"

    s = client.post(f"/api/sessions/{sid}/analyze").json()
    assert s["state"] == "await_review" and s["needs_review"]
    centering = next(g for g in s["subgrades"] if g["criterion"] == "centering")
    assert centering["value"] == 9  # 58/42 front -> 9
    assert centering["source"] == "auto"

    s = client.post(f"/api/sessions/{sid}/review", json={"corners": 9, "edges": 9, "surface": 10}).json()
    assert s["state"] == "completed"
    assert s["final_grade"] == 9
    assert s["simulated"] is True
    assert s["rules_version"]

    cap_id = s["captures"][0]["id"]
    img = client.get(f"/api/captures/{cap_id}/image")
    assert img.status_code == 200 and img.headers["content-type"] == "image/png"


def test_capture_without_card_is_refused(client):
    client.post("/api/sim/remove")
    sid = _new_session(client)
    r = client.post(f"/api/sessions/{sid}/capture").json()
    assert not r["accepted"]
    assert "Aucune carte" in r["message"]
    assert r["session"]["state"] == "await_front"


def test_invalid_review_grade_rejected(client):
    sid = _new_session(client)
    client.post(f"/api/sessions/{sid}/capture")
    client.post("/api/sim/flip")
    client.post(f"/api/sessions/{sid}/capture")
    client.post(f"/api/sessions/{sid}/analyze")
    assert client.post(f"/api/sessions/{sid}/review", json={"corners": 11, "edges": 9, "surface": 9}).status_code == 422
