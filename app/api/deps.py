"""Identity resolution.

Precedence:

1. ``AIGATE_API_KEYS`` — bootstrap keys, no database needed;
2. a key row created through ``/admin/keys`` — the normal path;
3. no key configured anywhere — dev mode, every caller is tenant ``dev``.

Database keys are stored as SHA-256 digests. A gateway that keeps plaintext
keys is a gateway that leaks every tenant's credentials the first time its
database is dumped.
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass

from fastapi import Request
from sqlalchemy import select

from app.db.models import ApiKey, Tenant
from app.errors import APIError


@dataclass(frozen=True, slots=True)
class Identity:
    tenant: str
    key_id: str
    tenant_id: int | None = None
    key_row_id: int | None = None


def _extract_token(request: Request) -> str | None:
    header = request.headers.get("authorization")
    if header:
        parts = header.split(None, 1)
        if len(parts) == 2 and parts[0].lower() == "bearer":
            return parts[1].strip()
        if len(parts) == 1:
            return parts[0].strip()
    key = request.headers.get("x-api-key")
    return key.strip() if key else None


def _digest(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _env_identity(settings, token: str) -> Identity | None:
    for key in settings.api_keys:
        if secrets.compare_digest(token, key):
            digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:8]
            return Identity(tenant=f"key-{digest}", key_id=digest)
    return None


async def _database_identity(request: Request, token: str) -> Identity | None:
    database = getattr(request.app.state, "db", None)
    if database is None:
        return None

    async with database.session() as session:
        hit = (
            await session.execute(
                select(ApiKey, Tenant.slug)
                .join(Tenant, ApiKey.tenant_id == Tenant.id)
                .where(
                    ApiKey.key_hash == _digest(token),
                    ApiKey.revoked_at.is_(None),
                    Tenant.is_active.is_(True),
                )
            )
        ).first()

    if hit is None:
        return None
    key, slug = hit
    return Identity(
        tenant=slug,
        key_id=f"key-{key.id}",
        tenant_id=key.tenant_id,
        key_row_id=key.id,
    )


async def _dev_identity(request: Request) -> Identity:
    """Dev mode still links to the seeded ``dev`` tenant when it exists.

    Without that link, unauthenticated traffic would bypass quota accounting
    entirely and the dev tenant would show zero spend in the dashboard.
    """
    database = getattr(request.app.state, "db", None)
    if database is not None:
        async with database.session() as session:
            tenant = await session.scalar(select(Tenant).where(Tenant.slug == "dev"))
        if tenant is not None:
            return Identity(tenant="dev", key_id="anonymous", tenant_id=tenant.id)
    return Identity(tenant="dev", key_id="anonymous")


async def get_identity(request: Request) -> Identity:
    settings = request.app.state.settings
    token = _extract_token(request)
    auth_required = settings.auth_enabled or bool(
        getattr(request.app.state, "db_keys_present", False)
    )

    if token:
        identity = _env_identity(settings, token) or await _database_identity(request, token)
        if identity is not None:
            request.state.tenant = identity.tenant
            return identity

    if auth_required:
        if not token:
            raise APIError(
                401,
                "Missing API key. Send 'Authorization: Bearer <key>'.",
                type_="authentication_error",
                code="invalid_api_key",
            )
        raise APIError(
            401, "Invalid API key.", type_="authentication_error", code="invalid_api_key"
        )

    return await _dev_identity(request)
