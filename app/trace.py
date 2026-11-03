"""Per-call trace record.

For now it is written to the JSON log stream. M3 replaces the TODO with an
insert into ``requests`` plus a pricing table, without changing callers.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

from app.logging import get_logger

logger = get_logger("app.trace")


@dataclass(slots=True)
class TraceRecord:
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
    ttft_ms: float | None = None
    status: str = "ok"
    cache_hit: bool = False
    usage_estimated: bool = True


def record_trace(record: TraceRecord) -> None:
    payload = asdict(record)
    logger.info("llm call", extra={"event": "llm_call", **payload})
    # TODO(M3): persist payload to the `requests` table and compute cost_usd
    # from the `models` pricing table. Keep the event name stable: dashboards
    # and tests key off it.
