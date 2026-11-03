"""Metering schema: who used what, what it cost, and how much is left.

Money is stored in USD with float precision. For a portfolio gateway that is
fine (sub-cent precision, no ledger requirement); a real billing system would
use integer micro-dollars plus an append-only ledger. Noted in the README so
the tradeoff is explicit rather than accidental.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


def utcnow() -> datetime:
    return datetime.now(UTC)


class Tenant(Base):
    __tablename__ = "tenants"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    slug: Mapped[str] = mapped_column(String(64), unique=True)
    name: Mapped[str] = mapped_column(String(128), default="")
    monthly_budget_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    keys: Mapped[list[ApiKey]] = relationship(back_populates="tenant", cascade="all, delete-orphan")


class ApiKey(Base):
    __tablename__ = "api_keys"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(64), default="")
    # Only the digest is stored; the raw key is shown exactly once at creation.
    key_hash: Mapped[str] = mapped_column(String(64), unique=True)
    key_prefix: Mapped[str] = mapped_column(String(32), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    tenant: Mapped[Tenant] = relationship(back_populates="keys")


class ModelPrice(Base):
    """USD per 1M tokens. The DB overrides the built-in price book."""

    __tablename__ = "models"
    __table_args__ = (UniqueConstraint("provider", "model"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    provider: Mapped[str] = mapped_column(String(32))
    model: Mapped[str] = mapped_column(String(96))
    input_usd_per_1m: Mapped[float] = mapped_column(Float, default=0.0)
    output_usd_per_1m: Mapped[float] = mapped_column(Float, default=0.0)


class RequestLog(Base):
    __tablename__ = "requests"
    __table_args__ = (Index("ix_requests_tenant_created", "tenant", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    request_id: Mapped[str] = mapped_column(String(64), unique=True)
    tenant_id: Mapped[int | None] = mapped_column(
        ForeignKey("tenants.id", ondelete="SET NULL"), nullable=True, index=True
    )
    # Denormalised slug: aggregation keeps working after a tenant is deleted,
    # and dev-mode requests (no tenant row) still group correctly.
    tenant: Mapped[str] = mapped_column(String(64), index=True)
    key_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    route: Mapped[str] = mapped_column(String(64), default="")
    provider: Mapped[str] = mapped_column(String(32))
    model: Mapped[str] = mapped_column(String(96))
    stream: Mapped[bool] = mapped_column(Boolean, default=False)
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0)
    total_tokens: Mapped[int] = mapped_column(Integer, default=0)
    latency_ms: Mapped[float] = mapped_column(Float, default=0.0)
    ttft_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="ok")
    cache_hit: Mapped[bool] = mapped_column(Boolean, default=False)
    # What the call would have cost upstream. Zero on a miss; on a hit it is the
    # number the "we saved X%" claim is computed from, so it lives next to cost.
    saved_cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    usage_estimated: Mapped[bool] = mapped_column(Boolean, default=True)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )


class Quota(Base):
    """Monthly counters. Limits are optional; usage always accumulates."""

    __tablename__ = "quotas"
    __table_args__ = (UniqueConstraint("tenant_id", "period"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), index=True)
    period: Mapped[str] = mapped_column(String(7))  # YYYY-MM, UTC
    tokens_used: Mapped[int] = mapped_column(Integer, default=0)
    cost_used_usd: Mapped[float] = mapped_column(Float, default=0.0)
    tokens_limit: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cost_limit_usd: Mapped[float | None] = mapped_column(Float, nullable=True)


class EvalRun(Base):
    """One scoring pass of one strategy over one dataset.

    ``strategy`` is stored as JSON rather than normalised columns on purpose:
    eval configs grow (cache flags, routing tables, prompt versions) and old
    runs must stay readable after the schema moves on.
    """

    __tablename__ = "eval_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128), index=True)
    dataset: Mapped[str] = mapped_column(String(255))
    strategy: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(16), default="running")
    passed: Mapped[int] = mapped_column(Integer, default=0)
    total: Mapped[int] = mapped_column(Integer, default=0)
    pass_rate: Mapped[float] = mapped_column(Float, default=0.0)
    total_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cases: Mapped[list[EvalCaseResult]] = relationship(
        back_populates="run", cascade="all, delete-orphan"
    )


class EvalCaseResult(Base):
    __tablename__ = "eval_case_results"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("eval_runs.id", ondelete="CASCADE"), index=True)
    case_id: Mapped[str] = mapped_column(String(128))
    passed: Mapped[bool] = mapped_column(Boolean, default=False)
    output: Mapped[str] = mapped_column(Text, default="")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    latency_ms: Mapped[float] = mapped_column(Float, default=0.0)
    total_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    run: Mapped[EvalRun] = relationship(back_populates="cases")
