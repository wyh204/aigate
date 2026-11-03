"""Offline evaluation: does a routing/caching change keep quality up?

The unit of work is a *case* (one prompt plus what a correct answer looks
like), the unit of comparison is a *run* (one strategy over one dataset), and
the unit of decision is a *gate* (does the candidate regress beyond a budget
of pass rate).

Nothing here imports the HTTP layer: evals call providers directly through the
same router the gateway uses, so a scoring run and a live request take the same
code path to the model.
"""

from app.evals.dataset import DatasetError, load_dataset
from app.evals.gate import GateReport, evaluate_gate
from app.evals.graders import grade
from app.evals.runner import RunOptions, run_dataset
from app.evals.schema import CaseResult, EvalCase, Grading, RunResult, Strategy

__all__ = [
    "CaseResult",
    "DatasetError",
    "EvalCase",
    "GateReport",
    "Grading",
    "RunOptions",
    "RunResult",
    "Strategy",
    "evaluate_gate",
    "grade",
    "load_dataset",
    "run_dataset",
]
