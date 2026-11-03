"""M3: metering, quotas, keys and the cost aggregates built from `requests`."""

from __future__ import annotations

from fastapi.testclient import TestClient


def _payload(**overrides) -> dict:
    payload = {
        "model": "mock",
        "messages": [{"role": "user", "content": "hello metering world"}],
    }
    payload.update(overrides)
    return payload


def _admin(client: TestClient) -> dict[str, str]:
    from tests.conftest import ADMIN_TOKEN

    return {"x-admin-token": ADMIN_TOKEN}


def test_usage_endpoint_reports_persisted_call(client: TestClient) -> None:
    headers = _admin(client)
    price = client.put(
        "/admin/prices",
        json={
            "provider": "mock",
            "model": "mock-echo",
            "input_usd_per_1m": 1.0,
            "output_usd_per_1m": 2.0,
        },
        headers=headers,
    )
    assert price.status_code == 200

    response = client.post("/v1/chat/completions", json=_payload())
    assert response.status_code == 200
    request_id = response.headers["x-request-id"]

    usage = client.get("/admin/usage", headers=headers)
    assert usage.status_code == 200
    body = usage.json()
    assert body["totals"]["requests"] == 1
    assert body["totals"]["total_tokens"] > 0
    # 1 USD/1M input + 2 USD/1M output on a handful of tokens: non-zero, tiny.
    assert 0 < body["totals"]["cost_usd"] < 0.001
    assert body["group_by"] == "day"
    assert body["data"][0]["requests"] == 1

    recent = client.get("/admin/requests", headers=headers).json()
    assert recent["data"][0]["request_id"] == request_id
    assert recent["data"][0]["provider"] == "mock"


def test_usage_group_by_provider(client: TestClient) -> None:
    client.post("/v1/chat/completions", json=_payload())
    body = client.get("/admin/usage?group_by=provider", headers=_admin(client)).json()
    assert [row["bucket"] for row in body["data"]] == ["mock"]


def test_admin_is_closed_without_token(tmp_path) -> None:
    from fastapi.testclient import TestClient as Client

    from app.main import create_app
    from tests.conftest import mock_settings

    with Client(create_app(mock_settings(tmp_path, admin_token=""))) as closed:
        response = closed.get("/admin/usage")
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "admin_disabled"


def test_admin_rejects_wrong_token(client: TestClient) -> None:
    response = client.get("/admin/usage", headers={"x-admin-token": "nope"})
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_admin_token"


def test_key_lifecycle(client: TestClient) -> None:
    headers = _admin(client)
    created = client.post("/admin/keys", json={"tenant": "dev", "name": "ci"}, headers=headers)
    assert created.status_code == 201
    body = created.json()
    assert body["key"].startswith("sk-aigate-")

    ok = client.post(
        "/v1/chat/completions",
        json=_payload(),
        headers={"Authorization": f"Bearer {body['key']}"},
    )
    assert ok.status_code == 200

    assert client.post("/v1/chat/completions", json=_payload()).status_code == 401
    assert (
        client.post(
            "/v1/chat/completions",
            json=_payload(),
            headers={"Authorization": "Bearer sk-aigate-wrong"},
        ).status_code
        == 401
    )

    revoked = client.delete(f"/admin/keys/{body['id']}", headers=headers)
    assert revoked.status_code == 200
    assert (
        client.post(
            "/v1/chat/completions",
            json=_payload(),
            headers={"Authorization": f"Bearer {body['key']}"},
        ).status_code
        == 401
    )


def test_quota_returns_429_once_exhausted(client: TestClient) -> None:
    headers = _admin(client)
    quota = client.put("/admin/quotas/dev", json={"tokens_limit": 1}, headers=headers)
    assert quota.status_code == 200

    assert client.post("/v1/chat/completions", json=_payload()).status_code == 200
    # The writer commits in batches; the admin read flushes so this is not a race.
    client.get("/admin/quotas/dev", headers=headers)

    blocked = client.post("/v1/chat/completions", json=_payload())
    assert blocked.status_code == 429
    assert blocked.json()["error"]["code"] == "quota_exceeded"

    # Streaming must be gated by the same check, not bypass it.
    assert client.post("/v1/chat/completions", json=_payload(stream=True)).status_code == 429

    status = client.get("/admin/quotas/dev", headers=headers).json()
    assert status["tokens_limit"] == 1
    assert status["tokens_used"] > 1


def test_streamed_call_is_persisted_as_stream(client: TestClient) -> None:
    response = client.post("/v1/chat/completions", json=_payload(stream=True))
    assert response.status_code == 200

    recent = client.get("/admin/requests", headers=_admin(client)).json()
    assert recent["data"][0]["stream"] is True
    assert recent["data"][0]["ttft_ms"] is not None
    assert recent["data"][0]["usage_estimated"] is True


def test_tenants_are_listed_with_month_spend(client: TestClient) -> None:
    client.post("/v1/chat/completions", json=_payload())
    body = client.get("/admin/tenants", headers=_admin(client)).json()
    slugs = {row["slug"] for row in body["data"]}
    assert "dev" in slugs
