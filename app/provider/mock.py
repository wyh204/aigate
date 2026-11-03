"""Deterministic offline provider.

Its job: let the whole request path (routing, streaming, tracing, tests) run
with zero API keys and zero spend. Every reply is prefixed with ``[mock:model]``
so a mock response can never be mistaken for a real one in a demo.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections.abc import AsyncIterator

from app.protocol.openai import (
    ChatCompletion,
    ChatCompletionChunk,
    ChatCompletionRequest,
    ChatMessage,
    Choice,
    ChunkChoice,
    Delta,
    Usage,
)
from app.provider.base import Provider
from app.usage import estimate_tokens, messages_text


def _split(text: str, size: int) -> list[str]:
    return [text[index : index + size] for index in range(0, len(text), size)] or [""]


class MockProvider(Provider):
    type = "mock"

    def __init__(self, *, name: str = "mock", delay_s: float = 0.0, chunk_chars: int = 4) -> None:
        self.name = name
        self._delay_s = delay_s
        self._chunk_chars = max(1, chunk_chars)

    def _reply(self, model: str, request: ChatCompletionRequest) -> str:
        user_text = ""
        for message in reversed(request.messages):
            if message.role == "user":
                content = message.content
                if isinstance(content, str):
                    user_text = content
                elif content is not None:
                    user_text = json.dumps(content, ensure_ascii=False)
                break
        return f"[mock:{model}] {user_text}".strip()

    def _usage(self, request: ChatCompletionRequest, text: str) -> Usage:
        prompt_tokens = estimate_tokens(messages_text(request.messages))
        completion_tokens = estimate_tokens(text)
        return Usage(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=prompt_tokens + completion_tokens,
        )

    async def chat(self, model: str, request: ChatCompletionRequest) -> ChatCompletion:
        text = self._reply(model, request)
        return ChatCompletion(
            id=f"chatcmpl-mock-{uuid.uuid4().hex[:20]}",
            created=int(time.time()),
            model=request.model,
            choices=[
                Choice(
                    index=0,
                    message=ChatMessage(role="assistant", content=text),
                    finish_reason="stop",
                )
            ],
            usage=self._usage(request, text),
        )

    async def chat_stream(
        self, model: str, request: ChatCompletionRequest
    ) -> AsyncIterator[ChatCompletionChunk]:
        text = self._reply(model, request)
        completion_id = f"chatcmpl-mock-{uuid.uuid4().hex[:20]}"
        created = int(time.time())

        def chunk(**kwargs) -> ChatCompletionChunk:
            return ChatCompletionChunk(
                id=completion_id, created=created, model=request.model, **kwargs
            )

        yield chunk(choices=[ChunkChoice(index=0, delta=Delta(role="assistant", content=""))])

        for piece in _split(text, self._chunk_chars):
            await asyncio.sleep(self._delay_s) if self._delay_s else await asyncio.sleep(0)
            yield chunk(choices=[ChunkChoice(index=0, delta=Delta(content=piece))])

        yield chunk(choices=[ChunkChoice(index=0, delta=Delta(), finish_reason="stop")])

        if request.stream_options is not None and request.stream_options.include_usage:
            yield chunk(choices=[], usage=self._usage(request, text))
