import io
import re

import pytest
from fastapi.testclient import TestClient
from pypdf import PdfReader

from helpers import create_client, create_order, login
from opgrade.api import create_app
from opgrade.config import Settings
from opgrade.reports import new_certificate_number, normalize_certificate, qr_payload

CERT = re.compile(r"^OPG-[2-9A-HJKMNP-Z]{4}-[2-9A-HJKMNP-Z]{4}$")


def graded_session(api, name="Dracaufeu", **card_fields) -> dict:
    c = create_client(api, first_name="Juliette", email="juliette@example.test")
    o = create_order(api, c["id"])
    ptype = next(t["id"] for t in api.get("/api/card-types").json() if t["code"] == "pokemon")
    card = api.post("/api/cards", json={"client_id": c["id"], "order_id": o["id"], "card_type_id": ptype,
                                        "name": name, "set_name": "Set de Base", "card_number": "4/102",
                                        "year": 1999, **card_fields}).json()
    api.post("/api/sim/place", json={"front_left_share": 58})
    sid = api.post("/api/sessions", json={"card_id": card["id"]}).json()["id"]
    api.post(f"/api/sessions/{sid}/capture")
    api.post("/api/sim/flip")
    api.post(f"/api/sessions/{sid}/capture")
    api.post(f"/api/sessions/{sid}/analyze")
    return api.post(f"/api/sessions/{sid}/review", json={"corners": 9, "edges": 9, "surface": 10}).json()


def pdf_text(content: bytes) -> str:
    return "\n".join(page.extract_text() for page in PdfReader(io.BytesIO(content)).pages)


@pytest.fixture
def api(tmp_path):
    with TestClient(create_app(Settings(simulation=True, data_dir=tmp_path))) as c:
        login(c, "operator", "marc")
        yield c


def test_certificate_numbers():
    numbers = {new_certificate_number() for _ in range(500)}
    assert len(numbers) == 500 and all(CERT.match(n) for n in numbers)
    assert normalize_certificate("opg7kq2m9xd") == "OPG-7KQ2-M9XD"
    assert normalize_certificate(" OPG-7KQ2-M9XD ") == "OPG-7KQ2-M9XD"
    assert qr_payload("OPG-7KQ2-M9XD", "https://verifier.opgrade.ca/") == "https://verifier.opgrade.ca/verify/OPG-7KQ2-M9XD"
    assert qr_payload("OPG-7KQ2-M9XD", None) == "OPGRADE CERT OPG-7KQ2-M9XD"


def test_completed_grading_gets_a_certificate(api):
    s = graded_session(api)
    assert CERT.match(s["certificate_number"])
    other = graded_session(api, "Mew")
    assert other["certificate_number"] != s["certificate_number"]


def test_report_pdf_content(api):
    s = graded_session(api)
    r = api.get(f"/api/sessions/{s['id']}/report.pdf")
    assert r.status_code == 200 and r.headers["content-type"] == "application/pdf"
    assert r.content.startswith(b"%PDF")
    reader = PdfReader(io.BytesIO(r.content))
    assert len(reader.pages) == 1
    text = pdf_text(r.content)
    for expected in (s["certificate_number"], "Dracaufeu", "Set de Base", "4/102", "NOTE FINALE",
                     "Centrage", "Coins", "Bordures", "Surface", "marc", s["rules_version"],
                     "SIMULATION", "ne constitue pas une"):
        assert expected in text, expected
    assert str(s["final_grade"]) in text


def test_report_has_no_client_personal_data(api):
    s = graded_session(api)
    text = pdf_text(api.get(f"/api/sessions/{s['id']}/report.pdf").content)
    assert "Juliette" not in text and "juliette@example.test" not in text and "Tremblay" not in text


def test_report_only_for_completed_grading_and_logged_in_users(api):
    s = graded_session(api)
    c = create_client(api)
    o = create_order(api, c["id"])
    ptype = next(t["id"] for t in api.get("/api/card-types").json() if t["code"] == "pokemon")
    card = api.post("/api/cards", json={"client_id": c["id"], "order_id": o["id"], "card_type_id": ptype, "name": "X"}).json()
    pending = api.post("/api/sessions", json={"card_id": card["id"]}).json()
    assert api.get(f"/api/sessions/{pending['id']}/report.pdf").status_code == 409
    api.post("/api/auth/logout")
    assert api.get(f"/api/sessions/{s['id']}/report.pdf").status_code == 401


def test_non_latin_card_name_does_not_break_the_report(api):
    s = graded_session(api, "Charizard δ ex")
    assert api.get(f"/api/sessions/{s['id']}/report.pdf").status_code == 200


def test_public_verification_is_off_by_default(api):
    s = graded_session(api)
    api.post("/api/auth/logout")
    assert api.get(f"/verify/{s['certificate_number']}").status_code == 404


def test_public_verification_page(tmp_path):
    settings = Settings(simulation=True, data_dir=tmp_path, public_verification=True,
                        verify_url="https://verifier.opgrade.ca")
    with TestClient(create_app(settings)) as api:
        login(api)
        s = graded_session(api)
        api.post("/api/auth/logout")  # the page is public
        cert = s["certificate_number"]
        r = api.get(f"/verify/{cert.lower().replace('-', '')}")  # as typed by a buyer
        assert r.status_code == 200
        assert cert in r.text and "Dracaufeu" in r.text and "SIMULÉE" in r.text
        assert "Juliette" not in r.text and "juliette@example.test" not in r.text
        assert api.get("/verify/OPG-2222-2222").status_code == 404


def test_qr_code_decodes_to_the_verification_url():
    """Same encoder as the PDF; rasterised and read back with OpenCV's QR detector."""
    import cv2
    import numpy as np
    from reportlab.graphics.barcode.qr import QrCodeWidget

    payload = qr_payload("OPG-VK5D-ZNWR", "https://verifier.opgrade.ca")
    widget = QrCodeWidget(payload, barLevel="M")
    widget.draw()
    n = widget.qr.getModuleCount()
    modules = np.array([[widget.qr.isDark(r, c) for c in range(n)] for r in range(n)])
    image = np.full((n + 8, n + 8), 255, np.uint8)  # 4-module quiet zone
    image[4:-4, 4:-4][modules] = 0
    image = cv2.resize(image, None, fx=10, fy=10, interpolation=cv2.INTER_NEAREST)
    decoded, _, _ = cv2.QRCodeDetector().detectAndDecode(image)
    assert decoded == "https://verifier.opgrade.ca/verify/OPG-VK5D-ZNWR"
