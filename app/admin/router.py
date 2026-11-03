"""Admin API: tenants, keys, quotas, prices, and the cost numbers.

Everything here is gated by ``AIGATE_ADMIN_TOKEN``. If the token is not
configured the whole surface answers 503 instead of falling open — a gateway
exposing its key management to the internet is a much bigger problem than a
missing dashboard.

The usage endpoints flush the usage writer first, so they always read their own
writes instead of being one flush interval behind.
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import UTC, date, datetime, time, timedelta
from typing import Literal

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import case, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ApiKey, ModelPrice, Quota, RequestLog, Tenant
from app.db.session import Database
from app.errors import APIError
from app.logging import get_logger
from app.pricing import PriceBook
from app.quota import current_period

logger = get_logger(__name__)

router = APIRouter(prefix="/admin", tags=["admin"])

KEY_PREFIX = "sk-aigate-"


def _hash_key(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def require_admin(request: Request) -> None:
    settings = request.app.state.settings
    if not settings.admin_enabled:
        raise APIError(
            503,
            "Admin API is disabled. Set AIGATE_ADMIN_TOKEN to enable it.",
            code="admin_disabled",
        )

    supplied = request.headers.get("x-admin-token", "").strip()
    if not supplied:
        header = request.headers.get("authorization", "")
        parts = header.split(None, 1)
        if len(parts) == 2 and parts[0].lower() == "bearer":
            supplied = parts[1].strip()

    if not supplied or not secrets.compare_digest(supplied, settings.admin_token):
        raise APIError(
            401,
            "Invalid admin token.",
            type_="authentication_error",
            code="invalid_admin_token",
        )


admin_only = [Depends(require_admin)]


# ---------------------------------------------------------------- schemas


class TenantCreate(BaseModel):
    slug: str = Field(min_length=2, max_length=64, pattern=r"^[a-z0-9][a-z0-9_-]*$")
    name: str = ""
    monthly_budget_usd: float | None = Field(default=None, ge=0)


class KeyCreate(BaseModel):
    tenant: str
    name: str = ""


class QuotaUpdate(BaseModel):
    tokens_limit: int | None = Field(default=None, ge=0)
    cost_limit_usd: float | None = Field(default=None, ge=0)


class PriceUpsert(BaseModel):
    provider: str
    model: str
    input_usd_per_1m: float = Field(ge=0)
    output_usd_per_1m: float = Field(ge=0)


# ---------------------------------------------------------------- helpers


def _db(request: Request) -> Database:
    return request.app.state.db


async def _tenant_or_404(session: AsyncSession, slug: str) -> Tenant:
    tenant = await session.scalar(select(Tenant).where(Tenant.slug == slug))
    if tenant is None:
        raise APIError(404, f"Unknown tenant {slug!r}.", code="tenant_not_found")
    return tenant


def _tenant_payload(tenant: Tenant, **extra: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "id": tenant.id,
        "slug": tenant.slug,
        "name": tenant.name,
        "monthly_budget_usd": tenant.monthly_budget_usd,
        "is_active": tenant.is_active,
        "created_at": tenant.created_at.isoformat(),
    }
    payload.update(extra)
    return payload


# ---------------------------------------------------------------- tenants


@router.post("/tenants", status_code=201, dependencies=admin_only)
async def create_tenant(body: TenantCreate, request: Request) -> dict[str, object]:
    database = _db(request)
    async with database.session() as session:
        exists = await session.scalar(select(Tenant).where(Tenant.slug == body.slug))
        if exists is not None:
            raise APIError(409, f"Tenant {body.slug!r} already exists.", code="tenant_exists")
        tenant = Tenant(
            slug=body.slug,
            name=body.name or body.slug,
            monthly_budget_usd=body.monthly_budget_usd,
        )
        session.add(tenant)
        await session.commit()
        await session.refresh(tenant)
        return _tenant_payload(tenant)


@router.get("/tenants", dependencies=admin_only)
async def list_tenants(request: Request) -> dict[str, object]:
    database = _db(request)
    period_start = datetime.now(UTC).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    month_cost = (
        select(func.coalesce(func.sum(RequestLog.cost_usd), 0.0))
        .where(RequestLog.tenant == Tenant.slug, RequestLog.created_at >= period_start)
        .scalar_subquery()
    )
    month_tokens = (
        select(func.coalesce(func.sum(RequestLog.total_tokens), 0))
        .where(RequestLog.tenant == Tenant.slug, RequestLog.created_at >= period_start)
        .scalar_subquery()
    )
    async with database.session() as session:
        rows = (
            await session.execute(select(Tenant, month_cost, month_tokens).order_by(Tenant.slug))
        ).all()
    return {
        "period": current_period(),
        "data": [
            _tenant_payload(
                tenant,
                month_cost_usd=round(float(cost or 0.0), 6),
                month_tokens=int(tokens or 0),
            )
            for tenant, cost, tokens in rows
        ],
    }


# ---------------------------------------------------------------- keys


@router.post("/keys", status_code=201, dependencies=admin_only)
async def create_key(body: KeyCreate, request: Request) -> dict[str, object]:
    database = _db(request)
    raw = KEY_PREFIX + secrets.token_urlsafe(32)
    async with database.session() as session:
        tenant = await _tenant_or_404(session, body.tenant)
        row = ApiKey(
            tenant_id=tenant.id,
            name=body.name,
            key_hash=_hash_key(raw),
            key_prefix=raw[: len(KEY_PREFIX) + 6],
        )
        session.add(row)
        await session.commit()
        await session.refresh(row)
        key_id, prefix = row.id, row.key_prefix

    # From now on the database decides authentication, not the env file.
    request.app.state.db_keys_present = True
    return {
        "id": key_id,
        "tenant": body.tenant,
        "name": body.name,
        "prefix": prefix,
        "key": raw,
        "note": "This is the only time the key is shown. Only its SHA-256 digest is stored.",
    }


@router.get("/keys", dependencies=admin_only)
async def list_keys(request: Request, tenant: str | None = None) -> dict[str, object]:
    database = _db(request)
    statement = select(ApiKey, Tenant.slug).join(Tenant, ApiKey.tenant_id == Tenant.id)
    if tenant:
        statement = statement.where(Tenant.slug == tenant)
    async with database.session() as session:
        rows = (await session.execute(statement.order_by(ApiKey.id))).all()
    return {
        "data": [
            {
                "id": key.id,
                "tenant": slug,
                "name": key.name,
                "prefix": key.key_prefix,
                "created_at": key.created_at.isoformat(),
                "revoked_at": key.revoked_at.isoformat() if key.revoked_at else None,
            }
            for key, slug in rows
        ]
    }


@router.delete("/keys/{key_id}", dependencies=admin_only)
async def revoke_key(key_id: int, request: Request) -> dict[str, object]:
    database = _db(request)
    async with database.session() as session:
        row = await session.get(ApiKey, key_id)
        if row is None:
            raise APIError(404, f"Unknown key {key_id}.", code="key_not_found")
        row.revoked_at = datetime.now(UTC)
        await session.commit()
    return {"id": key_id, "revoked": True}


# ---------------------------------------------------------------- quotas


@router.get("/quotas/{tenant}", dependencies=admin_only)
async def get_quota(tenant: str, request: Request) -> dict[str, object]:
    database = _db(request)
    await request.app.state.writer.flush()
    period = current_period()
    async with database.session() as session:
        row = await _tenant_or_404(session, tenant)
        quota = await session.scalar(
            select(Quota).where(Quota.tenant_id == row.id, Quota.period == period)
        )
    return {
        "tenant": tenant,
        "period": period,
        "tokens_used": quota.tokens_used if quota else 0,
        "cost_used_usd": round(quota.cost_used_usd, 6) if quota else 0.0,
        "tokens_limit": quota.tokens_limit if quota else None,
        "cost_limit_usd": quota.cost_limit_usd if quota else None,
        "monthly_budget_usd": row.monthly_budget_usd,
    }


@router.put("/quotas/{tenant}", dependencies=admin_only)
async def set_quota(tenant: str, body: QuotaUpdate, request: Request) -> dict[str, object]:
    database = _db(request)
    period = current_period()
    async with database.session() as session:
        row = await _tenant_or_404(session, tenant)
        result = await session.execute(
            update(Quota)
            .where(Quota.tenant_id == row.id, Quota.period == period)
            .values(tokens_limit=body.tokens_limit, cost_limit_usd=body.cost_limit_usd)
        )
        if result.rowcount == 0:
            session.add(
                Quota(
                    tenant_id=row.id,
                    period=period,
                    tokens_limit=body.tokens_limit,
                    cost_limit_usd=body.cost_limit_usd,
                )
            )
        await session.commit()
    return await get_quota(tenant, request)


# ---------------------------------------------------------------- prices


@router.get("/prices", dependencies=admin_only)
async def list_prices(request: Request) -> dict[str, object]:
    database = _db(request)
    async with database.session() as session:
        rows = (await session.scalars(select(ModelPrice).order_by(ModelPrice.provider))).all()
    return {
        "data": [
            {
                "provider": row.provider,
                "model": row.model,
                "input_usd_per_1m": row.input_usd_per_1m,
                "output_usd_per_1m": row.output_usd_per_1m,
            }
            for row in rows
        ]
    }


@router.put("/prices", dependencies=admin_only)
async def upsert_price(body: PriceUpsert, request: Request) -> dict[str, object]:
    database = _db(request)
    async with database.session() as session:
        row = await session.scalar(
            select(ModelPrice).where(
                ModelPrice.provider == body.provider, ModelPrice.model == body.model
            )
        )
        if row is None:
            row = ModelPrice(provider=body.provider, model=body.model)
            session.add(row)
        row.input_usd_per_1m = body.input_usd_per_1m
        row.output_usd_per_1m = body.output_usd_per_1m
        await session.commit()

    # The price book is cached in memory; refresh so the next request uses the
    # new numbers, not the ones loaded at startup.
    request.app.state.price_book = await PriceBook.load(database)
    request.app.state.writer.set_price_book(request.app.state.price_book)
    cache = getattr(request.app.state, "cache", None)
    if cache is not None:
        cache.set_price_book(request.app.state.price_book)
    return {
        "provider": body.provider,
        "model": body.model,
        "input_usd_per_1m": body.input_usd_per_1m,
        "output_usd_per_1m": body.output_usd_per_1m,
    }


# ---------------------------------------------------------------- usage


def _bucket_expression(dialect: str, group_by: str):
    if group_by == "provider":
        return RequestLog.provider
    if group_by == "model":
        return RequestLog.model
    if dialect == "postgresql":
        return func.to_char(RequestLog.created_at, "YYYY-MM-DD")
    return func.strftime("%Y-%m-%d", RequestLog.created_at)


def _window(start: date | None, end: date | None) -> tuple[datetime, datetime | None]:
    if start is None:
        start_dt = datetime.now(UTC).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    else:
        start_dt = datetime.combine(start, time.min, tzinfo=UTC)
    end_dt = datetime.combine(end + timedelta(days=1), time.min, tzinfo=UTC) if end else None
    return start_dt, end_dt


@router.get("/usage", dependencies=admin_only)
async def usage(
    request: Request,
    tenant: str | None = None,
    start: date | None = None,
    end: date | None = None,
    group_by: Literal["day", "model", "provider"] = "day",
) -> dict[str, object]:
    database = _db(request)
    await request.app.state.writer.flush()

    start_dt, end_dt = _window(start, end)
    dialect = database.engine.dialect.name
    bucket = _bucket_expression(dialect, group_by).label("bucket")

    filters = [RequestLog.created_at >= start_dt]
    if end_dt is not None:
        filters.append(RequestLog.created_at < end_dt)
    if tenant:
        filters.append(RequestLog.tenant == tenant)

    cache_hit = case((RequestLog.cache_hit, 1), else_=0)
    estimated = case((RequestLog.usage_estimated, 1), else_=0)

    statement = (
        select(
            bucket,
            func.count().label("requests"),
            func.coalesce(func.sum(RequestLog.prompt_tokens), 0).label("prompt_tokens"),
            func.coalesce(func.sum(RequestLog.completion_tokens), 0).label("completion_tokens"),
            func.coalesce(func.sum(RequestLog.total_tokens), 0).label("total_tokens"),
            func.coalesce(func.sum(RequestLog.cost_usd), 0.0).label("cost_usd"),
            func.coalesce(func.sum(RequestLog.saved_cost_usd), 0.0).label("saved_usd"),
            func.coalesce(func.sum(cache_hit), 0).label("cache_hits"),
            func.coalesce(func.sum(estimated), 0).label("estimated"),
            func.avg(RequestLog.latency_ms).label("avg_latency_ms"),
            func.avg(RequestLog.ttft_ms).label("avg_ttft_ms"),
        )
        .where(*filters)
        .group_by(bucket)
        .order_by(bucket)
    )

    async with database.session() as session:
        rows = (await session.execute(statement)).all()

    data = []
    totals = {
        "requests": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "cost_usd": 0.0,
        "saved_usd": 0.0,
        "cache_hits": 0,
        "estimated_requests": 0,
    }
    for row in rows:
        requests_count = int(row.requests or 0)
        item = {
            "bucket": str(row.bucket),
            "requests": requests_count,
            "prompt_tokens": int(row.prompt_tokens or 0),
            "completion_tokens": int(row.completion_tokens or 0),
            "total_tokens": int(row.total_tokens or 0),
            "cost_usd": round(float(row.cost_usd or 0.0), 8),
            "saved_usd": round(float(row.saved_usd or 0.0), 8),
            "cache_hits": int(row.cache_hits or 0),
            "cache_hit_rate": round(int(row.cache_hits or 0) / requests_count, 4)
            if requests_count
            else 0.0,
            "estimated_share": round(int(row.estimated or 0) / requests_count, 4)
            if requests_count
            else 0.0,
            "avg_latency_ms": round(float(row.avg_latency_ms or 0.0), 2),
            "avg_ttft_ms": round(float(row.avg_ttft_ms), 2)
            if row.avg_ttft_ms is not None
            else None,
        }
        data.append(item)
        for key in ("requests", "prompt_tokens", "completion_tokens", "total_tokens", "cache_hits"):
            totals[key] += item[key]
        totals["cost_usd"] += item["cost_usd"]
        totals["saved_usd"] += item["saved_usd"]
        totals["estimated_requests"] += round(item["estimated_share"] * requests_count)

    totals["cost_usd"] = round(totals["cost_usd"], 8)
    totals["saved_usd"] = round(totals["saved_usd"], 8)
    if totals["requests"]:
        totals["cache_hit_rate"] = round(totals["cache_hits"] / totals["requests"], 4)
    else:
        totals["cache_hit_rate"] = 0.0
    return {
        "tenant": tenant,
        "group_by": group_by,
        "range": {
            "start": start_dt.isoformat(),
            "end": end_dt.isoformat() if end_dt else None,
            "timezone": "UTC",
        },
        "totals": totals,
        "data": data,
    }


@router.get("/requests", dependencies=admin_only)
async def recent_requests(
    request: Request,
    tenant: str | None = None,
    status: str | None = None,
    limit: int = Query(default=50, ge=1, le=500),
) -> dict[str, object]:
    database = _db(request)
    await request.app.state.writer.flush()

    statement = select(RequestLog).order_by(RequestLog.created_at.desc()).limit(limit)
    if tenant:
        statement = statement.where(RequestLog.tenant == tenant)
    if status:
        statement = statement.where(RequestLog.status == status)

    async with database.session() as session:
        rows = (await session.scalars(statement)).all()
    return {
        "data": [
            {
                "request_id": row.request_id,
                "tenant": row.tenant,
                "route": row.route,
                "provider": row.provider,
                "model": row.model,
                "stream": row.stream,
                "total_tokens": row.total_tokens,
                "cost_usd": row.cost_usd,
                "saved_usd": row.saved_cost_usd,
                "latency_ms": row.latency_ms,
                "ttft_ms": row.ttft_ms,
                "status": row.status,
                "cache_hit": row.cache_hit,
                "usage_estimated": row.usage_estimated,
                "created_at": row.created_at.isoformat(),
            }
            for row in rows
        ]
    }


@router.get("/cache/stats", dependencies=admin_only)
async def cache_stats(request: Request) -> dict[str, object]:
    cache = getattr(request.app.state, "cache", None)
    if cache is None:
        return {"enabled": False, "reason": "cache not initialised"}
    return cache.stats()


@router.post("/cache/purge", dependencies=admin_only)
async def cache_purge(request: Request) -> dict[str, object]:
    cache = getattr(request.app.state, "cache", None)
    if cache is None:
        return {"removed": 0, "enabled": False}
    removed = await cache.purge()
    return {"removed": removed, "enabled": cache.enabled}
