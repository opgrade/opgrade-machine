from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from helpers import create_client, create_order, login
from opgrade.api import create_app
from opgrade.config import Settings
from opgrade.data import Client, ConsentRecord, Order, make_engine


@pytest.fixture
def ctx(tmp_path):
    settings = Settings(simulation=True, data_dir=tmp_path)
    with TestClient(create_app(settings)) as api:
        login(api)
        db = sessionmaker(bind=make_engine(settings), expire_on_commit=False)
        yield api, db


def _pokemon(api):
    return next(t["id"] for t in api.get("/api/card-types").json() if t["code"] == "pokemon")


def add_card(api, client_id, order_id, name="Pikachu"):
    r = api.post("/api/cards", json={"client_id": client_id, "order_id": order_id, "card_type_id": _pokemon(api), "name": name})
    return r


def grade(api, card_id):
    api.post("/api/sim/place", json={})
    sid = api.post("/api/sessions", json={"card_id": card_id}).json()["id"]
    assert api.post(f"/api/sessions/{sid}/capture").json()["accepted"]
    api.post("/api/sim/flip")
    assert api.post(f"/api/sessions/{sid}/capture").json()["accepted"]
    api.post(f"/api/sessions/{sid}/analyze")
    r = api.post(f"/api/sessions/{sid}/review", json={"corners": 9, "edges": 9, "surface": 9})
    assert r.json()["state"] == "completed"
    return sid


# -- Loi 25: collection and consent -----------------------------------------------------------


def test_notice_is_versioned_and_bilingual(ctx):
    api, _ = ctx
    fr = api.get("/api/privacy/notice").json()
    en = api.get("/api/privacy/notice", params={"lang": "en"}).json()
    assert fr["version"] == en["version"]
    assert "anonymisés" in fr["notice"] and "anonymized" in en["notice"]
    assert "{" not in fr["notice"]  # placeholders filled
    assert fr["responsible_configured"] is False  # OpGrade must fill in the privacy officer


@pytest.mark.parametrize(
    "fields,status",
    [
        ({"email": None, "phone": None}, 422),  # one contact required
        ({"consent_service": False}, 422),  # consent required
        ({"consent_policy_version": "old"}, 409),  # notice shown was not the current one
    ],
)
def test_client_creation_rules(ctx, fields, status):
    api, _ = ctx
    version = api.get("/api/privacy/notice").json()["version"]
    body = {"first_name": "A", "last_name": "B", "email": "a@b.test", "consent_service": True,
            "consent_policy_version": version, **fields}
    assert api.post("/api/clients", json=body).status_code == status


def test_consent_is_recorded_and_marketing_is_opt_in(ctx):
    api, db = ctx
    c = create_client(api, phone="514-555-0100", email=None)
    assert c["consent_valid"] and c["consent_marketing"] is False
    with db() as s:
        records = s.query(ConsentRecord).filter_by(client_id=c["id"]).all()
    assert {(r.kind, r.granted) for r in records} == {("service", True), ("marketing", False)}

    version = c["consent_policy_version"]
    r = api.post(f"/api/clients/{c['id']}/consent", json={"kind": "marketing", "granted": True, "policy_version": version})
    assert r.json()["consent_marketing"] is True
    # Withdrawing service consent = deletion request: refused here, the operator must anonymize.
    r = api.post(f"/api/clients/{c['id']}/consent", json={"kind": "service", "granted": False, "policy_version": version})
    assert r.status_code == 409 and "Anonymiser" in r.json()["detail"]


def test_new_notice_version_requires_new_consent(ctx):
    api, db = ctx
    c = create_client(api)
    with db() as s:
        s.get(Client, c["id"]).consent_policy_version = "2020-01-1"
        s.commit()
    assert api.get(f"/api/clients/{c['id']}").json()["consent_valid"] is False
    r = api.post("/api/orders", json={"client_id": c["id"], "channel": "counter"})
    assert r.status_code == 409 and "Consentement requis" in r.json()["detail"]


def test_rectification(ctx):
    api, _ = ctx
    c = create_client(api)
    r = api.patch(f"/api/clients/{c['id']}", json={"last_name": "Gagnon", "phone": "418-555-0199"})
    assert r.json()["last_name"] == "Gagnon" and r.json()["phone"] == "418-555-0199"
    assert api.patch(f"/api/clients/{c['id']}", json={"email": None, "phone": None}).status_code == 422


# -- orders ----------------------------------------------------------------------------------


def test_order_lifecycle(ctx):
    api, _ = ctx
    c = create_client(api)
    order = create_order(api, c["id"], "counter", return_address="ignoré au comptoir")
    assert order["number"].startswith("OPG-") and order["status"] == "received"
    assert order["return_address"] is None  # minimal collection: only for mail-in

    card1 = add_card(api, c["id"], order["id"]).json()
    card2 = add_card(api, c["id"], order["id"], "Dracaufeu").json()
    assert api.post(f"/api/orders/{order['id']}/return", json={}).status_code == 409  # not graded yet

    grade(api, card1["id"])
    o = api.get(f"/api/orders/{order['id']}").json()
    assert (o["status"], o["graded_count"], o["cards_count"]) == ("in_progress", 1, 2)

    grade(api, card2["id"])
    o = api.get(f"/api/orders/{order['id']}").json()
    assert o["status"] == "graded" and all(card["final_grade"] for card in o["cards"])

    o = api.post(f"/api/orders/{order['id']}/return", json={"tracking_out": "CP123"}).json()
    assert o["status"] == "returned" and o["returned_at"]
    assert add_card(api, c["id"], order["id"]).status_code == 409
    assert api.post("/api/sessions", json={"card_id": card1["id"]}).status_code == 409
    assert order["id"] not in [x["id"] for x in api.get("/api/orders").json()]  # no longer open


def test_mail_order_needs_return_address(ctx):
    api, _ = ctx
    c = create_client(api)
    assert api.post("/api/orders", json={"client_id": c["id"], "channel": "mail"}).status_code == 409
    o = create_order(api, c["id"], "mail", return_address="123 rue Principale, Québec", tracking_in="IN42")
    assert o["return_address"].startswith("123") and o["tracking_in"] == "IN42"


def test_card_cannot_join_another_clients_order(ctx):
    api, _ = ctx
    a, b = create_client(api), create_client(api, first_name="Luc")
    order = create_order(api, a["id"])
    assert add_card(api, b["id"], order["id"]).status_code == 409


def test_new_card_reopens_graded_order(ctx):
    api, _ = ctx
    c = create_client(api)
    order = create_order(api, c["id"])
    grade(api, add_card(api, c["id"], order["id"]).json()["id"])
    assert api.get(f"/api/orders/{order['id']}").json()["status"] == "graded"
    add_card(api, c["id"], order["id"], "Mew")
    assert api.get(f"/api/orders/{order['id']}").json()["status"] == "in_progress"


# -- Loi 25: rights and retention -----------------------------------------------------------


def test_access_request_export(ctx):
    api, db = ctx
    c = create_client(api)
    order = create_order(api, c["id"], "mail", return_address="1 rue A")
    add_card(api, c["id"], order["id"])
    r = api.get(f"/api/clients/{c['id']}/export")
    assert "attachment" in r.headers["content-disposition"]
    data = r.json()
    assert data["client"]["email"] == "marie@example.test"
    assert data["orders"][0]["return_address"] == "1 rue A"
    assert data["cards"][0]["name"] == "Pikachu"
    with db() as s:
        assert s.query(ConsentRecord).filter_by(client_id=c["id"], kind="access").count() == 1


def test_anonymization_keeps_grades_but_removes_personal_data(ctx):
    api, db = ctx
    c = create_client(api, notes="Préfère être appelée le soir")
    order = create_order(api, c["id"], "mail", return_address="1 rue A", tracking_in="IN1")
    card = add_card(api, c["id"], order["id"]).json()
    sid = grade(api, card["id"])

    body = {"reason": "Demande du client", "confirm": True}
    r = api.post(f"/api/clients/{c['id']}/anonymize", json=body)
    assert r.status_code == 409  # open order
    api.post(f"/api/orders/{order['id']}/return", json={})

    out = api.post(f"/api/clients/{c['id']}/anonymize", json=body).json()
    assert out["anonymized_at"] and out["email"] is None and out["notes"] is None
    assert "Marie" not in out["first_name"] + out["last_name"]
    with db() as s:
        o = s.get(Order, order["id"])
        assert o.return_address is None and o.tracking_in is None
        assert s.query(ConsentRecord).filter_by(client_id=c["id"], kind="anonymization").count() == 1
    assert api.get(f"/api/sessions/{sid}").json()["final_grade"]  # certificate data kept
    assert c["id"] not in [x["id"] for x in api.get("/api/clients").json()]
    assert api.post("/api/orders", json={"client_id": c["id"], "channel": "counter"}).status_code == 409


def test_retention_anonymizes_inactive_clients_only(ctx):
    api, db = ctx
    old, recent, busy = create_client(api), create_client(api, first_name="Recent"), create_client(api, first_name="Busy")
    create_order(api, busy["id"])  # open order: never anonymized automatically
    long_ago = datetime.now(timezone.utc) - timedelta(days=365 * 4)
    with db() as s:
        for cid in (old["id"], busy["id"]):
            client = s.get(Client, cid)
            client.created_at = client.consent_service_at = long_ago
            for o in client.orders:
                o.created_at = long_ago
        s.commit()
    assert [c["id"] for c in api.get("/api/privacy/retention").json()] == [old["id"]]
    assert api.post("/api/privacy/retention/apply").json() == {"anonymized": 1}
    assert api.get(f"/api/clients/{old['id']}").json()["anonymized_at"]
    assert api.get(f"/api/clients/{recent['id']}").json()["anonymized_at"] is None


def test_dates_read_back_are_utc_with_offset(ctx):
    """SQLite drops time zones: dates must still leave the API as UTC with an offset,
    otherwise the browser shows them hours off."""
    api, db = ctx
    c = create_client(api)
    with db() as s:
        created = s.get(Client, c["id"]).created_at
    assert created.tzinfo is not None
    created_json = api.get(f"/api/clients/{c['id']}").json()["created_at"]
    assert created_json.endswith(("Z", "+00:00"))
