"""POST /v1/chat/completions — the only endpoint that matters right now.

Two paths share one routing/fallback policy:

* non-stream: try the chain in order, first success wins.
* stream: open the stream and wait for the **first** chunk *before* committing
  to HTTP 200. That is what makes failover possible at all — after the first
  byte the status line is gone, so a mid-stream failure can only be reported as
  an SSE error event.
"""

from __future__ import annotations

import contextlib
import json
import time
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from app.api.deps import Identity, get_identity
from app.cache.service import CacheHit, CacheService
from app.db.writer import UsageEvent
from app.errors import APIError, error_body
from app.logging import get_logger
from app.protocol.openai import (
    ChatCompletion,
    ChatCompletionChunk,
    ChatCompletionRequest,
    Usage,
)
from app.provider.base import ProviderError
from app.provider.registry import ProviderRegistry
from app.quota import enforce as enforce_quota
from app.router.simple import Route, RouteNotFound, Router, RouteTarget
from app.trace import TraceRecord, record_trace
from app.usage import estimate_tokens, messages_text

router = APIRouter()
logger = get_logger(__name__)


def _headers(
    route: Route, target: RouteTarget, request_id: str, *, cache: str = "miss"
) -> dict[str, str]:
    return {
        "x-request-id": request_id,
        "x-aigate-route": route.alias,
        "x-aigate-provider": target.provider,
        "x-aigate-model": target.model,
        "x-aigate-cache": cache,
        "cache-control": "no-cache",
        "x-accel-buffering": "no",
    }


def _cache_bypass(request: Request) -> bool:
    """Standard ``Cache-Control: no-cache`` plus an explicit gateway header."""
    directive = request.headers.get("x-aigate-cache", "").strip().lower()
    if directive in {"off", "bypass", "no", "none"}:
        return True
    return "no-cache" in request.headers.get("cache-control", "").lower()


@router.post("/chat/completions")
async def create_chat_completion(
    payload: ChatCompletionRequest,
    request: Request,
    identity: Identity = Depends(get_identity),
) -> Response:
    state = request.app.state
    gateway: Router = state.router
    registry: ProviderRegistry = state.registry
    request_id: str = getattr(request.state, "request_id", "-")

    try:
        route = gateway.resolve(payload.model)
    except RouteNotFound as exc:
        raise APIError(
            404,
            f"Model {payload.model!r} is not available. Known routes: "
            f"{', '.join(gateway.aliases())}",
            code="model_not_found",
        ) from exc

    # Quota is checked before either path: a streamed request must not be a way
    # to spend past the budget. It is also checked *before the cache* so a cached
    # answer can never be a way around an exhausted quota.
    await enforce_quota(getattr(state, "db", None), identity.tenant_id, identity.tenant)

    cache: CacheService | None = getattr(state, "cache", None)
    cache_active = cache is not None and cache.enabled and not _cache_bypass(request)

    if payload.stream:
        # M4: streams bypass the cache. Replaying SSE from a stored completion
        # needs a chunker; until that exists, saying "hit" here would be a lie.
        return await _streaming_response(
            payload,
            route,
            registry,
            identity,
            request,
            cache="bypass" if cache is not None and cache.enabled else "off",
        )

    started = time.perf_counter()
    if cache_active:
        hit = await cache.lookup(identity.tenant, route, payload)
        if hit is not None:
            return _cached_response(hit, route, request, identity, started)

    target, completion = await _complete(payload, route, registry)
    latency_ms = round((time.perf_counter() - started) * 1000, 2)
    usage = completion.usage or Usage()

    _record(
        request,
        identity,
        route,
        target,
        usage,
        stream=False,
        latency_ms=latency_ms,
        ttft_ms=None,
        status="ok",
        usage_estimated=completion.usage is None,
    )
    if cache_active:
        await cache.store(identity.tenant, route, payload, target, completion)
    return JSONResponse(
        completion.model_dump(exclude_none=True),
        headers=_headers(route, target, request_id, cache="miss" if cache_active else "off"),
    )


def _cached_response(
    hit: CacheHit,
    route: Route,
    request: Request,
    identity: Identity,
    started: float,
) -> JSONResponse:
    target = RouteTarget(provider=hit.provider, model=hit.model)
    usage = hit.completion.usage or Usage()
    _record(
        request,
        identity,
        route,
        target,
        usage,
        stream=False,
        latency_ms=round((time.perf_counter() - started) * 1000, 2),
        ttft_ms=None,
        status="ok",
        usage_estimated=False,
        cache_hit=True,
        saved_usd=hit.saved_usd,
    )
    headers = _headers(route, target, getattr(request.state, "request_id", "-"), cache=hit.kind)
    if hit.similarity is not None:
        headers["x-aigate-cache-similarity"] = f"{hit.similarity:.4f}"
    headers["x-aigate-cache-age"] = f"{hit.age_s:.1f}"
    return JSONResponse(hit.completion.model_dump(exclude_none=True), headers=headers)


async def _complete(
    payload: ChatCompletionRequest, route: Route, registry: ProviderRegistry
) -> tuple[RouteTarget, ChatCompletion]:
    failures: list[str] = []
    for target in route.chain:
        provider = registry.get(target.provider)
        try:
            return target, await provider.chat(target.model, payload)
        except ProviderError as exc:
            failures.append(f"{target}: {exc.message}")
            logger.warning(
                "provider call failed",
                extra={
                    "event": "provider_error",
                    "provider": target.provider,
                    "model": target.model,
                    "retryable": exc.retryable,
                    "upstream_status": exc.upstream_status,
                    "error": exc.message,
                },
            )
            if not exc.retryable:
                raise APIError(
                    502,
                    f"Upstream error from {target}: {exc.message}",
                    type_="upstream_error",
                    code="upstream_error",
                ) from exc

    raise APIError(
        502,
        "All providers failed: " + "; ".join(failures),
        type_="upstream_error",
        code="upstream_error",
    )


async def _open_stream(
    payload: ChatCompletionRequest, route: Route, registry: ProviderRegistry
) -> tuple[RouteTarget, ChatCompletionChunk, AsyncIterator[ChatCompletionChunk]]:
    failures: list[str] = []
    for target in route.chain:
        provider = registry.get(target.provider)
        stream = provider.chat_stream(target.model, payload)
        try:
            first = await anext(stream)
        except StopAsyncIteration as exc:
            raise APIError(
                502,
                f"Provider {target} returned an empty stream",
                type_="upstream_error",
                code="upstream_error",
            ) from exc
        except ProviderError as exc:
            failures.append(f"{target}: {exc.message}")
            logger.warning(
                "provider stream failed before first chunk",
                extra={
                    "event": "provider_error",
                    "provider": target.provider,
                    "model": target.model,
                    "retryable": exc.retryable,
                    "upstream_status": exc.upstream_status,
                    "error": exc.message,
                },
            )
            if not exc.retryable:
                raise APIError(
                    502,
                    f"Upstream error from {target}: {exc.message}",
                    type_="upstream_error",
                    code="upstream_error",
                ) from exc
            with contextlib.suppress(Exception):
                await stream.aclose()
            continue
        return target, first, stream

    raise APIError(
        502,
        "All providers failed: " + "; ".join(failures),
        type_="upstream_error",
        code="upstream_error",
    )


async def _streaming_response(
    payload: ChatCompletionRequest,
    route: Route,
    registry: ProviderRegistry,
    identity: Identity,
    request: Request,
    *,
    cache: str = "bypass",
) -> Response:
    request_id: str = getattr(request.state, "request_id", "-")
    started = time.perf_counter()
    target, first, stream = await _open_stream(payload, route, registry)
    ttft_ms = round((time.perf_counter() - started) * 1000, 2)

    body = _stream_body(first, stream, payload, route, target, identity, request, started, ttft_ms)
    return StreamingResponse(
        body,
        media_type="text/event-stream",
        headers=_headers(route, target, request_id, cache=cache),
    )


async def _stream_body(
    first: ChatCompletionChunk,
    stream: AsyncIterator[ChatCompletionChunk],
    payload: ChatCompletionRequest,
    route: Route,
    target: RouteTarget,
    identity: Identity,
    request: Request,
    started: float,
    ttft_ms: float,
) -> AsyncIterator[str]:
    aggregate: dict[str, Any] = {"usage": None, "text": []}
    status = "ok"
    try:
        _collect(first, aggregate)
        yield _format(first)
        async for chunk in stream:
            _collect(chunk, aggregate)
            yield _format(chunk)
    except ProviderError as exc:
        status = "stream_error"
        logger.warning(
            "provider stream failed mid-response",
            extra={
                "event": "provider_error",
                "provider": target.provider,
                "model": target.model,
                "error": exc.message,
            },
        )
        yield _error_event(exc)
    finally:
        with contextlib.suppress(Exception):
            await stream.aclose()
        usage = aggregate["usage"]
        usage_estimated = usage is None
        if usage is None:
            prompt_tokens = estimate_tokens(messages_text(payload.messages))
            completion_tokens = estimate_tokens("".join(aggregate["text"]))
            usage = Usage(
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=prompt_tokens + completion_tokens,
            )
        _record(
            request,
            identity,
            route,
            target,
            usage,
            stream=True,
            latency_ms=round((time.perf_counter() - started) * 1000, 2),
            ttft_ms=ttft_ms,
            status=status,
            usage_estimated=usage_estimated,
        )

    yield "data: [DONE]\n\n"


def _record(
    request: Request,
    identity: Identity,
    route: Route,
    target: RouteTarget,
    usage: Usage,
    *,
    stream: bool,
    latency_ms: float,
    ttft_ms: float | None,
    status: str,
    usage_estimated: bool,
    cache_hit: bool = False,
    saved_usd: float = 0.0,
) -> None:
    """Write the trace line and hand the call to the usage writer.

    The writer is fire-and-forget: the response never waits for a database
    commit, and a saturated queue drops the metering row instead of the request.
    """
    record = TraceRecord(
        request_id=getattr(request.state, "request_id", "-"),
        tenant=identity.tenant,
        route=route.alias,
        provider=target.provider,
        model=target.model,
        stream=stream,
        prompt_tokens=usage.prompt_tokens,
        completion_tokens=usage.completion_tokens,
        total_tokens=usage.total_tokens,
        latency_ms=latency_ms,
        ttft_ms=ttft_ms,
        status=status,
        cache_hit=cache_hit,
        usage_estimated=usage_estimated,
    )
    record_trace(record)

    writer = getattr(request.app.state, "writer", None)
    if writer is None:
        return
    writer.submit(
        UsageEvent(
            request_id=record.request_id,
            tenant=record.tenant,
            tenant_id=identity.tenant_id,
            key_id=identity.key_row_id,
            route=record.route,
            provider=record.provider,
            model=record.model,
            stream=record.stream,
            prompt_tokens=record.prompt_tokens,
            completion_tokens=record.completion_tokens,
            total_tokens=record.total_tokens,
            latency_ms=record.latency_ms,
            ttft_ms=record.ttft_ms,
            status=record.status,
            cache_hit=record.cache_hit,
            saved_cost_usd=saved_usd,
            usage_estimated=record.usage_estimated,
        )
    )


def _collect(chunk: ChatCompletionChunk, aggregate: dict[str, Any]) -> None:
    if chunk.usage is not None:
        aggregate["usage"] = chunk.usage
    for choice in chunk.choices:
        if choice.delta is not None and choice.delta.content:
            aggregate["text"].append(choice.delta.content)


def _format(chunk: ChatCompletionChunk) -> str:
    return f"data: {chunk.model_dump_json(exclude_none=True)}\n\n"


def _error_event(exc: ProviderError) -> str:
    body = error_body(
        f"Upstream stream error: {exc.message}",
        type_="upstream_error",
        code="upstream_error",
    )
    return f"data: {json.dumps(body, ensure_ascii=False)}\n\n"
