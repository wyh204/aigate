"""OpenAI wire format.

This *is* the internal protocol for now: every provider speaks it or is
translated into it, and every client already understands it. When Anthropic's
native API lands (M2), introduce an internal schema and translate at the edge.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

Role = Literal["system", "user", "assistant", "tool"]


class ChatMessage(BaseModel):
    model_config = ConfigDict(extra="allow")

    role: Role = "user"
    content: str | list[dict[str, Any]] | None = None
    name: str | None = None
    tool_calls: list[dict[str, Any]] | None = None
    tool_call_id: str | None = None


class StreamOptions(BaseModel):
    include_usage: bool = False


class ChatCompletionRequest(BaseModel):
    """Accepts unknown fields (``response_format``, ``tools``, ...) and forwards them."""

    model_config = ConfigDict(extra="allow")

    model: str
    messages: list[ChatMessage] = Field(default_factory=list)
    stream: bool = False
    stream_options: StreamOptions | None = None
    temperature: float | None = None
    top_p: float | None = None
    max_tokens: int | None = None
    stop: str | list[str] | None = None
    n: int | None = None

    def upstream_payload(self, model: str) -> dict[str, Any]:
        """Body to send upstream, with the resolved model substituted."""
        payload = self.model_dump(exclude_none=True, exclude={"stream_options"})
        payload["model"] = model
        return payload


class Usage(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


class Choice(BaseModel):
    index: int = 0
    message: ChatMessage
    finish_reason: str | None = None
    logprobs: Any | None = None


class ChatCompletion(BaseModel):
    id: str
    object: Literal["chat.completion"] = "chat.completion"
    created: int
    model: str
    choices: list[Choice]
    usage: Usage | None = None


class Delta(BaseModel):
    role: str | None = None
    content: str | None = None


class ChunkChoice(BaseModel):
    index: int = 0
    delta: Delta = Field(default_factory=Delta)
    finish_reason: str | None = None


class ChatCompletionChunk(BaseModel):
    id: str
    object: Literal["chat.completion.chunk"] = "chat.completion.chunk"
    created: int
    model: str
    choices: list[ChunkChoice] = Field(default_factory=list)
    usage: Usage | None = None
