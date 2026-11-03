"""Application factory: build once, inject everything through app.state."""

from __future__ import annotations

from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from starlette.exceptions import HTTPException as StarletteHTTPException

from app import __version__
from app.admin.evals import router as evals_router
from app.admin.router import router as admin_router
from app.api.v1 import chat, health, models
from app.cache.embedding import embed
from app.cache.service import CacheService
from app.cache.store import MemoryStore, RedisStore
from app.config import Settings, build_settings
from app.db.models import ApiKey
from app.db.seed import seed
from app.db.session import Database
from app.db.writer import UsageWriter
from app.errors import (
    APIError,
    api_error_handler,
    database_error_handler,
    http_exception_handler,
    provider_error_handler,
    unhandled_error_handler,
    validation_error_handler,
)
from app.logging import get_logger, setup_logging
from app.middleware import RequestContextMiddleware
from app.pricing import PriceBook
from app.provider.base import ProviderError
from app.provider.registry import ProviderRegistry
from app.router.simple import Router


async def _has_api_keys(database: Database) -> bool:
    async with database.session() as session:
        count = await session.scalar(
            select(func.count()).select_from(ApiKey).where(ApiKey.revoked_at.is_(None))
        )
    return bool(count)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or build_settings()
    setup_logging(settings.log_level)
    logger = get_logger("app.main")

    registry = ProviderRegistry.from_settings(settings)
    fallback = settings.default_route if settings.unknown_model == "route" else None
    gateway = Router(settings.routes, registry, fallback=fallback)
    database = Database(settings.database_url)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # SQLite is the zero-setup path, so the schema is created on boot.
        # Postgres deployments are expected to run `alembic upgrade head`.
        await database.create_all()
        drifts = await database.schema_drift()
        if drifts:
            logger.warning(
                "database schema is out of date; run `alembic upgrade head`",
                extra={"event": "schema_drift", "drift": drifts},
            )
        await seed(database)
        price_book = await PriceBook.load(database)

        writer = UsageWriter(
            database,
            price_book,
            queue_size=settings.usage_queue_size,
            flush_interval_s=settings.usage_flush_interval_s,
        )
        writer.start()
        app.state.price_book = price_book
        app.state.writer = writer
        app.state.db_keys_present = await _has_api_keys(database)

        store = (
            RedisStore(settings.redis_url)
            if settings.redis_url
            else MemoryStore(max_entries=settings.cache_max_entries)
        )
        cache = CacheService(settings, store, price_book, embedder=embed)
        app.state.cache = cache

        logger.info(
            "gateway starting",
            extra={
                "event": "startup",
                "version": settings.version,
                "providers": [provider.name for provider in registry.all()],
                "routes": gateway.aliases(),
                "auth_enabled": settings.auth_enabled or app.state.db_keys_present,
                "admin_enabled": settings.admin_enabled,
                "database": database.engine.dialect.name,
            },
        )
        yield
        await cache.close()
        await writer.stop()
        await registry.aclose()
        await database.dispose()
        logger.info("gateway stopped", extra={"event": "shutdown"})

    app = FastAPI(title="aigate", version=__version__, lifespan=lifespan)
    app.state.settings = settings
    app.state.registry = registry
    app.state.router = gateway
    app.state.db = database

    app.add_middleware(RequestContextMiddleware)
    app.add_exception_handler(APIError, api_error_handler)
    app.add_exception_handler(RequestValidationError, validation_error_handler)
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)
    app.add_exception_handler(ProviderError, provider_error_handler)
    app.add_exception_handler(SQLAlchemyError, database_error_handler)
    app.add_exception_handler(Exception, unhandled_error_handler)

    app.include_router(health.router, tags=["ops"])
    app.include_router(chat.router, prefix="/v1", tags=["chat"])
    app.include_router(models.router, prefix="/v1", tags=["models"])
    app.include_router(admin_router)
    app.include_router(evals_router)
    return app


app = create_app()


if __name__ == "__main__":
    uvicorn.run("app.main:app", host="127.0.0.1", port=8000, reload=False)
