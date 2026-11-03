"""Provider abstraction: one interface, N vendors."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from typing import Any

from app.protocol.openai import ChatCompletion, ChatCompletionChunk, ChatCompletionRequest


class ProviderError(Exception):
    """Upstream failure, already classified for the fallback chain.

    ``retryable`` means: another provider in the chain may succeed, so the
    router is allowed to try the next target.
    """

    def __init__(
        self,
        message: str,
        *,
        provider: str,
        status_code: int = 502,
        retryable: bool = False,
        upstream_status: int | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.provider = provider
        self.status_code = status_code
        self.retryable = retryable
        self.upstream_status = upstream_status


class Provider(ABC):
    name: str = "unknown"
    type: str = "unknown"

    @abstractmethod
    async def chat(self, model: str, request: ChatCompletionRequest) -> ChatCompletion:
        """One non-streaming completion."""

    @abstractmethod
    def chat_stream(
        self, model: str, request: ChatCompletionRequest
    ) -> AsyncIterator[ChatCompletionChunk]:
        """Async generator of chunks. Raises ProviderError before the first chunk
        when the upstream is unreachable, so the router can still fail over."""

    async def aclose(self) -> None:
        return None

    def info(self) -> dict[str, Any]:
        return {"name": self.name, "type": self.type}
