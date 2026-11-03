"""Pure ASGI middleware.

Deliberately not ``BaseHTTPMiddleware``: that one buffers/wraps StreamingResponse
in ways that show up as broken SSE. This only decorates the response-start
message and logs once the call is finished.
"""

from __future__ import annotations

import time
import uuid

from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.logging import get_logger

REQUEST_ID_HEADER = "x-request-id"
logger = get_logger("app.access")


class RequestContextMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = Headers(scope=scope)
        request_id = headers.get(REQUEST_ID_HEADER) or uuid.uuid4().hex[:16]
        scope.setdefault("state", {})
        scope["state"]["request_id"] = request_id

        started = time.perf_counter()
        status_code = 500

        async def send_wrapper(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
                MutableHeaders(scope=message)[REQUEST_ID_HEADER] = request_id
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            logger.info(
                "http request",
                extra={
                    "event": "http_request",
                    "request_id": request_id,
                    "method": scope.get("method"),
                    "path": scope.get("path"),
                    "status": status_code,
                    "latency_ms": round((time.perf_counter() - started) * 1000, 2),
                },
            )
