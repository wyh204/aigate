"""Key handling: off by default in dev, enforced when AIGATE_API_KEYS is set."""

from __future__ import annotations

from fastapi.testclient import TestClient

MESSAGES = [{"role": "user", "content": "hi"}]


def _call(client: TestClient, headers: dict[str, str] | None = None):
    return client.post(
        "/v1/chat/completions",
        json={"model": "default", "messages": MESSAGES},
        headers=headers or {},
    )


def test_missing_key_is_rejected(auth_client: TestClient) -> None:
    response = _call(auth_client)
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_api_key"


def test_wrong_key_is_rejected(auth_client: TestClient) -> None:
    response = _call(auth_client, {"Authorization": "Bearer nope"})
    assert response.status_code == 401


def test_bearer_key_is_accepted(auth_client: TestClient) -> None:
    response = _call(auth_client, {"Authorization": "Bearer sk-test-123"})
    assert response.status_code == 200


def test_x_api_key_header_is_accepted(auth_client: TestClient) -> None:
    response = _call(auth_client, {"x-api-key": "sk-test-123"})
    assert response.status_code == 200


def test_healthz_stays_public(auth_client: TestClient) -> None:
    assert auth_client.get("/healthz").status_code == 200
