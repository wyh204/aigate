"""Adapter for any vendor that speaks the OpenAI chat-completions protocol.

OpenAI, DeepSeek, Qwen (DashScope compatible mode), Moonshot, vLLM, Ollama —
all of them work by changing ``base_url``. That is the whole point: one adapter,
many vendors, no per-vendor code until a vendor breaks the protocol
(Anthropic does; that gets its own adapter).
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx

from app.protocol.openai import ChatCompletion, ChatCompletionChunk, ChatCompletionRequest
from app.provider.base import Provider, ProviderError


class OpenAICompatProvider(Provider):
    type = "openai_compat"

    def __init__(
        self,
        *,
        name: str,
        base_url: str,
        api_key: str = "",
        timeout_s: float = 60.0,
    ) -> None:
        self.name = name
        self._url = base_url.rstrip("/") + "/chat/completions"
        headers = {"content-type": "application/json", "user-agent": "aigate/0.1"}
        if api_key:
            headers["authorization"] = f"Bearer {api_key}"
        self._client = httpx.AsyncClient(
            headers=headers,
            timeout=httpx.Timeout(timeout_s, connect=10.0),
        )

    def _payload(
        self, model: str, request: ChatCompletionRequest, *, stream: bool
    ) -> dict[str, Any]:
        payload = request.upstream_payload(model)
        payload["stream"] = stream
        if stream and request.stream_options is not None:
            payload["stream_options"] = {"include_usage": request.stream_options.include_usage}
        return payload

    def _upstream_error(self, status: int, body: str) -> ProviderError:
        retryable = status in (408, 409, 425, 429) or status >= 500
        return ProviderError(
            f"upstream {status}: {body[:400]}",
            provider=self.name,
            status_code=502,
            retryable=retryable,
            upstream_status=status,
        )

    async def chat(self, model: str, request: ChatCompletionRequest) -> ChatCompletion:
        payload = self._payload(model, request, stream=False)
        try:
            response = await self._client.post(self._url, json=payload)
        except httpx.TimeoutException as exc:
            raise ProviderError(
                "request timed out", provider=self.name, status_code=504, retryable=True
            ) from exc
        except httpx.HTTPError as exc:
            raise ProviderError(
                f"transport error: {exc}", provider=self.name, status_code=502, retryable=True
            ) from exc

        if response.status_code >= 400:
            raise self._upstream_error(response.status_code, response.text)

        try:
            return ChatCompletion.model_validate(response.json())
        except Exception as exc:  # noqa: BLE001 - malformed upstream payload
            raise ProviderError(
                f"invalid upstream payload: {exc}",
                provider=self.name,
                status_code=502,
                retryable=False,
            ) from exc

    async def chat_stream(
        self, model: str, request: ChatCompletionRequest
    ) -> AsyncIterator[ChatCompletionChunk]:
        payload = self._payload(model, request, stream=True)
        try:
            async with self._client.stream("POST", self._url, json=payload) as response:
                if response.status_code >= 400:
                    body = (await response.aread()).decode("utf-8", "replace")
                    raise self._upstream_error(response.status_code, body)

                async for line in response.aiter_lines():
                    line = line.strip()
                    if not line or not line.startswith("data:"):
                        continue
                    data = line[len("data:") :].strip()
                    if data == "[DONE]":
                        return
                    if not data:
                        continue
                    try:
                        obj = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    yield ChatCompletionChunk.model_validate(obj)
        except ProviderError:
            raise
        except httpx.TimeoutException as exc:
            raise ProviderError(
                "stream timed out", provider=self.name, status_code=504, retryable=False
            ) from exc
        except httpx.HTTPError as exc:
            raise ProviderError(
                f"stream transport error: {exc}",
                provider=self.name,
                status_code=502,
                retryable=False,
            ) from exc

    async def aclose(self) -> None:
        await self._client.aclose()

    def info(self) -> dict[str, Any]:
        return {"name": self.name, "type": self.type, "url": self._url}
