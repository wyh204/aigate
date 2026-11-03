"""Monthly quota: checked before the call, incremented by the usage writer.

The check reads committed counters, so it is eventually consistent at the
granularity of one batch flush (~0.5s). That is deliberate: a hard real-time
check would need a lock or a counter store in front of the database, which is
M4/M6 material, not M3.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select

from app.db.models import Quota, Tenant
from app.db.session import Database
from app.errors import APIError


def current_period(now: datetime | None = None) -> str:
    return (now or datetime.now(UTC)).strftime("%Y-%m")


async def enforce(database: Database | None, tenant_id: int | None, tenant_slug: str) -> None:
    if database is None or tenant_id is None:
        return

    period = current_period()
    async with database.session() as session:
        quota = await session.scalar(
            select(Quota).where(Quota.tenant_id == tenant_id, Quota.period == period)
        )
        tenant = await session.get(Tenant, tenant_id)

    budget = tenant.monthly_budget_usd if tenant is not None else None
    if quota is None and budget is None:
        return

    tokens_limit = quota.tokens_limit if quota is not None else None
    cost_limit = quota.cost_limit_usd if quota is not None else None
    if budget is not None:
        cost_limit = budget if cost_limit is None else min(cost_limit, budget)

    tokens_used = quota.tokens_used if quota is not None else 0
    cost_used = quota.cost_used_usd if quota is not None else 0.0

    if tokens_limit is not None and tokens_used >= tokens_limit:
        raise APIError(
            429,
            f"Monthly token quota exhausted for {tenant_slug!r}: "
            f"{tokens_used}/{tokens_limit} tokens used in {period}.",
            type_="rate_limit_error",
            code="quota_exceeded",
        )
    if cost_limit is not None and cost_used >= cost_limit:
        raise APIError(
            429,
            f"Monthly budget exhausted for {tenant_slug!r}: "
            f"${cost_used:.4f}/${cost_limit:.4f} spent in {period}.",
            type_="rate_limit_error",
            code="quota_exceeded",
        )
