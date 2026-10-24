import cv2
import httpx
import numpy as np
import pytest
from fastapi.testclient import TestClient

from opgrade.api import create_app
from opgrade.config import Settings
from opgrade.hardware import LightingMode, Side
from opgrade.hardware.sim import SimCardSpec, render_scene
from opgrade.identify import CardCandidate
from opgrade.identify.build_index import build
from opgrade.identify.visual import VisualIndex, _to_sql, fingerprint, popcount64
from opgrade.vision import detect_card


def card_picture(seed: int) -> np.ndarray:
    """A distinct, card-like picture: light border around low-frequency random 'artwork'."""
    rng = np.random.default_rng(seed)
    art = cv2.resize(rng.integers(0, 255, (11, 8, 3), dtype=np.uint8), (220, 310), interpolation=cv2.INTER_CUBIC)
    card = np.full((337, 245, 3), (200, 210, 220), np.uint8)
    card[14:324, 12:232] = art
    return card


def captured(picture: np.ndarray, rotation: float = 3.0, brightness: float = 0.9) -> np.ndarray:
    scene = render_scene(SimCardSpec(front_image=picture, rotation_deg=rotation, brightness=brightness, blur=0.8),
                         Side.FRONT, LightingMode.DIFFUSE)
    return detect_card(scene, 63, 88).warped


def ref(i: int, catalog: str = "tcgdex") -> CardCandidate:
    return CardCandidate(source=catalog, external_id=f"en:set-{i}", name=f"Card {i}", set_name="Set", number=str(i),
                         image_url=f"https://img.test/{i}.png")


def test_popcount_and_signed_storage_roundtrip():
    values = np.array([0, 1, 0xFF, (1 << 64) - 1], np.uint64)
    assert popcount64(values).tolist() == [0, 1, 8, 64]
    h = (1 << 64) - 5
    assert np.array([_to_sql(h)], np.int64).view(np.uint64)[0] == h


def test_fingerprint_survives_capture_but_separates_cards():
    a, b = card_picture(1), card_picture(2)
    fa, fa_captured, fb = fingerprint(a), fingerprint(captured(a)), fingerprint(b)
    same = bin(fa.full ^ fa_captured.full).count("1") + bin(fa.art ^ fa_captured.art).count("1")
    other = bin(fa.full ^ fb.full).count("1") + bin(fa.art ^ fb.art).count("1")
    assert same <= 12 < 30 <= other


def test_index_search_ranks_the_captured_card_first(tmp_path):
    index = VisualIndex(tmp_path / "idx.db")
    index.add_many([(ref(i), fingerprint(card_picture(i))) for i in range(50)])
    index.add_many([(ref(7, "scryfall"), fingerprint(card_picture(99)))])  # other catalog is ignored
    matches = index.search("tcgdex", fingerprint(captured(card_picture(7))), limit=5)
    assert matches[0].candidate.external_id == "en:set-7"
    assert matches[0].confidence == "high"
    assert matches[1].distance > matches[0].distance
    assert index.count("tcgdex") == 50


def test_builder_downloads_records_failures_and_resumes(tmp_path):
    def handler(request: httpx.Request):
        i = int(request.url.path.strip("/").removesuffix(".png"))
        if i == 3:
            return httpx.Response(404)
        ok, png = cv2.imencode(".png", card_picture(i))
        return httpx.Response(200, content=png.tobytes())

    client = httpx.Client(transport=httpx.MockTransport(handler))
    index = VisualIndex(tmp_path / "idx.db")
    refs = [(ref(i), f"https://img.test/{i}.png") for i in range(6)]
    assert build(index, refs, client, workers=2, log=lambda *_: None) == (5, 1)
    # Second run: nothing left to do (failures are not retried automatically).
    assert build(index, refs, client, workers=2, log=lambda *_: None) == (0, 0)
    assert index.count("tcgdex") == 5


def test_remove_prefixes_purges_digital_cards(tmp_path):
    index = VisualIndex(tmp_path / "idx.db")
    pocket = CardCandidate(source="tcgdex", external_id="en:A1-036", name="Charizard")
    index.add_many([(ref(1), fingerprint(card_picture(1))), (pocket, fingerprint(card_picture(2)))])
    assert index.remove_prefixes("tcgdex", ["en:A1-", "en:A1a-"]) == 1
    assert [m.candidate.external_id for m in index.search("tcgdex", fingerprint(card_picture(2)))] == ["en:set-1"]


@pytest.fixture
def api_with_index(tmp_path):
    index = VisualIndex(tmp_path / "card_index.db")
    index.add_many([(ref(i), fingerprint(card_picture(i))) for i in range(30)])
    app = create_app(Settings(simulation=True, data_dir=tmp_path), visual_index=index)
    with TestClient(app) as c:
        yield c, app.state.hardware.camera.table


def _type_id(client, code):
    return next(t["id"] for t in client.get("/api/card-types").json() if t["code"] == code)


def test_identify_image_endpoint(api_with_index):
    client, table = api_with_index
    table.place(SimCardSpec(front_image=card_picture(12), rotation_deg=-2.5))
    r = client.post("/api/identify/image", json={"card_type_id": _type_id(client, "pokemon")}).json()
    assert r["available"]
    best = r["candidates"][0]
    assert best["external_id"] == "en:set-12" and best["confidence"] == "high"
    assert "reconnue" in r["message"]


def test_identify_image_warns_on_same_artwork_reprints(tmp_path):
    index = VisualIndex(tmp_path / "card_index.db")
    reprint = CardCandidate(source="tcgdex", external_id="en:other-12", name="Card 12", set_name="Reprint Set",
                            number="40", image_url="https://img.test/r.png")
    index.add_many([(ref(i), fingerprint(card_picture(i))) for i in range(10)])
    index.add_many([(reprint, fingerprint(card_picture(12))), (ref(12), fingerprint(card_picture(12)))])
    app = create_app(Settings(simulation=True, data_dir=tmp_path), visual_index=index)
    with TestClient(app) as client:
        app.state.hardware.camera.table.place(SimCardSpec(front_image=card_picture(12)))
        r = client.post("/api/identify/image", json={"card_type_id": _type_id(client, "pokemon")}).json()
    assert {c["external_id"] for c in r["candidates"][:2]} == {"en:set-12", "en:other-12"}
    assert "Plusieurs éditions" in r["message"]


def test_identify_image_without_card_or_index(api_with_index):
    client, table = api_with_index
    table.remove()
    r = client.post("/api/identify/image", json={"card_type_id": _type_id(client, "pokemon")}).json()
    assert r["candidates"] == [] and "Aucune carte" in r["message"]

    r = client.post("/api/identify/image", json={"card_type_id": _type_id(client, "magic")}).json()
    assert not r["available"] and "build_index scryfall" in r["message"]

    r = client.post("/api/identify/image", json={"card_type_id": _type_id(client, "sport_hockey")}).json()
    assert not r["available"]
