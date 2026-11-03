"""Cache behaviour: what is allowed to hit, what must not, and what a hit costs."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from app.main import create_app
from tests.conftest import mock_settings


def _payload(prompt: str = "hello there", **overrides: object) -> dict[str, object]:
    body: dict[str, object] = {
        "model": "mock",
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0,
    }
    body.update(overrides)
    return body


def _app_client(tmp_path: Path, **settings: object) -> TestClient:
    return TestClient(create_app(mock_settings(tmp_path, **settings)))


def test_exact_cache_hit_returns_same_body(client: TestClient) -> None:
    first = client.post("/v1/chat/completions", json=_payload())
    assert first.status_code == 200
    assert first.headers["x-aigate-cache"] == "miss"

    second = client.post("/v1/chat/completions", json=_payload())
    assert second.status_code == 200
    assert second.headers["x-aigate-cache"] == "exact"
    assert (
        second.json()["choices"][0]["message"]["content"]
        == (first.json()["choices"][0]["message"]["content"])
    )
    assert second.json()["usage"]["total_tokens"] == first.json()["usage"]["total_tokens"]


def test_bypass_header_forces_upstream(client: TestClient) -> None:
    client.post("/v1/chat/completions", json=_payload())
    response = client.post(
        "/v1/chat/completions",
        json=_payload(),
        headers={"x-aigate-cache": "off"},
    )
    assert response.status_code == 200
    assert response.headers["x-aigate-cache"] == "off"


def test_streaming_is_never_served_from_cache(client: TestClient) -> None:
    client.post("/v1/chat/completions", json=_payload())
    response = client.post("/v1/chat/completions", json=_payload(stream=True))
    assert response.status_code == 200
    assert response.headers["x-aigate-cache"] == "bypass"


def test_creative_temperature_is_not_cached(client: TestClient) -> None:
    client.post("/v1/chat/completions", json=_payload(temperature=0.9))
    second = client.post("/v1/chat/completions", json=_payload(temperature=0.9))
    assert second.headers["x-aigate-cache"] == "miss"


def test_requests_with_tools_are_not_cached(client: TestClient) -> None:
    body = _payload()
    body["tools"] = [
        {
            "type": "function",
            "function": {"name": "lookup", "parameters": {"type": "object"}},
        }
    ]
    client.post("/v1/chat/completions", json=body)
    second = client.post("/v1/chat/completions", json=body)
    assert second.headers["x-aigate-cache"] == "miss"


def test_different_max_tokens_is_a_different_key(client: TestClient) -> None:
    client.post("/v1/chat/completions", json=_payload(max_tokens=5))
    second = client.post("/v1/chat/completions", json=_payload(max_tokens=10))
    assert second.headers["x-aigate-cache"] == "miss"


def test_semantic_hit_on_paraphrase(tmp_path: Path) -> None:
    # Measured with the hashing embedder: this pair scores 0.926.
    with _app_client(tmp_path, semantic_threshold=0.9) as client:
        client.post("/v1/chat/completions", json=_payload("what is the capital of france"))
        response = client.post(
            "/v1/chat/completions",
            json=_payload("what is the capital city of france?"),
        )
        assert response.status_code == 200
        assert response.headers["x-aigate-cache"] == "semantic"
        assert float(response.headers["x-aigate-cache-similarity"]) >= 0.9


def test_default_threshold_rejects_entity_swap(tmp_path: Path) -> None:
    # "capital of france" vs "capital of germany" scores 0.833 with this
    # embedder; the 0.92 default must keep them apart.
    with _app_client(tmp_path) as client:
        client.post("/v1/chat/completions", json=_payload("what is the capital of france"))
        response = client.post(
            "/v1/chat/completions", json=_payload("what is the capital of germany")
        )
        assert response.headers["x-aigate-cache"] == "miss"


def test_semantic_cache_only_for_single_turn(tmp_path: Path) -> None:
    with _app_client(tmp_path, semantic_threshold=0.9) as client:
        client.post("/v1/chat/completions", json=_payload("what is the capital of france"))
        body = _payload("what is the capital city of france?")
        body["messages"] = [
            {"role": "user", "content": "what is the capital of france"},
            {"role": "assistant", "content": "Paris."},
            {"role": "user", "content": "what is the capital city of france?"},
        ]
        response = client.post("/v1/chat/completions", json=body)
        assert response.headers["x-aigate-cache"] == "miss"


def test_tenants_do_not_share_cache(tmp_path: Path) -> None:
    with _app_client(tmp_path, api_keys=("sk-alpha", "sk-beta")) as client:
        alpha = {"authorization": "Bearer sk-alpha"}
        beta = {"authorization": "Bearer sk-beta"}
        client.post("/v1/chat/completions", json=_payload(), headers=alpha)
        hit = client.post("/v1/chat/completions", json=_payload(), headers=alpha)
        assert hit.headers["x-aigate-cache"] == "exact"

        other = client.post("/v1/chat/completions", json=_payload(), headers=beta)
        assert other.headers["x-aigate-cache"] == "miss"


def test_quota_is_checked_before_cache(client: TestClient, admin_headers: dict[str, str]) -> None:
    client.put(
        "/admin/quotas/dev", json={"tokens_limit": 1}, headers=admin_headers
    ).raise_for_status()
    first = client.post("/v1/chat/completions", json=_payload())
    assert first.status_code == 200
    # The second request is cacheable, but quota must win: a cache is not a
    # way around the counter.
    second = client.post("/v1/chat/completions", json=_payload())
    assert second.status_code == 429
    assert second.json()["error"]["code"] == "quota_exceeded"


def test_hit_is_recorded_with_saved_cost(client: TestClient, admin_headers: dict[str, str]) -> None:
    client.put(
        "/admin/prices",
        json={
            "provider": "mock",
            "model": "mock-echo",
            "input_usd_per_1m": 1.0,
            "output_usd_per_1m": 2.0,
        },
        headers=admin_headers,
    ).raise_for_status()

    client.post("/v1/chat/completions", json=_payload())
    client.post("/v1/chat/completions", json=_payload())

    rows = client.get("/admin/requests", headers=admin_headers).json()["data"]
    hits = [row for row in rows if row["cache_hit"]]
    assert len(hits) == 1
    assert hits[0]["cost_usd"] == 0.0
    assert hits[0]["saved_usd"] > 0.0

    usage = client.get("/admin/usage?group_by=model", headers=admin_headers).json()
    assert usage["totals"]["cache_hits"] == 1
    assert usage["totals"]["saved_usd"] > 0.0
    assert usage["totals"]["cache_hit_rate"] == 0.5


def test_stats_and_purge(client: TestClient, admin_headers: dict[str, str]) -> None:
    client.post("/v1/chat/completions", json=_payload())
    client.post("/v1/chat/completions", json=_payload())

    stats = client.get("/admin/cache/stats", headers=admin_headers).json()
    assert stats["enabled"] is True
    assert stats["backend"] == "memory"
    assert stats["hits_exact"] == 1
    assert stats["entries"] >= 1

    purged = client.post("/admin/cache/purge", headers=admin_headers).json()
    assert purged["removed"] >= 1
    after = client.post("/v1/chat/completions", json=_payload())
    assert after.headers["x-aigate-cache"] == "miss"
