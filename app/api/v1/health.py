"""Liveness/readiness + a cheap way to see what got configured."""

from __future__ import annotations

from fastapi import APIRouter, Request

router = APIRouter()


@router.get("/healthz")
async def healthz(request: Request) -> dict:
    settings = request.app.state.settings
    registry = request.app.state.registry
    gateway = request.app.state.router
    return {
        "status": "ok",
        "version": settings.version,
        "auth_enabled": settings.auth_enabled,
        "providers": [provider.info() for provider in registry.all()],
        "routes": gateway.aliases(),
        "default_route": settings.default_route,
    }
