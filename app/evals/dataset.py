"""JSONL dataset loading with line-numbered validation errors.

One case per line, comments and blank lines allowed:

    {"id": "math-1", "input": "What is 17*23?", "expected": "391", "grading": "numeric"}

Keeping this a loader rather than a class hierarchy is deliberate: datasets are
data owned by whoever writes prompts, and a bad line must fail loudly with the
line number instead of silently shrinking the benchmark.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.evals.schema import EvalCase, Grading

_CASE_FIELDS = {"id", "input", "expected", "grading", "tags", "system", "max_tokens"}


class DatasetError(ValueError):
    """Invalid dataset file; the message always names the line."""


def load_dataset(path: str | Path) -> list[EvalCase]:
    dataset = Path(path)
    if not dataset.exists():
        raise DatasetError(f"dataset not found: {dataset}")
    cases: list[EvalCase] = []
    seen: dict[str, int] = {}
    for lineno, raw in enumerate(dataset.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as exc:
            raise DatasetError(f"{dataset.name}:{lineno}: invalid JSON ({exc.msg})") from exc
        try:
            case = _parse_case(payload)
        except ValueError as exc:
            raise DatasetError(f"{dataset.name}:{lineno}: {exc}") from exc
        if case.id in seen:
            raise DatasetError(
                f"{dataset.name}:{lineno}: duplicate id {case.id!r} "
                f"(first seen on line {seen[case.id]})"
            )
        seen[case.id] = lineno
        cases.append(case)
    if not cases:
        raise DatasetError(f"dataset {dataset} has no cases")
    return cases


def _parse_case(payload: Any) -> EvalCase:
    if not isinstance(payload, dict):
        raise ValueError("case must be a JSON object")
    unknown = set(payload) - _CASE_FIELDS
    if unknown:
        raise ValueError(f"unknown field(s): {', '.join(sorted(unknown))}")

    case_id = payload.get("id")
    if not isinstance(case_id, str) or not case_id.strip():
        raise ValueError("'id' must be a non-empty string")
    prompt = payload.get("input")
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("'input' must be a non-empty string")
    expected = payload.get("expected", "")
    if not isinstance(expected, str):
        raise ValueError("'expected' must be a string")

    tags = payload.get("tags") or []
    if not isinstance(tags, list) or not all(isinstance(tag, str) for tag in tags):
        raise ValueError("'tags' must be a list of strings")

    system = payload.get("system")
    if system is not None and not isinstance(system, str):
        raise ValueError("'system' must be a string")

    max_tokens = payload.get("max_tokens")
    if max_tokens is not None and (not isinstance(max_tokens, int) or max_tokens < 1):
        raise ValueError("'max_tokens' must be a positive integer")

    try:
        grading = Grading.parse(payload.get("grading"))
    except ValueError as exc:
        raise ValueError(f"invalid grading: {exc}") from exc

    return EvalCase(
        id=case_id.strip(),
        input=prompt,
        expected=expected,
        grading=grading,
        tags=tuple(tags),
        system=system,
        max_tokens=max_tokens,
    )
