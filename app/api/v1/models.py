"""GET /v1/models — every alias the gateway serves."""

from __future__ import annotations

from fastapi import APIRouter, Request

router = APIRouter()


@router.get("/models")
async def list_models(request: Request) -> dict:
    gateway = request.app.state.router
    return {
        "object": "list",
        "data": [
            {"id": alias, "object": "model", "created": 0, "owned_by": "aigate"}
            for alias in gateway.aliases()
        ],
    }
