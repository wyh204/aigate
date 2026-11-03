"""OpenAI-compatible error envelopes, one place for every failure mode."""

from __future__ import annotations

from typing import Any

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.logging import get_logger
from app.provider.base import ProviderError

logger = get_logger(__name__)


class APIError(Exception):
    def __init__(
        self,
        status_code: int,
        message: str,
        *,
        type_: str = "invalid_request_error",
        code: str | None = None,
        param: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message
        self.type = type_
        self.code = code
        self.param = param


def error_body(
    message: str,
    *,
    type_: str = "invalid_request_error",
    code: str | None = None,
    param: str | None = None,
) -> dict[str, Any]:
    return {"error": {"message": message, "type": type_, "param": param, "code": code}}


async def api_error_handler(request: Request, exc: APIError) -> JSONResponse:
    return JSONResponse(
        error_body(exc.message, type_=exc.type, code=exc.code, param=exc.param),
        status_code=exc.status_code,
    )


async def validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    errors = exc.errors()
    first = errors[0] if errors else {}
    reason = first.get("msg", "validation error")
    location = ".".join(str(part) for part in first.get("loc", ()))
    message = f"Invalid request: {reason} (at {location or 'body'})"
    return JSONResponse(error_body(message, code="invalid_request"), status_code=400)


async def http_exception_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    """Every 404/405 leaves in the same shape as every other error."""
    detail = exc.detail
    message = detail if isinstance(detail, str) else str(detail)
    type_ = "authentication_error" if exc.status_code == 401 else "invalid_request_error"
    return JSONResponse(error_body(message, type_=type_), status_code=exc.status_code)


async def provider_error_handler(request: Request, exc: ProviderError) -> JSONResponse:
    return JSONResponse(
        error_body(
            f"Upstream provider error: {exc.message}", type_="upstream_error", code="upstream_error"
        ),
        status_code=502,
    )


async def database_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """A missing column is an operator problem, not a client one: say so."""
    logger.exception("database access failed")
    return JSONResponse(
        error_body(
            "Database error. If this is a fresh checkout, the schema may be "
            "out of date: run `alembic upgrade head`.",
            type_="server_error",
            code="database_error",
        ),
        status_code=503,
    )


async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.exception("unhandled error")
    return JSONResponse(
        error_body("Internal server error", type_="server_error", code="internal_error"),
        status_code=500,
    )
