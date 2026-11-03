"""Run a dataset through the router and score it.

Three properties matter for the numbers to be trustworthy:

* the run goes through :class:`Router` and :class:`Provider`, the same objects
  a live request uses, so a strategy that looks good here can actually ship;
* every case is isolated: a provider error fails one case with its own message
  instead of aborting the run, so "80% pass" is never a rounding of "one call
  blew up";
* results are persisted with the exact strategy that produced them, otherwise
  a pass-rate number cannot be reproduced later.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.db.models import EvalCaseResult, EvalRun
from app.db.session import Database
from app.evals.graders import Judge, grade, judge_prompt, parse_judge_reply
from app.evals.schema import CaseResult, EvalCase, RunResult, Strategy
from app.logging import get_logger
from app.pricing import PriceBook
from app.protocol.openai import ChatCompletion, ChatCompletionRequest, ChatMessage, Usage
from app.provider.base import ProviderError
from app.provider.registry import ProviderRegistry
from app.router.simple import Route, Router, RouteTarget

logger = get_logger(__name__)


@dataclass
class RunOptions:
    dataset: Path = Path("data/eval_smoke.jsonl")
    strategy: Strategy = field(default_factory=Strategy)
    limit: int | None = None
    tags: tuple[str, ...] = ()
    save_path: Path | None = None
    db: str | None = None


async def run_dataset(
    options: RunOptions,
    cases: list[EvalCase],
    *,
    registry: ProviderRegistry,
    router: Router,
    price_book: PriceBook,
    database: Database | None = None,
) -> RunResult:
    started_at = datetime.now(UTC).isoformat(timespec="seconds")
    name = options.strategy.name
    run_id = await _open_run(database, name, str(options.dataset), options.strategy, started_at)

    selected = _select(cases, options)
    if not selected:
        raise ValueError("no cases selected; check --limit/--tags against the dataset")

    semaphore = asyncio.Semaphore(max(1, options.strategy.concurrency))

    async def run_one(case: EvalCase) -> CaseResult:
        async with semaphore:
            return await _run_case(case, registry, router, price_book, options.strategy)

    results = await asyncio.gather(*(run_one(case) for case in selected))
    finished_at = datetime.now(UTC).isoformat(timespec="seconds")

    result = RunResult(
        name=name,
        dataset=str(options.dataset),
        strategy=options.strategy.to_dict(),
        cases=list(results),
        started_at=started_at,
        finished_at=finished_at,
        run_id=run_id,
    )
    await _finish_run(database, run_id, result)
    if options.save_path is not None:
        save_result(result, options.save_path)
    logger.info(
        "eval run finished",
        extra={
            "event": "eval_run",
            "run_id": run_id,
            "run_name": name,
            "pass_rate": result.pass_rate,
            "cost_usd": result.cost_usd,
            "cases": result.total,
        },
    )
    return result


def _select(cases: list[EvalCase], options: RunOptions) -> list[EvalCase]:
    selected = cases
    if options.tags:
        wanted = set(options.tags)
        selected = [case for case in selected if wanted & set(case.tags)]
    if options.limit is not None:
        selected = selected[: options.limit]
    return selected


async def _run_case(
    case: EvalCase,
    registry: ProviderRegistry,
    router: Router,
    price_book: PriceBook,
    strategy: Strategy,
) -> CaseResult:
    started = time.perf_counter()
    try:
        completion, target = await _ask(case, registry, router, strategy)
    except Exception as exc:  # noqa: BLE001 - one bad case must not kill the run
        latency_ms = (time.perf_counter() - started) * 1000
        logger.warning(
            "eval case failed",
            extra={"event": "eval_case_error", "case_id": case.id, "error": str(exc)},
        )
        return CaseResult(
            case_id=case.id,
            passed=False,
            error=f"{type(exc).__name__}: {exc}",
            latency_ms=latency_ms,
        )

    latency_ms = (time.perf_counter() - started) * 1000
    output = completion.choices[0].message.content if completion.choices else ""
    output = output if isinstance(output, str) else ""
    usage = completion.usage or Usage()
    passed, reason = await grade(case, output, judge=_judge(registry, router, strategy))

    return CaseResult(
        case_id=case.id,
        passed=passed,
        output=output,
        error=None if passed else reason,
        latency_ms=latency_ms,
        prompt_tokens=usage.prompt_tokens,
        completion_tokens=usage.completion_tokens,
        total_tokens=usage.total_tokens,
        cost_usd=price_book.cost_usd(
            target.provider, target.model, usage.prompt_tokens, usage.completion_tokens
        ),
    )


async def _ask(
    case: EvalCase, registry: ProviderRegistry, router: Router, strategy: Strategy
) -> tuple[ChatCompletion, RouteTarget]:
    route: Route = router.resolve(strategy.route)
    messages: list[ChatMessage] = []
    system = case.system or strategy.system_prompt
    if system:
        messages.append(ChatMessage(role="system", content=system))
    messages.append(ChatMessage(role="user", content=case.input))
    max_tokens = case.max_tokens or strategy.max_tokens

    last_error: ProviderError | None = None
    for target in route.chain:
        provider = registry.get(target.provider)
        payload = ChatCompletionRequest(
            model=target.model,
            messages=messages,
            temperature=strategy.temperature,
            max_tokens=max_tokens,
        )
        try:
            return await provider.chat(target.model, payload), target
        except ProviderError as exc:
            last_error = exc
            if not exc.retryable:
                raise
            logger.warning(
                "eval provider failed, trying next in chain",
                extra={"event": "eval_fallback", "provider": target.provider, "error": str(exc)},
            )
    raise last_error or ProviderError("route chain is empty")


def _judge(registry: ProviderRegistry, router: Router, strategy: Strategy) -> Judge | None:
    """Return an async judge callable, used only for llm_judge cases."""

    async def judge(case: EvalCase, output: str) -> bool:
        route = router.resolve(strategy.judge_route)
        prompt = judge_prompt(case, output)
        last_error: ProviderError | None = None
        for target in route.chain:
            provider = registry.get(target.provider)
            try:
                completion = await provider.chat(
                    target.model,
                    ChatCompletionRequest(
                        model=target.model,
                        messages=[ChatMessage(role="user", content=prompt)],
                        temperature=0.0,
                    ),
                )
            except ProviderError as exc:
                last_error = exc
                if not exc.retryable:
                    raise
                continue
            text = completion.choices[0].message.content if completion.choices else ""
            return parse_judge_reply(text if isinstance(text, str) else "")
        raise last_error or ProviderError("judge route chain is empty")

    return judge


def _now() -> datetime:
    return datetime.now(UTC)


async def _open_run(
    database: Database | None,
    name: str,
    dataset: str,
    strategy: Strategy,
    started_at: str,
) -> int | None:
    if database is None:
        return None
    try:
        async with database.session() as session:
            run = EvalRun(
                name=name,
                dataset=dataset,
                strategy=strategy.to_dict(),
                status="running",
                started_at=datetime.fromisoformat(started_at),
            )
            session.add(run)
            await session.commit()
            await session.refresh(run)
            return run.id
    except Exception as exc:  # noqa: BLE001 - scoring must work without a DB
        logger.warning(
            "could not open eval run row", extra={"event": "eval_db_error", "error": str(exc)}
        )
        return None


async def _finish_run(database: Database | None, run_id: int | None, result: RunResult) -> None:
    if database is None or run_id is None:
        return
    try:
        async with database.session() as session:
            run = await session.get(EvalRun, run_id)
            if run is None:
                return
            run.status = "done"
            run.passed = result.passed
            run.total = result.total
            run.pass_rate = result.pass_rate
            run.total_tokens = result.total_tokens
            run.cost_usd = result.cost_usd
            run.finished_at = _now()
            session.add_all(
                EvalCaseResult(
                    run_id=run_id,
                    case_id=case.case_id,
                    passed=case.passed,
                    output=(case.output or "")[:4000],
                    error=case.error,
                    latency_ms=case.latency_ms,
                    total_tokens=case.total_tokens,
                    cost_usd=case.cost_usd,
                )
                for case in result.cases
            )
            await session.commit()
    except Exception as exc:  # noqa: BLE001 - never lose the in-memory result to a DB hiccup
        logger.warning(
            "could not persist eval run",
            extra={"event": "eval_db_error", "run_id": run_id, "error": str(exc)},
        )


def save_result(result: RunResult, path: str | Path) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(result.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    return target


def load_result(path: str | Path) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))
