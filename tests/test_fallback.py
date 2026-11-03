"""The part that actually sells the gateway: falling over to the next target.

These call the routing helpers directly — a real HTTP test would need a second
live provider, and the behaviour worth pinning down is "which target got used
and why", not HTTP plumbing (already covered in test_chat.py).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest

from app.api.v1.chat import _complete, _open_stream
from app.errors import APIError
from app.protocol.openai import ChatCompletionChunk, ChatCompletionRequest, ChatMessage
from app.provider.base import Provider, ProviderError
from app.provider.mock import MockProvider
from app.provider.registry import ProviderRegistry
from app.router.simple import Router


class FlakyProvider(Provider):
    name = "flaky"
    type = "test"

    def __init__(self, *, retryable: bool = True) -> None:
        self._retryable = retryable

    def _boom(self) -> ProviderError:
        return ProviderError("upstream exploded", provider=self.name, retryable=self._retryable)

    async def chat(self, model: str, request: ChatCompletionRequest):
        raise self._boom()

    async def chat_stream(
        self, model: str, request: ChatCompletionRequest
    ) -> AsyncIterator[ChatCompletionChunk]:
        raise self._boom()
        yield  # pragma: no cover - makes this an async generator


def _payload() -> ChatCompletionRequest:
    return ChatCompletionRequest(model="default", messages=[ChatMessage(role="user", content="hi")])


def _setup(retryable: bool) -> tuple[Router, ProviderRegistry]:
    registry = ProviderRegistry([FlakyProvider(retryable=retryable), MockProvider()])
    route_table = {"default": ("flaky:any", "mock:mock-echo")}
    return Router(route_table, registry), registry


def test_retryable_failure_falls_over() -> None:
    gateway, registry = _setup(retryable=True)
    route = gateway.resolve("default")

    target, completion = asyncio.run(_complete(_payload(), route, registry))

    assert target.provider == "mock"
    assert completion.choices[0].message.content.startswith("[mock:mock-echo]")


def test_non_retryable_failure_does_not_fall_over() -> None:
    gateway, registry = _setup(retryable=False)
    route = gateway.resolve("default")

    with pytest.raises(APIError) as info:
        asyncio.run(_complete(_payload(), route, registry))

    assert info.value.status_code == 502
    assert info.value.code == "upstream_error"


def test_stream_falls_over_before_first_chunk() -> None:
    gateway, registry = _setup(retryable=True)
    route = gateway.resolve("default")

    async def run():
        target, first, stream = await _open_stream(_payload(), route, registry)
        chunks = [first]
        async for chunk in stream:
            chunks.append(chunk)
        return target, chunks

    target, chunks = asyncio.run(run())

    assert target.provider == "mock"
    text = "".join(choice.delta.content or "" for chunk in chunks for choice in chunk.choices)
    assert text.startswith("[mock:mock-echo]")


def test_direct_provider_model_is_accepted() -> None:
    _, registry = _setup(retryable=True)
    gateway = Router({"default": ("mock:mock-echo",)}, registry)

    route = gateway.resolve("mock:mock-echo")

    assert route.reason == "direct"
    assert str(route.primary) == "mock:mock-echo"
