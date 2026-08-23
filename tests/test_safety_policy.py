"""Tests for the pure plan-safety decision extracted from `cli.py` (issue #35,
see `docs/23_JOB_LAYER_AND_SAFETY_SERVICE.md` §1)."""
from __future__ import annotations

from ai_os.core.models import TaskNode
from ai_os.core.safety_policy import PlanSafetyDecision, evaluate_plan_safety


def _task(task_id: str, write_set: set[str]) -> TaskNode:
    return TaskNode(
        id=task_id,
        title="t",
        description="d",
        risk_level="LOW",
        target_files=sorted(write_set),
        write_set=write_set,
    )


def test_no_flagged_paths_merge_to_main_allowed():
    tasks = [_task("T1", {"a.py"}), _task("T2", {"b.py"})]

    decision = evaluate_plan_safety(tasks, merge_to_main_requested=True)

    assert decision == PlanSafetyDecision(
        flagged_paths=frozenset(), merge_to_main_allowed=True, reason=None
    )


def test_flagged_paths_with_merge_to_main_requested_is_not_allowed():
    tasks = [_task("T1", {".github/workflows/deploy.yml"}), _task("T2", {"b.py"})]

    decision = evaluate_plan_safety(tasks, merge_to_main_requested=True)

    assert decision.flagged_paths == frozenset({".github/workflows/deploy.yml"})
    assert decision.merge_to_main_allowed is False
    assert decision.reason is not None
    assert "--merge-to-main" in decision.reason


def test_flagged_paths_without_merge_to_main_requested_goes_through_pr():
    tasks = [_task("T1", {".env"}), _task("T2", {"b.py"})]

    decision = evaluate_plan_safety(tasks, merge_to_main_requested=False)

    assert decision.flagged_paths == frozenset({".env"})
    assert decision.merge_to_main_allowed is True
    assert decision.reason is None


def test_multiple_flagged_paths_are_all_reported():
    tasks = [_task("T1", {".env", "Dockerfile", "src/app.py"})]

    decision = evaluate_plan_safety(tasks, merge_to_main_requested=False)

    assert decision.flagged_paths == frozenset({".env", "Dockerfile"})
