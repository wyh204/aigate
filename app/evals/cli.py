"""``python -m app.evals``: run and gate evaluations from a terminal.

The CLI is the honest interface for this subsystem: a scoring run is 3-10
minutes of real money and real latency, not something to hide behind a web
button. The admin endpoints reuse the same functions for demo purposes.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from app.config import build_settings
from app.db.session import Database
from app.evals.dataset import DatasetError, load_dataset
from app.evals.gate import evaluate_gate
from app.evals.runner import RunOptions, load_result, run_dataset
from app.evals.schema import RunResult, Strategy
from app.pricing import PriceBook
from app.provider.registry import ProviderRegistry
from app.router.simple import Router


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.evals",
        description="Offline evaluation runs and release gates.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="score a dataset with one strategy")
    run.add_argument("--dataset", required=True, help="JSONL dataset path")
    run.add_argument("--name", default=None, help="run name (default: dataset stem)")
    run.add_argument("--route", default="default", help="route alias or provider:model")
    run.add_argument("--temperature", type=float, default=0.0)
    run.add_argument("--max-tokens", type=int, default=None)
    run.add_argument("--judge-route", default="default", help="route used for llm_judge cases")
    run.add_argument("--concurrency", type=int, default=4)
    run.add_argument("--limit", type=int, default=None)
    run.add_argument("--tags", default="", help="comma separated tag filter")
    run.add_argument("--save", default=None, help="result JSON path")
    run.add_argument("--db", default=None, help="SQLAlchemy URL to persist the run")

    compare = sub.add_parser("compare", help="gate a candidate run against a baseline run")
    compare.add_argument("baseline", help="baseline result JSON")
    compare.add_argument("candidate", help="candidate result JSON")
    compare.add_argument("--max-pass-rate-drop", type=float, default=0.02)
    compare.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "run":
        return _cmd_run(args)
    return _cmd_compare(args)


def _cmd_run(args: argparse.Namespace) -> int:
    # Validate early: a typo in the dataset path should not require building the
    # whole provider stack first.
    try:
        load_dataset(args.dataset)
    except DatasetError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    strategy = Strategy(
        name=args.name or Path(args.dataset).stem,
        route=args.route,
        temperature=args.temperature,
        max_tokens=args.max_tokens,
        judge_route=args.judge_route,
        concurrency=args.concurrency,
    )
    save_path = Path(args.save) if args.save else Path("eval_runs") / f"{strategy.name}.json"
    options = RunOptions(
        dataset=Path(args.dataset),
        strategy=strategy,
        limit=args.limit,
        tags=tuple(part.strip() for part in args.tags.split(",") if part.strip()),
        save_path=save_path,
        db=args.db,
    )
    try:
        result = asyncio.run(_run(options))
    except Exception as exc:  # noqa: BLE001 - CLI reports, never tracebacks at users
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    _print_result(result, save_path)
    return 0


async def _run(options: RunOptions) -> RunResult:
    settings = build_settings()
    database = Database(options.db) if options.db else None
    registry = ProviderRegistry.from_settings(settings)
    try:
        if database is not None:
            await database.create_all()
            price_book = await PriceBook.load(database)
        else:
            price_book = PriceBook.builtin()
        cases = load_dataset(options.dataset)
        return await run_dataset(
            options,
            cases,
            registry=registry,
            router=Router(settings.routes, registry),
            price_book=price_book,
            database=database,
        )
    finally:
        await registry.aclose()
        if database is not None:
            await database.dispose()


def _print_result(result: RunResult, save_path: Path) -> None:
    failures = [case for case in result.cases if not case.passed]
    avg_latency = sum(case.latency_ms for case in result.cases) / max(1, result.total)
    print(f"strategy   {result.name}  route={result.strategy.get('route')}")
    print(f"dataset    {result.dataset}")
    print(
        f"cases      {result.passed}/{result.total} passed   "
        f"pass_rate {result.pass_rate * 100:.1f}%"
    )
    print(f"tokens     {result.total_tokens}")
    print(f"cost       ${result.cost_usd:.6f}")
    print(f"latency    avg {avg_latency:.1f}ms")
    if failures:
        print("failures")
        for case in failures:
            print(f"  FAIL {case.case_id}: {(case.error or '')[:120]}")
    print(f"saved      {save_path}")


def _cmd_compare(args: argparse.Namespace) -> int:
    try:
        baseline = load_result(args.baseline)
        candidate = load_result(args.candidate)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"error: cannot read run file: {exc}", file=sys.stderr)
        return 1

    report = evaluate_gate(baseline, candidate, max_pass_rate_drop=args.max_pass_rate_drop)
    if args.json:
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    else:
        print(
            f"baseline   pass {float(baseline.get('pass_rate') or 0) * 100:.1f}%  "
            f"cost ${float(baseline.get('cost_usd') or 0):.6f}"
        )
        print(
            f"candidate  pass {float(candidate.get('pass_rate') or 0) * 100:.1f}%  "
            f"cost ${float(candidate.get('cost_usd') or 0):.6f}"
        )
        print(
            f"pass drop  {report.pass_delta * 100:.1f}pp  "
            f"(budget {report.max_pass_rate_drop * 100:.1f}pp)"
        )
        if report.cost_delta_pct is not None:
            print(f"cost       {report.cost_delta_pct:+.1f}%")
        for warning in report.warnings:
            print(f"warning    {warning}")
        for reason in report.reasons:
            print(f"reason     {reason}")
        print(f"decision   {report.decision}")
    return 0 if report.passed else 2
