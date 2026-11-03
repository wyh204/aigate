"""Graders. Deterministic first, LLM judge only where the rule cannot be written.

Every grader returns ``(passed, reason)``; the reason lands in the run report so
a failing case is debuggable without re-reading the dataset.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.evals.schema import EvalCase

Judge = Callable[["EvalCase", str], Awaitable[bool]]

_NUMBER = re.compile(r"-?\d+(?:\.\d+)?")


def normalise(text: str, *, ignore_case: bool = True) -> str:
    """Collapse whitespace so formatting differences are not scored as errors."""
    collapsed = " ".join(text.split())
    return collapsed.lower() if ignore_case else collapsed


async def grade(case: EvalCase, output: str, *, judge: Judge | None = None) -> tuple[bool, str]:
    grading = case.grading
    expected = grading.expected if grading.expected is not None else case.expected

    if grading.kind == "llm_judge":
        if judge is None:
            return False, "llm_judge case but no judge configured"
        return await judge(case, output), "judge"

    if grading.kind == "regex":
        flags = re.DOTALL | (re.IGNORECASE if grading.ignore_case else 0)
        ok = re.search(grading.pattern or expected, output, flags) is not None
        return ok, "" if ok else "regex did not match"

    if not expected:
        return False, "case has no expected answer"

    if grading.kind == "exact":
        ok = normalise(output, ignore_case=grading.ignore_case) == normalise(
            expected, ignore_case=grading.ignore_case
        )
        return ok, "" if ok else "exact mismatch"

    if grading.kind == "contains":
        ok = normalise(expected, ignore_case=grading.ignore_case) in normalise(
            output, ignore_case=grading.ignore_case
        )
        return ok, "" if ok else f"missing {expected!r}"

    if grading.kind == "numeric":
        wanted = _numbers(expected)
        got = _numbers(output)
        ok = bool(wanted) and all(
            any(_close(want, value, grading.tolerance) for value in got) for want in wanted
        )
        return ok, "" if ok else f"expected number(s) {wanted} not found in {got}"

    return False, f"unsupported grading kind {grading.kind!r}"


def _numbers(text: str) -> list[float]:
    return [float(match.group()) for match in _NUMBER.finditer(text)]


def _close(left: float, right: float, tolerance: float) -> bool:
    return abs(left - right) <= tolerance * max(1.0, abs(left), abs(right))


def judge_prompt(case: EvalCase, output: str) -> str:
    rubric = case.grading.rubric or (
        "Is the candidate answer correct and complete compared with the reference answer?"
    )
    return (
        "You are an exacting grader for an AI evaluation suite.\n\n"
        f"Rubric: {rubric}\n\n"
        f"Question:\n{case.input}\n\n"
        f"Reference answer:\n{case.expected}\n\n"
        f"Candidate answer:\n{output}\n\n"
        "Reply with exactly one word: PASS if the candidate satisfies the rubric, FAIL otherwise."
    )


def parse_judge_reply(text: str) -> bool:
    first = text.strip().splitlines()[0].strip().upper() if text.strip() else ""
    return first.startswith("PASS")
