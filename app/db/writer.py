"""Usage persistence off the request path.

Metering must not add latency to a completion: the response is done when the
client has it, not when the row is committed. Events go into a bounded queue
and a background task flushes them in batches.

If the queue fills up we drop the event and count it. That is the deliberate
trade: losing a metering row is bad, blocking every request behind the database
is worse.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass

from sqlalchemy import update
from sqlalchemy.exc import IntegrityError

from app.db.models import Quota, RequestLog
from app.db.session import Database
from app.logging import get_logger
from app.pricing import PriceBook
from app.quota import current_period

logger = get_logger(__name__)


@dataclass(slots=True)
class UsageEvent:
    request_id: str
    tenant: str
    route: str
    provider: str
    model: str
    stream: bool
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    latency_ms: float
    status: str
    tenant_id: int | None = None
    key_id: int | None = None
    ttft_ms: float | None = None
    cache_hit: bool = False
    saved_cost_usd: float = 0.0
    usage_estimated: bool = True


@dataclass(slots=True)
class _Barrier:
    future: asyncio.Future[None]


class UsageWriter:
    def __init__(
        self,
        database: Database,
        price_book: PriceBook,
        *,
        queue_size: int = 2000,
        flush_interval_s: float = 0.5,
    ) -> None:
        self._database = database
        self._price_book = price_book
        self._queue: asyncio.Queue[UsageEvent | _Barrier] = asyncio.Queue(maxsize=queue_size)
        self._flush_interval_s = flush_interval_s
        self._task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()
        self._closed = False
        self.dropped = 0
        self.persisted = 0

    @property
    def pending(self) -> int:
        return self._queue.qsize()

    def set_price_book(self, price_book: PriceBook) -> None:
        """Swap prices after an admin edit, without a restart."""
        self._price_book = price_book

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="usage-writer")

    async def stop(self) -> None:
        self._closed = True
        if self._task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        await self.flush()
        logger.info(
            "usage writer stopped",
            extra={
                "event": "usage_writer_stop",
                "persisted": self.persisted,
                "dropped": self.dropped,
            },
        )

    def submit(self, event: UsageEvent) -> bool:
        """Non-blocking enqueue. Returns False when the event was dropped."""
        if self._closed:
            self.dropped += 1
            return False
        try:
            self._queue.put_nowait(event)
            return True
        except asyncio.QueueFull:
            self.dropped += 1
            logger.warning(
                "usage queue full, dropping event",
                extra={"event": "usage_dropped", "request_id": event.request_id},
            )
            return False

    async def flush(self, timeout_s: float = 5.0) -> None:
        """Wait until everything submitted so far is committed.

        Read paths (admin usage) call this first so numbers are never stale by
        one flush interval.
        """
        if self._task is None or self._task.done():
            await self._drain_once()
            return
        barrier = _Barrier(asyncio.get_running_loop().create_future())
        await self._queue.put(barrier)
        await asyncio.wait_for(barrier.future, timeout_s)

    async def _run(self) -> None:
        while not (self._closed and self._queue.empty()):
            try:
                first = await asyncio.wait_for(self._queue.get(), timeout=self._flush_interval_s)
            except TimeoutError:
                continue
            batch: list[UsageEvent] = []
            barriers: list[_Barrier] = []
            self._classify(first, batch, barriers)
            while True:
                try:
                    item = self._queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                self._classify(item, batch, barriers)
            if batch:
                await self._persist(batch)
            for barrier in barriers:
                if not barrier.future.done():
                    barrier.future.set_result(None)

    async def _drain_once(self) -> None:
        batch: list[UsageEvent] = []
        while True:
            try:
                item = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            if isinstance(item, UsageEvent):
                batch.append(item)
        if batch:
            await self._persist(batch)

    @staticmethod
    def _classify(
        item: UsageEvent | _Barrier, batch: list[UsageEvent], barriers: list[_Barrier]
    ) -> None:
        if isinstance(item, UsageEvent):
            batch.append(item)
        else:
            barriers.append(item)

    async def _persist(self, batch: list[UsageEvent]) -> None:
        async with self._lock:
            period = current_period()
            counters: dict[tuple[int, str], list[float]] = {}
            records: list[RequestLog] = []
            for event in batch:
                # A cached answer costs us nothing upstream; recording the list
                # price here would double-count the saving.
                cost = (
                    0.0
                    if event.cache_hit
                    else self._price_book.cost_usd(
                        event.provider,
                        event.model,
                        event.prompt_tokens,
                        event.completion_tokens,
                    )
                )
                records.append(
                    RequestLog(
                        request_id=event.request_id,
                        tenant_id=event.tenant_id,
                        tenant=event.tenant,
                        key_id=event.key_id,
                        route=event.route,
                        provider=event.provider,
                        model=event.model,
                        stream=event.stream,
                        prompt_tokens=event.prompt_tokens,
                        completion_tokens=event.completion_tokens,
                        total_tokens=event.total_tokens,
                        latency_ms=event.latency_ms,
                        ttft_ms=event.ttft_ms,
                        status=event.status,
                        cache_hit=event.cache_hit,
                        saved_cost_usd=event.saved_cost_usd,
                        usage_estimated=event.usage_estimated,
                        cost_usd=cost,
                    )
                )
                if event.tenant_id is not None:
                    delta = counters.setdefault((event.tenant_id, period), [0, 0.0])
                    delta[0] += event.total_tokens
                    delta[1] += cost

            try:
                async with self._database.session() as session:
                    session.add_all(records)
                    for (tenant_id, period_key), (tokens, cost) in counters.items():
                        await self._bump_quota(session, tenant_id, period_key, tokens, cost)
                    await session.commit()
            except Exception:
                logger.exception(
                    "usage persist failed",
                    extra={"event": "usage_persist_failed", "events": len(batch)},
                )
                return

            self.persisted += len(batch)

    @staticmethod
    async def _bump_quota(session, tenant_id: int, period: str, tokens: int, cost: float) -> None:
        result = await session.execute(
            update(Quota)
            .where(Quota.tenant_id == tenant_id, Quota.period == period)
            .values(
                tokens_used=Quota.tokens_used + tokens,
                cost_used_usd=Quota.cost_used_usd + cost,
            )
        )
        if result.rowcount:
            return
        session.add(
            Quota(
                tenant_id=tenant_id,
                period=period,
                tokens_used=tokens,
                cost_used_usd=cost,
            )
        )
        try:
            await session.flush()
        except IntegrityError:
            # The row appeared between UPDATE and INSERT (another writer).
            await session.rollback()
            await session.execute(
                update(Quota)
                .where(Quota.tenant_id == tenant_id, Quota.period == period)
                .values(
                    tokens_used=Quota.tokens_used + tokens,
                    cost_used_usd=Quota.cost_used_usd + cost,
                )
            )
