"""Shared test helpers (pytest puts tests/ on sys.path)."""

from fastapi.testclient import TestClient


def create_client(api: TestClient, **fields) -> dict:
    """A client with the mandatory Loi 25 consent for the current notice."""
    version = api.get("/api/privacy/notice").json()["version"]
    body = {
        "first_name": "Marie", "last_name": "Tremblay", "email": "marie@example.test",
        "consent_service": True, "consent_policy_version": version, **fields,
    }
    response = api.post("/api/clients", json=body)
    assert response.status_code == 201, response.text
    return response.json()


def create_order(api: TestClient, client_id: int, channel: str = "counter", **fields) -> dict:
    response = api.post("/api/orders", json={"client_id": client_id, "channel": channel, **fields})
    assert response.status_code == 201, response.text
    return response.json()
