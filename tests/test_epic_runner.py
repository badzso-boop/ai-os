"""Integration tests for `ai_os.core.epic_runner.EpicRunner`.

Real `LockManager` + real `GitStagingEngine` + a real disposable git repo
(Phase 2's testing philosophy), a real `DynamicScheduler`/`ProtocolRouter`,
a FAKE completion adapter (returns AI_OS_FILE blocks — no real LLM), and a
FAKE sandbox runner. Proves a multi-task DAG really executes generation by
generation against real git worktrees/merges, with per-task model routing,
and that a BLOCKED task's dependents are skipped. No real LLM or Docker.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from ai_os.core.epic_runner import EpicRunner
from ai_os.core.models import TaskNode
from ai_os.core.scheduler import DynamicScheduler
from ai_os.mcp.adapters.base_adapter import BaseMCPAdapter, LLMTaskRequest, LLMTaskResponse, TokenUsage
from ai_os.mcp.protocol_router import ProtocolRouter
from ai_os.sandbox.container_runner import ValidationResult


@pytest.fixture()
def git_repo(tmp_path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@ai-os.local"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "AI-OS Test"], cwd=repo, check=True)
    (repo / "README.md").write_text("init\n")
    (repo / "base.py").write_text("BASE = 1\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True)
    return repo


class _FilePerTaskAdapter(BaseMCPAdapter):
    """Simulates a completion model: writes `<task_id>.py` with the task id.
    Records which model it was called with, so routing can be asserted."""

    def __init__(self) -> None:
        self.models_seen: list[str | None] = []

    async def execute_task(self, request: LLMTaskRequest) -> LLMTaskResponse:
        self.models_seen.append(request.model)
        # task_id is the LLMTaskRequest.task_id
        filename = f"{request.task_id}.py"
        text = f"<<<AI_OS_FILE: {filename}>>>\nTASK_ID = {request.task_id!r}\n<<<AI_OS_END>>>"
        return LLMTaskResponse(
            task_id=request.task_id, provider="fake", model_name=request.model or "d", generated_text=text, usage=TokenUsage()
        )


class _AlwaysPassSandbox:
    def __init__(self) -> None:
        self.calls = 0

    async def run_validation(self, worktree_path: Path, language: str) -> ValidationResult:
        self.calls += 1
        return ValidationResult(success=True, exit_code=0, summary="ok", output="ok")


class _AlwaysFailSandbox:
    async def run_validation(self, worktree_path: Path, language: str) -> ValidationResult:
        return ValidationResult(success=False, exit_code=1, summary="fail", output="boom")


def _runner(git_repo, adapter, sandbox, on_status_change=None) -> EpicRunner:
    # Route everything to the fake adapter registered as "gemini" (a
    # non-anthropic provider -> the completion executor path, no real claude).
    router = ProtocolRouter({"gemini": adapter})
    scheduler = DynamicScheduler(router, environ={})
    return EpicRunner(
        repo_root=git_repo, scheduler=scheduler, adapters={"gemini": adapter},
        language="python", sandbox_runner=sandbox, on_status_change=on_status_change,
    )


def _task(tid, deps=None, risk="LOW", write=None) -> TaskNode:
    files = write or [f"{tid}.py"]
    return TaskNode(
        id=tid, title=tid, description=f"write {tid}", risk_level=risk,
        target_files=files, write_set=set(files), dependencies=deps or [],
    )


async def test_diamond_dag_all_complete_in_order(git_repo: Path):
    # A -> {B, C} -> D
    tasks = [
        _task("A"),
        _task("B", deps=["A"]),
        _task("C", deps=["A"]),
        _task("D", deps=["B", "C"]),
    ]
    adapter = _FilePerTaskAdapter()
    result = await _runner(git_repo, adapter, _AlwaysPassSandbox()).run_epic(tasks)

    assert set(result.completed) == {"A", "B", "C", "D"}
    assert result.blocked == []
    assert result.skipped == []

    # Every task's file landed on main (parser strips the trailing newline
    # before the AI_OS_END sentinel, so no trailing "\n").
    for tid in ["A", "B", "C", "D"]:
        assert (git_repo / f"{tid}.py").read_text() == f"TASK_ID = {tid!r}"

    # Commit order respects dependencies: A before B/C before D.
    log = subprocess.run(
        ["git", "log", "--format=%s", "main"], cwd=git_repo, check=True, capture_output=True, text=True
    ).stdout.splitlines()
    pos = {line.split(":")[0]: i for i, line in enumerate(log) if line.startswith(("A", "B", "C", "D"))}
    # git log is newest-first, so a smaller index = later commit.
    assert pos["A"] > pos["B"] and pos["A"] > pos["C"]
    assert pos["B"] > pos["D"] and pos["C"] > pos["D"]


async def test_per_task_model_routing(git_repo: Path):
    # LOW and CRITICAL tasks routed to different models within the same provider.
    tasks = [_task("LOWTASK", risk="LOW"), _task("CRIT", risk="CRITICAL", deps=["LOWTASK"])]
    adapter = _FilePerTaskAdapter()
    # Route every risk level to the fake "gemini" provider (the default order
    # sends CRITICAL only to anthropic), so this test can observe per-risk
    # model selection within one provider.
    router = ProtocolRouter(
        {"gemini": adapter},
        risk_provider_order={level: ["gemini"] for level in ("LOW", "MEDIUM", "HIGH", "CRITICAL")},
    )
    scheduler = DynamicScheduler(
        router, environ={"AI_OS_MODEL_GEMINI_LOW": "flash-lite", "AI_OS_MODEL_GEMINI_CRITICAL": "flash-pro"}
    )
    runner = EpicRunner(
        repo_root=git_repo, scheduler=scheduler, adapters={"gemini": adapter},
        language="python", sandbox_runner=_AlwaysPassSandbox(),
    )
    result = await runner.run_epic(tasks)
    assert set(result.completed) == {"LOWTASK", "CRIT"}
    assert "flash-lite" in adapter.models_seen
    assert "flash-pro" in adapter.models_seen


async def test_blocked_task_skips_its_dependents(git_repo: Path):
    # A fails validation forever -> BLOCKED; B depends on A -> skipped.
    tasks = [_task("A"), _task("B", deps=["A"])]
    result = await _runner(git_repo, _FilePerTaskAdapter(), _AlwaysFailSandbox()).run_epic(tasks)

    assert result.blocked == ["A"]
    assert result.skipped == ["B"]
    assert result.completed == []
    # Nothing merged to main.
    assert not (git_repo / "A.py").exists()
    assert not (git_repo / "B.py").exists()


async def test_independent_tasks_all_run(git_repo: Path):
    tasks = [_task("X"), _task("Y"), _task("Z")]  # no deps, all one generation
    result = await _runner(git_repo, _FilePerTaskAdapter(), _AlwaysPassSandbox()).run_epic(tasks)
    assert set(result.completed) == {"X", "Y", "Z"}


class _RateLimitedAdapter(BaseMCPAdapter):
    """Always raises RateLimitedError from its completion path — stands in for a
    provider that's being throttled, so EpicRunner must fall back to the next."""

    async def execute_task(self, request: LLMTaskRequest) -> LLMTaskResponse:
        from ai_os.mcp.adapters.base_adapter import RateLimitedError

        raise RateLimitedError("gemini", retry_after=0.0)


async def _no_sleep(_delay):  # keep fallback tests instant
    return None


async def test_scheduling_policy_falls_back_to_next_provider(git_repo: Path):
    from ai_os.core.scheduling_policy import SchedulingPolicy

    limited = _RateLimitedAdapter()
    working = _FilePerTaskAdapter()
    adapters = {"gemini": limited, "openrouter": working}
    # gemini first, openrouter second — gemini keeps rate-limiting, so the task
    # must complete via openrouter.
    order = {lvl: ["gemini", "openrouter"] for lvl in ("LOW", "MEDIUM", "HIGH", "CRITICAL")}
    router = ProtocolRouter(adapters, risk_provider_order=order)
    scheduler = DynamicScheduler(router, environ={})
    runner = EpicRunner(
        repo_root=git_repo, scheduler=scheduler, adapters=adapters, language="python",
        sandbox_runner=_AlwaysPassSandbox(),
        scheduling_policy=SchedulingPolicy(max_rate_limit_retries=1, sleep=_no_sleep),
    )
    result = await runner.run_epic([_task("A")])
    assert result.completed == ["A"]
    # openrouter (the fallback) actually wrote the file.
    assert (git_repo / "A.py").read_text() == "TASK_ID = 'A'"


async def test_budget_cap_skips_remaining_tasks(git_repo: Path, tmp_path: Path):
    from ai_os.core.persistence import Persistence
    from ai_os.core.scheduling_policy import SchedulingPolicy

    # A completion adapter that reports a real per-turn cost, so epic spend
    # accumulates and trips a tiny budget after the first batch.
    class _CostingAdapter(BaseMCPAdapter):
        async def execute_task(self, request: LLMTaskRequest) -> LLMTaskResponse:
            filename = f"{request.task_id}.py"
            text = f"<<<AI_OS_FILE: {filename}>>>\nX = 1\n<<<AI_OS_END>>>"
            return LLMTaskResponse(
                task_id=request.task_id, provider="gemini", model_name="flash",
                generated_text=text, usage=TokenUsage(estimated_usd_cost=1.0),
            )

    persistence, engine = await Persistence.open(f"sqlite+aiosqlite:///{tmp_path / 'b.db'}")
    adapter = _CostingAdapter()
    router = ProtocolRouter({"gemini": adapter})
    scheduler = DynamicScheduler(router, environ={})
    runner = EpicRunner(
        repo_root=git_repo, scheduler=scheduler, adapters={"gemini": adapter}, language="python",
        sandbox_runner=_AlwaysPassSandbox(), persistence=persistence,
        scheduling_policy=SchedulingPolicy(budget_usd=0.5, sleep=_no_sleep),
    )
    # A (batch 1, costs $1.0) then B depends on A (batch 2). After batch 1 the
    # $0.5 cap is blown, so B is skipped.
    result = await runner.run_epic([_task("A"), _task("B", deps=["A"])])
    assert result.completed == ["A"]
    assert result.skipped == ["B"]
    await engine.dispose()


async def test_epic_run_persists_accounting_rows(git_repo: Path, tmp_path: Path):
    # With persistence injected, a real epic run must leave epic/task/token_cost/
    # lock_audit rows behind (Stage 3), against a real SQLite file.
    from ai_os.core.persistence import Persistence

    persistence, engine = await Persistence.open(f"sqlite+aiosqlite:///{tmp_path / 'acct.db'}")
    adapter = _FilePerTaskAdapter()
    router = ProtocolRouter({"gemini": adapter})
    scheduler = DynamicScheduler(router, environ={})
    runner = EpicRunner(
        repo_root=git_repo, scheduler=scheduler, adapters={"gemini": adapter},
        language="python", sandbox_runner=_AlwaysPassSandbox(), persistence=persistence,
    )
    tasks = [_task("A"), _task("B", deps=["A"])]
    result = await runner.run_epic(tasks, epic_title="demo", raw_prompt="do A then B")

    assert set(result.completed) == {"A", "B"}
    assert result.epic_id is not None

    summaries = await persistence.epic_summaries()
    assert len(summaries) == 1
    s = summaries[0]
    assert s.id == result.epic_id and s.title == "demo" and s.status == "COMPLETED"
    assert s.total_tasks == 2 and s.completed_tasks == 2
    # The completion adapter reported usage per turn -> token_cost rows exist.
    breakdown = await persistence.provider_breakdown(epic_id=result.epic_id)
    assert breakdown and breakdown[0].calls >= 2
    await engine.dispose()
