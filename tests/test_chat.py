"""The chat endpoint: non-stream, stream, and the failure paths."""

from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from app.main import create_app
from tests.conftest import mock_settings

MESSAGES = [{"role": "user", "content": "你好 aigate"}]


def _payload(**overrides) -> dict:
    body = {"model": "default", "messages": MESSAGES}
    body.update(overrides)
    return body


def test_non_stream_completion(client: TestClient) -> None:
    response = client.post("/v1/chat/completions", json=_payload())
    assert response.status_code == 200

    body = response.json()
    assert body["object"] == "chat.completion"
    assert body["choices"][0]["message"]["content"].startswith("[mock:mock-echo]")
    assert body["choices"][0]["finish_reason"] == "stop"
    assert body["usage"]["total_tokens"] > 0
    assert response.headers["x-aigate-provider"] == "mock"
    assert response.headers["x-aigate-route"] == "default"


def test_extra_fields_are_accepted(client: TestClient) -> None:
    response = client.post(
        "/v1/chat/completions",
        json=_payload(temperature=0.2, response_format={"type": "json_object"}),
    )
    assert response.status_code == 200


def test_stream_completion(client: TestClient) -> None:
    with client.stream(
        "POST", "/v1/chat/completions", json=_payload(model="mock", stream=True)
    ) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        raw = "".join(response.iter_text())

    assert raw.rstrip().endswith("data: [DONE]")

    chunks = [
        json.loads(line[len("data: ") :]) for line in raw.splitlines() if line.startswith("data: {")
    ]
    assert chunks[0]["choices"][0]["delta"]["role"] == "assistant"

    text = "".join(
        chunk["choices"][0]["delta"].get("content") or "" for chunk in chunks if chunk["choices"]
    )
    assert text.startswith("[mock:mock-echo]")
    assert chunks[-1]["choices"][0]["finish_reason"] == "stop"


def test_stream_can_return_usage(client: TestClient) -> None:
    with client.stream(
        "POST",
        "/v1/chat/completions",
        json=_payload(stream=True, stream_options={"include_usage": True}),
    ) as response:
        raw = "".join(response.iter_text())

    chunks = [
        json.loads(line[len("data: ") :]) for line in raw.splitlines() if line.startswith("data: {")
    ]
    usage_chunks = [chunk for chunk in chunks if chunk.get("usage")]
    assert usage_chunks, "expected a final usage chunk"
    assert usage_chunks[-1]["usage"]["total_tokens"] > 0


def test_unknown_model_falls_back_to_default_route(client: TestClient) -> None:
    # Agent clients hardcode their own model ids; the gateway must still serve
    # them instead of 404ing on names it has never seen.
    response = client.post("/v1/chat/completions", json=_payload(model="gpt-4o-mini"))
    assert response.status_code == 200
    assert response.headers["x-aigate-route"] == "gpt-4o-mini"
    assert response.json()["choices"][0]["message"]["content"].startswith("[mock:mock-echo]")


def test_unknown_model_is_404_in_strict_mode(tmp_path: Path) -> None:
    settings = mock_settings(tmp_path, unknown_model="error")
    with TestClient(create_app(settings)) as strict_client:
        response = strict_client.post("/v1/chat/completions", json=_payload(model="gpt-9"))
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "model_not_found"


def test_invalid_body_is_400_with_envelope(client: TestClient) -> None:
    response = client.post("/v1/chat/completions", json={"messages": MESSAGES})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request"
