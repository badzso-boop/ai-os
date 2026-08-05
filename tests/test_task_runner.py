"""Tests for `ai_os.core.task_runner.TaskRunner` — real `LockManager` +
real `GitStagingEngine` against a disposable git repo (matching Phase 2's
testing philosophy), a real (but empty/trivial) `KnowledgeEngine`, and a
FAKE agent-turn executor + FAKE sandbox runner. No real `claude` CLI
invocation and no real Docker container anywhere in this file, per explicit
instruction: this proves the retry/HITL bookkeeping logic works correctly,
not that a live LLM can fix real code.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from ai_os.core.lock_manager import LockManager
from ai_os.core.models import TaskNode
from ai_os.core.staging import GitStagingEngine
from ai_os.core.task_runner import AgentTurnContext, TaskRunner
from ai_os.knowledge.graph_engine import KnowledgeEngine
from ai_os.sandbox.container_runner import ValidationResult


@pytest.fixture()
def git_repo(tmp_path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@ai-os.local"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "AI-OS Test"], cwd=repo, check=True)
    (repo / "README.md").write_text("init\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True)
    return repo


class _ScriptedSandboxRunner:
    """Fails the first `fail_count` calls, then succeeds forever after."""

    def __init__(self, fail_count: int) -> None:
        self.fail_count = fail_count
        self.calls = 0

    async def run_validation(self, worktree_path: Path, language: str) -> ValidationResult:
        self.calls += 1
        if self.calls <= self.fail_count:
            return ValidationResult(
                success=False, exit_code=1, summary="Validation failed.", output=f"attempt {self.calls} failed"
            )
        return ValidationResult(success=True, exit_code=0, summary="Validation passed.", output="ok")


def _make_fake_executor(filename: str = "output.txt"):
    calls: list[AgentTurnContext] = []

    async def execute(ctx: AgentTurnContext) -> None:
        calls.append(ctx)
        (ctx.worktree_path / filename).write_text(f"attempt {ctx.attempt}\n")

    execute.calls = calls  # type: ignore[attr-defined]
    return execute


def _task(**overrides) -> TaskNode:
    base = dict(
        id="TASK-1",
        title="do the thing",
        description="d",
        risk_level="LOW",
        target_files=[],
        max_retries=3,
    )
    base.update(overrides)
    return TaskNode(**base)


async def test_succeeds_on_first_attempt(git_repo: Path):
    staging = GitStagingEngine(git_repo)
    runner = TaskRunner(
        lock_manager=LockManager(),
        staging=staging,
        knowledge_engine=KnowledgeEngine(),
        agent_turn_executor=_make_fake_executor(),
        sandbox_runner=_ScriptedSandboxRunner(fail_count=0),
    )
    result = await runner.run_task(_task(), language="python")

    assert result.status == "COMPLETED"
    assert result.attempts == 1
    assert (git_repo / "output.txt").read_text() == "attempt 1\n"


async def test_succeeds_on_second_attempt_matching_doc16_acceptance_criterion(git_repo: Path):
    staging = GitStagingEngine(git_repo)
    executor = _make_fake_executor()
    runner = TaskRunner(
        lock_manager=LockManager(),
        staging=staging,
        knowledge_engine=KnowledgeEngine(),
        agent_turn_executor=executor,
        sandbox_runner=_ScriptedSandboxRunner(fail_count=1),
    )
    result = await runner.run_task(_task(max_retries=3), language="python")

    assert result.status == "COMPLETED"
    assert result.attempts == 2
    assert len(executor.calls) == 2
    # The second attempt must have seen the first attempt's failure output —
    # this is the actual "prompt feedback loop" doc 05/16 describe.
    assert executor.calls[0].previous_validation_output is None
    assert executor.calls[1].previous_validation_output == "attempt 1 failed"


async def test_exhausts_retries_removes_worktree_but_keeps_branch(git_repo: Path):
    staging = GitStagingEngine(git_repo)
    runner = TaskRunner(
        lock_manager=LockManager(),
        staging=staging,
        knowledge_engine=KnowledgeEngine(),
        agent_turn_executor=_make_fake_executor(),
        sandbox_runner=_ScriptedSandboxRunner(fail_count=999),
    )
    result = await runner.run_task(_task(max_retries=1), language="python")

    assert result.status == "BLOCKED"
    assert result.attempts == 2  # max_retries=1 -> 2 total attempts
    assert result.final_output == "attempt 2 failed"

    # The worktree DIRECTORY is torn down...
    wt_list = subprocess.run(
        ["git", "worktree", "list"], cwd=git_repo, check=True, capture_output=True, text=True
    ).stdout
    assert wt_list.strip().count("\n") == 0  # only the primary worktree line
    # ...but the BRANCH is KEPT so the failing code stays inspectable.
    branch_list = subprocess.run(
        ["git", "branch", "--list", "ai-os/TASK-1"], cwd=git_repo, check=True, capture_output=True, text=True
    ).stdout
    assert branch_list.strip() != ""


async def test_reports_status_transitions(git_repo: Path):
    staging = GitStagingEngine(git_repo)
    statuses: list[tuple[str, str]] = []
    runner = TaskRunner(
        lock_manager=LockManager(),
        staging=staging,
        knowledge_engine=KnowledgeEngine(),
        agent_turn_executor=_make_fake_executor(),
        sandbox_runner=_ScriptedSandboxRunner(fail_count=0),
        on_status_change=lambda task_id, status: statuses.append((task_id, status)),
    )
    await runner.run_task(_task(), language="python")

    assert statuses == [("TASK-1", "RUNNING"), ("TASK-1", "COMPLETED")]


async def test_reports_blocked_status_on_exhaustion(git_repo: Path):
    staging = GitStagingEngine(git_repo)
    statuses: list[tuple[str, str]] = []
    runner = TaskRunner(
        lock_manager=LockManager(),
        staging=staging,
        knowledge_engine=KnowledgeEngine(),
        agent_turn_executor=_make_fake_executor(),
        sandbox_runner=_ScriptedSandboxRunner(fail_count=999),
        on_status_change=lambda task_id, status: statuses.append((task_id, status)),
    )
    await runner.run_task(_task(max_retries=0), language="python")

    assert statuses == [("TASK-1", "RUNNING"), ("TASK-1", "BLOCKED")]
