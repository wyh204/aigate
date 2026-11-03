"""Admin surface for evaluations, sharing the CLI's implementation.

These endpoints exist so ``/docs`` can be used for a demo. The CLI remains the
primary interface: an eval run costs real tokens and should be a deliberate
action, not a button someone clicks on an empty database.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel
from sqlalchemy import select

from app.admin.router import require_admin
from app.db.models import EvalCaseResult, EvalRun
from app.errors import APIError
from app.evals.dataset import DatasetError, load_dataset
from app.evals.gate import evaluate_gate
from app.evals.runner import RunOptions, run_dataset
from app.evals.schema import Strategy
from app.pricing import PriceBook

router = APIRouter(prefix="/admin/evals", tags=["evals"], dependencies=[Depends(require_admin)])


class RunRequest(BaseModel):
    dataset: str = "eval_smoke.jsonl"
    name: str | None = None
    route: str = "default"
    temperature: float = 0.0
    concurrency: int = 4
    limit: int | None = None
    tags: str = ""


class CompareRequest(BaseModel):
    baseline_run: int
    candidate_run: int
    max_pass_rate_drop: float = 0.02


def _dataset_path(request: Request, name: str) -> Path:
    settings = request.app.state.settings
    base = Path(settings.eval_data_dir)
    path = Path(name)
    if not path.is_absolute():
        path = base / path
    if not path.exists():
        raise APIError(
            404,
            f"dataset {name!r} not found under {base}",
            code="dataset_not_found",
        )
    return path


def _summary(run: EvalRun) -> dict[str, Any]:
    return {
        "run_id": run.id,
        "name": run.name,
        "dataset": run.dataset,
        "status": run.status,
        "passed": run.passed,
        "total": run.total,
        "pass_rate": run.pass_rate,
        "total_tokens": run.total_tokens,
        "cost_usd": run.cost_usd,
        "strategy": run.strategy,
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
    }


def _database(request: Request):  # noqa: ANN201 - Database, imported lazily to avoid cycles
    return getattr(request.app.state, "db", None)


@router.post("/run")
async def run_now(body: RunRequest, request: Request) -> dict[str, Any]:
    state = request.app.state
    path = _dataset_path(request, body.dataset)
    try:
        cases = load_dataset(path)
    except DatasetError as exc:
        raise APIError(400, str(exc), code="invalid_dataset") from exc

    strategy = Strategy(
        name=body.name or path.stem,
        route=body.route,
        temperature=body.temperature,
        concurrency=body.concurrency,
    )
    options = RunOptions(
        dataset=path,
        strategy=strategy,
        limit=body.limit,
        tags=tuple(part.strip() for part in body.tags.split(",") if part.strip()),
    )
    database = _database(request)
    price_book = await PriceBook.load(database) if database is not None else PriceBook.builtin()
    try:
        result = await run_dataset(
            options,
            cases,
            registry=state.registry,
            router=state.router,
            price_book=price_book,
            database=database,
        )
    except ValueError as exc:
        raise APIError(400, str(exc), code="invalid_eval") from exc

    payload = result.to_dict()
    # Keep the response readable; the full detail is in /runs/{id}.
    payload["cases"] = payload["cases"][:50]
    return payload


@router.get("/runs")
async def list_runs(request: Request, limit: int = 20) -> dict[str, Any]:
    database = _database(request)
    if database is None:
        return {"data": []}
    async with database.session() as session:
        runs = (
            await session.scalars(select(EvalRun).order_by(EvalRun.id.desc()).limit(limit))
        ).all()
        return {"data": [_summary(run) for run in runs]}


@router.get("/runs/{run_id}")
async def get_run(run_id: int, request: Request) -> dict[str, Any]:
    database = _database(request)
    if database is None:
        raise APIError(503, "database is not configured", code="database_disabled")
    async with database.session() as session:
        run = await session.get(EvalRun, run_id)
        if run is None:
            raise APIError(404, f"eval run {run_id} not found", code="run_not_found")
        cases = (
            await session.scalars(
                select(EvalCaseResult)
                .where(EvalCaseResult.run_id == run_id)
                .order_by(EvalCaseResult.case_id)
            )
        ).all()
        return {
            **_summary(run),
            "cases": [
                {
                    "case_id": case.case_id,
                    "passed": case.passed,
                    "output": case.output,
                    "error": case.error,
                    "latency_ms": round(case.latency_ms, 2),
                    "total_tokens": case.total_tokens,
                    "cost_usd": case.cost_usd,
                }
                for case in cases
            ],
        }


@router.post("/compare")
async def compare(body: CompareRequest, request: Request) -> dict[str, Any]:
    database = _database(request)
    if database is None:
        raise APIError(503, "database is not configured", code="database_disabled")
    async with database.session() as session:
        baseline = await session.get(EvalRun, body.baseline_run)
        candidate = await session.get(EvalRun, body.candidate_run)
    missing = [
        str(run_id)
        for run_id, run in ((body.baseline_run, baseline), (body.candidate_run, candidate))
        if run is None
    ]
    if missing:
        raise APIError(404, f"eval run(s) not found: {', '.join(missing)}", code="run_not_found")

    report = evaluate_gate(
        _summary(baseline),
        _summary(candidate),
        max_pass_rate_drop=body.max_pass_rate_drop,
    )
    return report.to_dict()


@router.get("/datasets")
async def list_datasets(request: Request) -> dict[str, Any]:
    base = Path(request.app.state.settings.eval_data_dir)
    data: list[dict[str, Any]] = []
    for path in sorted(base.glob("*.jsonl")) if base.exists() else []:
        entry: dict[str, Any] = {"name": path.name, "path": str(path)}
        try:
            entry["cases"] = len(load_dataset(path))
        except DatasetError as exc:
            entry["cases"] = None
            entry["error"] = str(exc)
        data.append(entry)
    return {"data": data}
