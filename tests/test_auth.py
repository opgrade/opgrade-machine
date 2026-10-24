from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from helpers import TEST_PASSWORD, create_client, create_order, login
from opgrade.api import create_app
from opgrade.auth import hash_password, is_local, verify_password
from opgrade.auth import __main__ as auth_cli
from opgrade.config import Settings
from opgrade.data import AuditEvent, AuthSession, ConsentRecord, User


@pytest.fixture
def app(tmp_path):
    return create_app(Settings(simulation=True, data_dir=tmp_path))


@pytest.fixture
def api(app):
    with TestClient(app) as c:
        yield c


def events(app, action):
    with app.state.session_factory() as db:
        return db.query(AuditEvent).filter_by(action=action).all()


# -- passwords and first setup ---------------------------------------------------------------


def test_password_hash_is_salted_scrypt():
    h1, h2 = hash_password("secret-1234"), hash_password("secret-1234")
    assert h1.startswith("scrypt$") and h1 != h2
    assert verify_password("secret-1234", h1) and not verify_password("secret-1235", h1)
    assert not verify_password("x", "garbage")


@pytest.mark.parametrize("host,local", [("127.0.0.1", True), ("::1", True), ("localhost", True),
                                         ("192.168.1.20", False), ("testclient", False), (None, False)])
def test_is_local(host, local):
    assert is_local(host) is local


def test_api_requires_login_except_public_routes(api):
    assert api.get("/api/status").status_code == 200
    assert api.get("/api/auth/state").json() == {"setup_required": True, "user": None}
    for path in ("/api/clients", "/api/orders", "/api/sessions", "/api/privacy/notice", "/api/users"):
        assert api.get(path).status_code == 401, path


def test_first_admin_only_from_the_machine(app):
    body = {"username": "patron", "password": "un-long-mot-de-passe"}
    with TestClient(app) as remote:  # TestClient's client host is "testclient": not local
        assert remote.post("/api/auth/setup", json=body).status_code == 403
    with TestClient(app, client=("127.0.0.1", 50000)) as kiosk:
        r = kiosk.post("/api/auth/setup", json=body)
        assert r.status_code == 200 and r.json()["role"] == "admin"
        assert kiosk.get("/api/clients").status_code == 200  # logged in by the setup
        assert kiosk.post("/api/auth/setup", json=body).status_code == 409  # only once


# -- login -----------------------------------------------------------------------------------


def test_login_logout_and_lockout(app, api):
    login(api, "operator", "marc")
    api.post("/api/auth/logout")
    assert api.get("/api/clients").status_code == 401

    for _ in range(5):
        r = api.post("/api/auth/login", json={"username": "marc", "password": "mauvais"})
        assert r.status_code == 401 and "incorrect" in r.json()["detail"]
    r = api.post("/api/auth/login", json={"username": "marc", "password": TEST_PASSWORD})
    assert r.status_code == 401 and "verrouillé" in r.json()["detail"]
    assert len(events(app, "login_failed")) == 6

    # Unknown user: same message (no username probing).
    r = api.post("/api/auth/login", json={"username": "personne", "password": "x"})
    assert r.json()["detail"] == "Identifiant ou mot de passe incorrect."


def test_idle_session_expires(app, api):
    login(api, "operator", "marc")
    with app.state.session_factory() as db:
        for s in db.query(AuthSession):
            s.last_seen_at = datetime.now(timezone.utc) - timedelta(minutes=31)
        db.commit()
    assert api.get("/api/clients").status_code == 401


def test_cross_origin_writes_refused(api):
    login(api)
    r = api.post("/api/auth/logout", headers={"Origin": "https://evil.example"})
    assert r.status_code == 403
    assert api.post("/api/auth/logout", headers={"Origin": "http://testserver"}).status_code == 200


def test_session_cookie_flags(api):
    with api.app.state.session_factory() as db:
        from opgrade.auth import create_user
        create_user(db, "marc", TEST_PASSWORD, "operator")
        db.commit()
    r = api.post("/api/auth/login", json={"username": "marc", "password": TEST_PASSWORD})
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie


# -- roles -----------------------------------------------------------------------------------


def test_operator_works_but_cannot_touch_sensitive_actions(app, api):
    login(api, "operator", "marc")
    c = create_client(api)
    create_order(api, c["id"])
    with app.state.session_factory() as db:
        assert {r.recorded_by for r in db.query(ConsentRecord)} == {"marc"}

    forbidden = [
        ("get", f"/api/clients/{c['id']}/export", None),
        ("post", f"/api/clients/{c['id']}/anonymize", {"reason": "test", "confirm": True}),
        ("get", "/api/privacy/retention", None),
        ("post", "/api/privacy/retention/apply", None),
        ("post", "/api/card-types", {"code": "x", "name": "X", "width_mm": 60, "height_mm": 80}),
        ("get", "/api/users", None),
        ("post", "/api/users", {"username": "eve", "password": "x" * 12, "role": "admin"}),
        ("get", "/api/audit", None),
    ]
    for method, path, body in forbidden:
        r = getattr(api, method)(path, json=body) if body else getattr(api, method)(path)
        assert r.status_code == 403, path
        assert r.json()["detail"] == "Réservé à l'administrateur."


def test_admin_actions_are_journaled(app, api):
    login(api, "admin", "patron")
    c = create_client(api)
    assert api.get(f"/api/clients/{c['id']}/export").status_code == 200
    assert api.post(f"/api/clients/{c['id']}/anonymize", json={"reason": "Demande", "confirm": True}).status_code == 200
    r = api.post("/api/card-types", json={"code": "mini", "name": "Mini", "width_mm": 44, "height_mm": 63})
    assert r.status_code == 201
    actions = {e["action"] for e in api.get("/api/audit").json()}
    assert {"login", "client_export", "client_anonymized", "card_type_created"} <= actions
    assert all("@" not in str(e["details"]) for e in api.get("/api/audit").json())  # no client e-mail


def test_grading_operator_is_the_logged_in_user(api):
    login(api, "operator", "marc")
    c = create_client(api)
    o = create_order(api, c["id"])
    ptype = next(t["id"] for t in api.get("/api/card-types").json() if t["code"] == "pokemon")
    card = api.post("/api/cards", json={"client_id": c["id"], "order_id": o["id"], "card_type_id": ptype, "name": "Mew"}).json()
    s = api.post("/api/sessions", json={"card_id": card["id"], "operator": "quelqu'un d'autre"}).json()
    assert s["operator"] == "marc"


# -- account management ----------------------------------------------------------------------


def test_user_management(app, api):
    login(api, "admin", "patron")
    # Password policy depends on the role.
    assert api.post("/api/users", json={"username": "opr", "password": "12345", "role": "operator"}).status_code == 400
    assert api.post("/api/users", json={"username": "adm", "password": "123456789", "role": "admin"}).status_code == 400
    op = api.post("/api/users", json={"username": "opr", "password": "123456", "role": "operator"}).json()
    assert api.post("/api/users", json={"username": "opr", "password": "123456", "role": "operator"}).status_code == 400

    with TestClient(app) as other:
        assert other.post("/api/auth/login", json={"username": "opr", "password": "123456"}).status_code == 200
        api.patch(f"/api/users/{op['id']}", json={"active": False})
        assert other.get("/api/clients").status_code == 401  # deactivation closes sessions

    me = api.get("/api/auth/me").json()
    r = api.patch(f"/api/users/{me['id']}", json={"role": "operator"})
    assert r.status_code == 400 and "au moins un administrateur" in r.json()["detail"]

    api.patch(f"/api/users/{op['id']}", json={"active": True})
    assert api.post(f"/api/users/{op['id']}/password", json={"new_password": "nouveau1"}).status_code == 200
    with TestClient(app) as other:
        assert other.post("/api/auth/login", json={"username": "opr", "password": "123456"}).status_code == 401
        assert other.post("/api/auth/login", json={"username": "opr", "password": "nouveau1"}).status_code == 200


def test_change_own_password(api):
    login(api, "operator", "marc")
    bad = api.post("/api/auth/password", json={"current_password": "faux", "new_password": "abcdefgh"})
    assert bad.status_code == 401
    ok = api.post("/api/auth/password", json={"current_password": TEST_PASSWORD, "new_password": "abcdefgh"})
    assert ok.status_code == 200
    assert api.get("/api/clients").status_code == 200  # still logged in with the new session
    r = api.post("/api/auth/login", json={"username": "marc", "password": TEST_PASSWORD})
    assert r.status_code == 401


def test_cli_create_admin(tmp_path, monkeypatch):
    monkeypatch.setenv("OPGRADE_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(auth_cli.getpass, "getpass", lambda prompt="": "un-long-mot-de-passe")
    assert auth_cli.main(["create-admin", "--username", "Patron"]) == 0
    assert auth_cli.main(["create-admin", "--username", "patron"]) == 1  # already exists
    app = create_app(Settings(simulation=True, data_dir=tmp_path))
    with app.state.session_factory() as db:
        user = db.query(User).one()
        assert (user.username, user.role) == ("patron", "admin")
