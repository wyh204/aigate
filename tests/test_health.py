"""Ops endpoints: what is configured, and does it answer."""

from __future__ import annotations

from fastapi.testclient import TestClient


def test_healthz_reports_configuration(client: TestClient) -> None:
    response = client.get("/healthz")
    assert response.status_code == 200

    body = response.json()
    assert body["status"] == "ok"
    assert [provider["name"] for provider in body["providers"]] == ["mock"]
    assert "default" in body["routes"]
    assert body["auth_enabled"] is False
    assert response.headers["x-request-id"]


def test_models_lists_aliases(client: TestClient) -> None:
    body = client.get("/v1/models").json()
    assert {model["id"] for model in body["data"]} == {"default", "mock"}


def test_unknown_path_uses_the_same_error_envelope(client: TestClient) -> None:
    response = client.get("/nope")
    assert response.status_code == 404
    assert response.json()["error"]["message"] == "Not Found"


def test_request_id_is_echoed(client: TestClient) -> None:
    response = client.get("/healthz", headers={"x-request-id": "trace-me"})
    assert response.headers["x-request-id"] == "trace-me"
