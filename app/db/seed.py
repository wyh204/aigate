"""Idempotent seed: the dev tenant and the default price rows.

Prices live in the DB so they can be adjusted without a deploy; the built-in
book is only the starting point.
"""

from __future__ import annotations

from sqlalchemy import func, select

from app.db.models import ModelPrice, Tenant
from app.db.session import Database
from app.pricing import DEFAULT_PRICES


async def seed(database: Database) -> None:
    async with database.session() as session:
        if await session.scalar(select(func.count()).select_from(Tenant)) == 0:
            session.add(Tenant(slug="dev", name="Development"))

        known = set((await session.execute(select(ModelPrice.provider, ModelPrice.model))).all())
        for (provider, model), (input_price, output_price) in DEFAULT_PRICES.items():
            if (provider, model) in known:
                continue
            session.add(
                ModelPrice(
                    provider=provider,
                    model=model,
                    input_usd_per_1m=input_price,
                    output_usd_per_1m=output_price,
                )
            )
        await session.commit()
