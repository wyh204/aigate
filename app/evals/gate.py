"""The release gate.

A strategy is allowed to be cheaper only if it is not meaningfully worse. Two
numbers decide it: pass rate (absolute percentage-point drop) and, as a
warning rather than a block, cost. The default budget is 2pp — small enough to
catch real regressions, large enough to tolerate judge noise.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

DEFAULT_MAX_PASS_RATE_DROP = 0.02


@dataclass
class GateReport:
    passed: bool
    reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    pass_delta: float = 0.0
    cost_delta_pct: float | None = None
    max_pass_rate_drop: float = DEFAULT_MAX_PASS_RATE_DROP
    baseline: dict[str, Any] = field(default_factory=dict)
    candidate: dict[str, Any] = field(default_factory=dict)

    @property
    def decision(self) -> str:
        return "ALLOW" if self.passed else "BLOCKED"

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision": self.decision,
            "reasons": self.reasons,
            "warnings": self.warnings,
            "pass_rate_drop": round(self.pass_delta, 4),
            "cost_delta_pct": self.cost_delta_pct,
            "max_pass_rate_drop": self.max_pass_rate_drop,
            "baseline": _summary(self.baseline),
            "candidate": _summary(self.candidate),
        }


def _summary(run: dict[str, Any]) -> dict[str, Any]:
    return {
        "run_id": run.get("run_id"),
        "name": run.get("name"),
        "dataset": run.get("dataset"),
        "pass_rate": run.get("pass_rate"),
        "passed": run.get("passed"),
        "total": run.get("total"),
        "cost_usd": run.get("cost_usd"),
    }


def evaluate_gate(
    baseline: dict[str, Any],
    candidate: dict[str, Any],
    *,
    max_pass_rate_drop: float = DEFAULT_MAX_PASS_RATE_DROP,
) -> GateReport:
    base_rate = float(baseline.get("pass_rate") or 0.0)
    cand_rate = float(candidate.get("pass_rate") or 0.0)
    drop = round(base_rate - cand_rate, 4)

    reasons: list[str] = []
    warnings: list[str] = []

    base_total = int(baseline.get("total") or 0)
    cand_total = int(candidate.get("total") or 0)
    if base_total != cand_total:
        reasons.append(
            f"datasets are not comparable: baseline has {base_total} cases, "
            f"candidate has {cand_total}"
        )
    if drop > max_pass_rate_drop + 1e-9:
        reasons.append(
            f"pass rate dropped {drop * 100:.1f}pp, budget is {max_pass_rate_drop * 100:.1f}pp"
        )

    base_cost = float(baseline.get("cost_usd") or 0.0)
    cand_cost = float(candidate.get("cost_usd") or 0.0)
    cost_delta_pct: float | None = None
    if base_cost > 0:
        cost_delta_pct = round((cand_cost - base_cost) / base_cost * 100, 1)
        if cost_delta_pct > 0:
            warnings.append(f"cost increased {cost_delta_pct:.1f}% alongside quality change")

    return GateReport(
        passed=not reasons,
        reasons=reasons,
        warnings=warnings,
        pass_delta=drop,
        cost_delta_pct=cost_delta_pct,
        max_pass_rate_drop=max_pass_rate_drop,
        baseline=baseline,
        candidate=candidate,
    )
