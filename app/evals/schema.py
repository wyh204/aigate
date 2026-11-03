"""Data model for eval datasets, runs and results."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

GradingKind = Literal["exact", "contains", "regex", "numeric", "llm_judge"]
GRADING_KINDS: tuple[str, ...] = ("exact", "contains", "regex", "numeric", "llm_judge")

_GRADING_FIELDS = {"kind", "expected", "pattern", "tolerance", "ignore_case", "rubric"}


@dataclass(frozen=True)
class Grading:
    """How one case is judged.

    ``expected`` overrides the case-level expected text for this grader only.
    That is what makes one dataset reusable across strategies: the reference
    answer stays in the case, the judging rule travels with whoever changes it.
    """

    kind: GradingKind = "contains"
    expected: str | None = None
    pattern: str | None = None
    tolerance: float = 1e-6
    ignore_case: bool = True
    rubric: str | None = None

    def __post_init__(self) -> None:
        if self.kind not in GRADING_KINDS:
            raise ValueError(
                f"unknown grading kind {self.kind!r}; expected one of {', '.join(GRADING_KINDS)}"
            )
        if self.kind == "regex" and not self.pattern:
            raise ValueError("grading kind 'regex' requires 'pattern'")

    @classmethod
    def parse(cls, raw: Any) -> Grading:
        if raw is None:
            return cls()
        if isinstance(raw, str):
            return cls(kind=raw)  # type: ignore[arg-type]
        if isinstance(raw, dict):
            unknown = set(raw) - _GRADING_FIELDS
            if unknown:
                raise ValueError(f"unknown grading field(s): {', '.join(sorted(unknown))}")
            return cls(**raw)
        raise ValueError("'grading' must be a string kind or an object")

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "expected": self.expected,
            "pattern": self.pattern,
            "tolerance": self.tolerance,
            "ignore_case": self.ignore_case,
            "rubric": self.rubric,
        }


@dataclass(frozen=True)
class EvalCase:
    id: str
    input: str
    expected: str = ""
    grading: Grading = field(default_factory=Grading)
    tags: tuple[str, ...] = ()
    system: str | None = None
    max_tokens: int | None = None
    source: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "input": self.input,
            "expected": self.expected,
            "grading": self.grading.to_dict(),
            "tags": list(self.tags),
            "system": self.system,
            "max_tokens": self.max_tokens,
        }


@dataclass(frozen=True)
class Strategy:
    """One way to answer the dataset. The thing a gate compares against base."""

    name: str = "default"
    route: str = "default"
    temperature: float = 0.0
    max_tokens: int | None = None
    judge_route: str = "default"
    system_prompt: str | None = None
    concurrency: int = 4

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "route": self.route,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "judge_route": self.judge_route,
            "system_prompt": self.system_prompt,
            "concurrency": self.concurrency,
        }


@dataclass
class CaseResult:
    case_id: str
    passed: bool
    output: str = ""
    error: str | None = None
    latency_ms: float = 0.0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cost_usd: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "passed": self.passed,
            "output": self.output,
            "error": self.error,
            "latency_ms": round(self.latency_ms, 2),
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "cost_usd": self.cost_usd,
        }


@dataclass
class RunResult:
    name: str
    dataset: str
    strategy: dict[str, Any]
    cases: list[CaseResult]
    started_at: str
    finished_at: str
    run_id: int | None = None

    @property
    def passed(self) -> int:
        return sum(1 for case in self.cases if case.passed)

    @property
    def total(self) -> int:
        return len(self.cases)

    @property
    def pass_rate(self) -> float:
        return round(self.passed / self.total, 4) if self.total else 0.0

    @property
    def total_tokens(self) -> int:
        return sum(case.total_tokens for case in self.cases)

    @property
    def cost_usd(self) -> float:
        return round(sum(case.cost_usd for case in self.cases), 8)

    def summary(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "name": self.name,
            "dataset": self.dataset,
            "strategy": self.strategy,
            "passed": self.passed,
            "total": self.total,
            "pass_rate": self.pass_rate,
            "total_tokens": self.total_tokens,
            "cost_usd": self.cost_usd,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self.summary(), "cases": [case.to_dict() for case in self.cases]}
