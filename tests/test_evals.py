"""Evaluation subsystem tests: loader, graders, runner, gate, admin surface."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import ProviderConfig, Settings
from app.evals.dataset import DatasetError, load_dataset
from app.evals.gate import evaluate_gate
from app.evals.graders import grade, parse_judge_reply
from app.evals.runner import RunOptions, run_dataset
from app.evals.schema import EvalCase, Grading, Strategy
from app.main import create_app
from app.pricing import PriceBook
from app.provider.registry import ProviderRegistry
from app.router.simple import Router

ADMIN_TOKEN = "admin-test-token"


def _settings(tmp_path: Path, **overrides) -> Settings:
    base = Settings(
        providers=(ProviderConfig(name="mock", type="mock"),),
        routes={"mock": ("mock:mock-echo",), "default": ("mock:mock-echo",)},
        database_url=f"sqlite+aiosqlite:///{(tmp_path / 'aigate-test.db').as_posix()}",
        admin_token=ADMIN_TOKEN,
        usage_flush_interval_s=0.05,
        eval_data_dir=str(tmp_path / "evals"),
    )
    return replace(base, **overrides)


def _write_dataset(path: Path, cases: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(case) for case in cases) + "\n", encoding="utf-8")
    return path


def _case(kind: str, expected: str = "", **grading: object) -> EvalCase:
    return EvalCase(id="c", input="q", expected=expected, grading=Grading(kind=kind, **grading))


# --------------------------------------------------------------------------- loader


def test_load_dataset_skips_comments_and_blank_lines(tmp_path: Path) -> None:
    path = tmp_path / "cases.jsonl"
    path.write_text(
        '# a comment\n\n{"id": "a", "input": "hi", "expected": "hi", "grading": "exact"}\n',
        encoding="utf-8",
    )
    cases = load_dataset(path)
    assert [case.id for case in cases] == ["a"]
    assert cases[0].grading.kind == "exact"


def test_load_dataset_reports_line_number_for_bad_json(tmp_path: Path) -> None:
    path = tmp_path / "cases.jsonl"
    path.write_text('{"id": "a", "input": "hi"}\n{not json}\n', encoding="utf-8")
    with pytest.raises(DatasetError, match=r"cases.jsonl:2: invalid JSON"):
        load_dataset(path)


def test_load_dataset_rejects_duplicate_ids(tmp_path: Path) -> None:
    path = _write_dataset(
        tmp_path / "cases.jsonl",
        [{"id": "a", "input": "x"}, {"id": "a", "input": "y"}],
    )
    with pytest.raises(DatasetError, match="duplicate id 'a'"):
        load_dataset(path)


def test_load_dataset_rejects_unknown_fields(tmp_path: Path) -> None:
    path = _write_dataset(tmp_path / "cases.jsonl", [{"id": "a", "input": "x", "expect": "typo"}])
    with pytest.raises(DatasetError, match="unknown field"):
        load_dataset(path)


# --------------------------------------------------------------------------- graders


def test_contains_is_case_and_whitespace_insensitive() -> None:
    passed, reason = asyncio.run(grade(_case("contains", "paris"), "  The PARIS metro."))
    assert passed and reason == ""


def test_numeric_accepts_numbers_inside_explanations() -> None:
    passed, _ = asyncio.run(grade(_case("numeric", "391"), "17 * 23 = 391."))
    assert passed
    failed, reason = asyncio.run(grade(_case("numeric", "391"), "The answer is 392"))
    assert not failed and "391" in reason


def test_regex_grader_uses_pattern() -> None:
    passed, _ = asyncio.run(grade(_case("regex", pattern=r"\b200\b"), "status 200 OK"))
    assert passed


def test_llm_judge_uses_injected_judge() -> None:
    async def judge(case: EvalCase, output: str) -> bool:
        return output.strip() == "yes"

    passed, reason = asyncio.run(
        grade(_case("llm_judge", "yes", rubric="say yes"), "  yes  ", judge=judge)
    )
    assert passed and reason == "judge"


def test_llm_judge_without_judge_fails_loudly() -> None:
    passed, reason = asyncio.run(grade(_case("llm_judge", "x"), "x"))
    assert not passed and "no judge configured" in reason


def test_judge_reply_parser() -> None:
    assert parse_judge_reply("PASS") is True
    assert parse_judge_reply("pass - looks right") is True
    assert parse_judge_reply("FAIL") is False
    assert parse_judge_reply("") is False


# --------------------------------------------------------------------------- runner


def _mock_stack() -> tuple[ProviderRegistry, Router]:
    settings = Settings(
        providers=(ProviderConfig(name="mock", type="mock"),),
        routes={"default": ("mock:mock-echo",)},
    )
    registry = ProviderRegistry.from_settings(settings)
    return registry, Router(settings.routes, registry)


def test_run_smoke_dataset_passes_with_mock(tmp_path: Path) -> None:
    cases = load_dataset(Path("data/eval_smoke.jsonl"))
    registry, router = _mock_stack()
    options = RunOptions(
        dataset=Path("data/eval_smoke.jsonl"),
        strategy=Strategy(name="smoke", concurrency=2),
    )
    result = asyncio.run(
        run_dataset(
            options,
            cases,
            registry=registry,
            router=router,
            price_book=PriceBook.builtin(),
        )
    )
    assert result.total == 6
    assert result.passed == 6
    assert result.pass_rate == 1.0
    assert result.cost_usd == 0.0
    assert all(case.output.startswith("[mock:mock-echo]") for case in result.cases)


def test_one_failing_case_does_not_kill_the_run(tmp_path: Path) -> None:
    dataset = _write_dataset(
        tmp_path / "mixed.jsonl",
        [
            {"id": "ok", "input": "hello", "expected": "hello", "grading": "contains"},
            {"id": "bad", "input": "hello", "expected": "goodbye", "grading": "contains"},
        ],
    )
    cases = load_dataset(dataset)
    registry, router = _mock_stack()
    options = RunOptions(dataset=dataset, strategy=Strategy(name="mixed"))
    result = asyncio.run(
        run_dataset(
            options,
            cases,
            registry=registry,
            router=router,
            price_book=PriceBook.builtin(),
        )
    )
    assert result.total == 2
    assert result.passed == 1
    by_id = {case.case_id: case for case in result.cases}
    assert by_id["ok"].passed
    assert not by_id["bad"].passed
    assert "goodbye" in (by_id["bad"].error or "")


# --------------------------------------------------------------------------- gate


def _run(pass_rate: float, cost: float, total: int = 10, name: str = "run") -> dict:
    return {
        "name": name,
        "total": total,
        "passed": round(pass_rate * total),
        "pass_rate": pass_rate,
        "cost_usd": cost,
    }


def test_gate_allows_small_quality_loss_with_big_savings() -> None:
    report = evaluate_gate(_run(0.95, 0.040), _run(0.94, 0.020), max_pass_rate_drop=0.02)
    assert report.passed
    assert report.cost_delta_pct == -50.0


def test_gate_blocks_quality_loss_beyond_budget() -> None:
    report = evaluate_gate(_run(0.95, 0.040), _run(0.92, 0.020), max_pass_rate_drop=0.02)
    assert not report.passed
    assert any("dropped 3.0pp" in reason for reason in report.reasons)


def test_gate_blocks_incomparable_datasets() -> None:
    report = evaluate_gate(_run(0.95, 0.0, total=20), _run(0.95, 0.0, total=10))
    assert not report.passed
    assert any("not comparable" in reason for reason in report.reasons)


def test_gate_warns_when_cost_goes_up() -> None:
    report = evaluate_gate(_run(0.95, 0.020), _run(0.96, 0.030))
    assert report.passed
    assert report.warnings and "cost increased" in report.warnings[0]


# --------------------------------------------------------------------------- admin


def _eval_client(tmp_path: Path) -> TestClient:
    data_dir = tmp_path / "evals"
    _write_dataset(
        data_dir / "tiny.jsonl",
        [
            {"id": "a", "input": "alpha", "expected": "alpha", "grading": "contains"},
            {"id": "b", "input": "beta", "expected": "beta", "grading": "contains"},
        ],
    )
    return TestClient(create_app(_settings(tmp_path)))


def test_admin_run_persists_and_lists(tmp_path: Path) -> None:
    with _eval_client(tmp_path) as client:
        headers = {"x-admin-token": ADMIN_TOKEN}
        response = client.post(
            "/admin/evals/run",
            json={"dataset": "tiny.jsonl", "name": "tiny"},
            headers=headers,
        )
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["passed"] == 2 and payload["pass_rate"] == 1.0
        run_id = payload["run_id"]
        assert run_id is not None

        runs = client.get("/admin/evals/runs", headers=headers).json()["data"]
        assert runs[0]["run_id"] == run_id

        detail = client.get(f"/admin/evals/runs/{run_id}", headers=headers).json()
        assert len(detail["cases"]) == 2
        assert all(case["passed"] for case in detail["cases"])

        datasets = client.get("/admin/evals/datasets", headers=headers).json()["data"]
        assert any(entry["name"] == "tiny.jsonl" and entry["cases"] == 2 for entry in datasets)


def test_admin_eval_requires_admin_token(tmp_path: Path) -> None:
    with _eval_client(tmp_path) as client:
        response = client.get("/admin/evals/runs")
        assert response.status_code == 401


def test_admin_compare_two_runs(tmp_path: Path) -> None:
    with _eval_client(tmp_path) as client:
        headers = {"x-admin-token": ADMIN_TOKEN}
        first = client.post(
            "/admin/evals/run", json={"dataset": "tiny.jsonl"}, headers=headers
        ).json()
        second = client.post(
            "/admin/evals/run", json={"dataset": "tiny.jsonl"}, headers=headers
        ).json()
        report = client.post(
            "/admin/evals/compare",
            json={"baseline_run": first["run_id"], "candidate_run": second["run_id"]},
            headers=headers,
        ).json()
        assert report["decision"] == "ALLOW"
        assert report["pass_rate_drop"] == 0.0
