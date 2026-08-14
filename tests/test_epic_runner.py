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
        self.task_ids_seen: list[str] = []

    async def execute_task(self, request: LLMTaskRequest) -> LLMTaskResponse:
        self.models_seen.append(request.model)
        self.task_ids_seen.append(request.task_id)
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


async def test_merge_to_main_mode_no_integration_branch(git_repo: Path):
    # create_pr=False -> tasks merge straight to main, no integration branch.
    router = ProtocolRouter({"gemini": _FilePerTaskAdapter()})
    scheduler = DynamicScheduler(router, environ={})
    runner = EpicRunner(
        repo_root=git_repo, scheduler=scheduler, adapters={"gemini": _FilePerTaskAdapter()},
        language="python", sandbox_runner=_AlwaysPassSandbox(), create_pr=False,
    )
    result = await runner.run_epic([_task("A")])
    assert result.completed == ["A"]
    assert result.integration_branch is None
    assert result.pull_request_url is None
    assert (git_repo / "A.py").read_text() == "TASK_ID = 'A'"
    # main is the checked-out branch and has the file
    head = subprocess.run(["git", "branch", "--show-current"], cwd=git_repo, capture_output=True, text=True).stdout.strip()
    assert head == "main"


async def test_pr_mode_falls_back_to_local_merge_without_remote(git_repo: Path):
    # PR mode (default) with no git remote -> integration branch + local merge.
    runner = EpicRunner(
        repo_root=git_repo, scheduler=DynamicScheduler(ProtocolRouter({"gemini": _FilePerTaskAdapter()}), environ={}),
        adapters={"gemini": _FilePerTaskAdapter()}, language="python",
        sandbox_runner=_AlwaysPassSandbox(),  # create_pr defaults True
    )
    result = await runner.run_epic([_task("A")])
    assert result.completed == ["A"]
    assert result.integration_branch and result.integration_branch.startswith("ai-os/epic-")
    assert result.merged_to_main is True
    assert result.pull_request_url is None
    # fallback merged the integration branch into main
    assert (git_repo / "A.py").read_text() == "TASK_ID = 'A'"


async def test_pr_mode_opens_pr_when_remote_and_gh(git_repo: Path, tmp_path: Path, monkeypatch):
    # Give the repo a (local bare) remote and pretend `gh` exists; stub the
    # actual PR call so no real GitHub is hit — proving the PR path is taken.
    bare = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(bare)], check=True)
    subprocess.run(["git", "remote", "add", "origin", str(bare)], cwd=git_repo, check=True)
    subprocess.run(["git", "push", "origin", "main"], cwd=git_repo, check=True)

    import ai_os.core.epic_runner as er
    monkeypatch.setattr(er.shutil, "which", lambda name: "/usr/bin/gh" if name == "gh" else None)

    opened = {}

    async def fake_open_pr(self, head, base, title, body):
        opened["head"] = head
        opened["base"] = base
        return "https://github.com/acme/repo/pull/42"

    monkeypatch.setattr(er.GitStagingEngine, "open_pull_request", fake_open_pr)

    runner = EpicRunner(
        repo_root=git_repo, scheduler=DynamicScheduler(ProtocolRouter({"gemini": _FilePerTaskAdapter()}), environ={}),
        adapters={"gemini": _FilePerTaskAdapter()}, language="python",
        sandbox_runner=_AlwaysPassSandbox(),
    )
    result = await runner.run_epic([_task("A")])
    assert result.pull_request_url == "https://github.com/acme/repo/pull/42"
    assert result.merged_to_main is False
    assert opened["base"] == "main"
    assert opened["head"] == result.integration_branch
    # the integration branch was really pushed to the bare remote
    branches = subprocess.run(["git", "branch", "-a"], cwd=bare, capture_output=True, text=True).stdout
    assert result.integration_branch in branches


async def test_pr_mode_raises_when_pr_creation_fails(git_repo: Path, tmp_path: Path, monkeypatch):
    # When gh and remote exist, but `open_pull_request` fails (e.g. auth/network error),
    # EpicRunner must raise an error instead of silently falling back to a direct merge to main.
    bare = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(bare)], check=True)
    subprocess.run(["git", "remote", "add", "origin", str(bare)], cwd=git_repo, check=True)
    subprocess.run(["git", "push", "origin", "main"], cwd=git_repo, check=True)

    import pytest
    import ai_os.core.epic_runner as er

    monkeypatch.setattr(er.shutil, "which", lambda name: "/usr/bin/gh" if name == "gh" else None)

    async def fake_open_pr_fail(self, head, base, title, body):
        raise RuntimeError("gh: GraphQL error: Pull request already exists")

    monkeypatch.setattr(er.GitStagingEngine, "open_pull_request", fake_open_pr_fail)

    runner = EpicRunner(
        repo_root=git_repo,
        scheduler=DynamicScheduler(ProtocolRouter({"gemini": _FilePerTaskAdapter()}), environ={}),
        adapters={"gemini": _FilePerTaskAdapter()},
        language="python",
        sandbox_runner=_AlwaysPassSandbox(),
    )
    with pytest.raises(RuntimeError, match="Failed to create pull request"):
        await runner.run_epic([_task("A")])

    # Direct merge to main must NOT have occurred
    current_branch = subprocess.run(["git", "branch", "--show-current"], cwd=git_repo, capture_output=True, text=True).stdout.strip()
    assert current_branch == runner.staging.base_branch
    # Check that main branch does not contain A.py
    show_main_file = subprocess.run(["git", "show", "main:A.py"], cwd=git_repo, capture_output=True, text=True)
    assert show_main_file.returncode != 0




class _UsageLimitAdapter(BaseMCPAdapter):
    """Raises AgentUsageLimitError for the given task ids (or all if None) — the
    rest complete normally by writing `<task_id>.py`."""

    def __init__(self, limit_task_ids: set[str] | None = None) -> None:
        self.limit_task_ids = limit_task_ids

    async def execute_task(self, request: LLMTaskRequest) -> LLMTaskResponse:
        from ai_os.core.task_runner import AgentUsageLimitError

        if self.limit_task_ids is None or request.task_id in self.limit_task_ids:
            raise AgentUsageLimitError("simulated 5-hour usage limit")
        text = f"<<<AI_OS_FILE: {request.task_id}.py>>>\nTASK_ID = {request.task_id!r}\n<<<AI_OS_END>>>"
        return LLMTaskResponse(
            task_id=request.task_id, provider="fake", model_name="d", generated_text=text, usage=TokenUsage()
        )


async def test_usage_limit_blocks_task_without_crashing_epic(git_repo: Path):
    adapter = _UsageLimitAdapter()  # every task hits the limit
    runner = EpicRunner(
        repo_root=git_repo, scheduler=DynamicScheduler(ProtocolRouter({"gemini": adapter}), environ={}),
        adapters={"gemini": adapter}, language="python", sandbox_runner=_AlwaysPassSandbox(), create_pr=False,
    )
    # Must NOT raise — the epic finishes with the task BLOCKED.
    result = await runner.run_epic([_task("A")])
    assert result.blocked == ["A"]
    assert result.completed == []


async def test_usage_limit_keeps_already_completed_work(git_repo: Path):
    # A completes; B (depends on A) hits the limit -> BLOCKED, but A's work stays.
    adapter = _UsageLimitAdapter(limit_task_ids={"B"})
    runner = EpicRunner(
        repo_root=git_repo, scheduler=DynamicScheduler(ProtocolRouter({"gemini": adapter}), environ={}),
        adapters={"gemini": adapter}, language="python", sandbox_runner=_AlwaysPassSandbox(), create_pr=False,
    )
    result = await runner.run_epic([_task("A"), _task("B", deps=["A"])])
    assert result.completed == ["A"]
    assert result.blocked == ["B"]
    assert (git_repo / "A.py").read_text() == "TASK_ID = 'A'"


class _LargeFailSandbox:
    async def run_validation(self, worktree_path: Path, language: str) -> ValidationResult:
        big = "ERROR: something failed here\n" * 200  # > 2000 chars
        return ValidationResult(success=False, exit_code=1, summary="fail", output=big)


class _PayloadCapturingAdapter(_FilePerTaskAdapter):
    def __init__(self) -> None:
        super().__init__()
        self.payloads: list[str] = []

    async def execute_task(self, request: LLMTaskRequest) -> LLMTaskResponse:
        self.payloads.append(request.context_payload)
        return await super().execute_task(request)


async def test_summarizer_compresses_large_failure_before_retry(git_repo: Path):
    calls: list[str] = []

    async def fake_summarizer(output: str) -> str:
        calls.append(output)
        return "SUMMARIZED_DIAGNOSIS: fix the thing"

    adapter = _PayloadCapturingAdapter()
    runner = EpicRunner(
        repo_root=git_repo, scheduler=DynamicScheduler(ProtocolRouter({"gemini": adapter}), environ={}),
        adapters={"gemini": adapter}, language="python", sandbox_runner=_LargeFailSandbox(),
        create_pr=False, summarizer=fake_summarizer,
    )
    await runner.run_epic([_task("A")])  # fails every attempt -> summarizer runs each time

    assert calls, "summarizer should be called on the large failure output"
    assert len(calls[0]) > 2000  # it got the raw (large) log
    # a retry's prompt carries the compressed summary, not the raw log
    assert any("SUMMARIZED_DIAGNOSIS" in p for p in adapter.payloads)


async def test_summarizer_skipped_for_small_output(git_repo: Path):
    calls: list[str] = []

    async def fake_summarizer(output: str) -> str:
        calls.append(output)
        return "x"

    runner = EpicRunner(
        repo_root=git_repo, scheduler=DynamicScheduler(ProtocolRouter({"gemini": _FilePerTaskAdapter()}), environ={}),
        adapters={"gemini": _FilePerTaskAdapter()}, language="python",
        sandbox_runner=_AlwaysFailSandbox(), create_pr=False, summarizer=fake_summarizer,
    )
    await runner.run_epic([_task("A")])  # _AlwaysFailSandbox output is short ("boom")
    assert calls == []  # under the 2000-char threshold -> no summarizer call


async def test_on_event_emits_observability_events(git_repo: Path):
    events: list[dict] = []
    router = ProtocolRouter({"gemini": _FilePerTaskAdapter()})
    scheduler = DynamicScheduler(router, environ={})
    runner = EpicRunner(
        repo_root=git_repo, scheduler=scheduler, adapters={"gemini": _FilePerTaskAdapter()},
        language="python", sandbox_runner=_AlwaysPassSandbox(), create_pr=False,
        on_event=lambda ev: events.append(ev),
    )
    await runner.run_epic([_task("A")])

    types = [e["type"] for e in events]
    assert "attempt" in types
    assert "validation" in types
    assert "merged" in types
    # the validation event carries the sandbox outcome
    val = next(e for e in events if e["type"] == "validation")
    assert val["task_id"] == "A" and val["success"] is True and val["exit_code"] == 0


async def test_on_event_reports_sandbox_failure_and_retry(git_repo: Path):
    events: list[dict] = []
    router = ProtocolRouter({"gemini": _FilePerTaskAdapter()})
    scheduler = DynamicScheduler(router, environ={})
    runner = EpicRunner(
        repo_root=git_repo, scheduler=scheduler, adapters={"gemini": _FilePerTaskAdapter()},
        language="python", sandbox_runner=_AlwaysFailSandbox(), create_pr=False,
        on_event=lambda ev: events.append(ev),
    )
    # default max_retries=3 -> 4 attempts, all fail
    await runner.run_epic([_task("A")])

    failed = [e for e in events if e["type"] == "validation" and not e["success"]]
    assert failed, "expected failing validation events"
    assert "boom" in failed[0]["output"]  # the sandbox output is surfaced
    assert any(e["type"] == "retry" for e in events)  # retries were reported


async def test_blocked_task_branch_is_preserved(git_repo: Path):
    # A BLOCKED task keeps its branch so the failing code stays inspectable.
    runner = EpicRunner(
        repo_root=git_repo, scheduler=DynamicScheduler(ProtocolRouter({"gemini": _FilePerTaskAdapter()}), environ={}),
        adapters={"gemini": _FilePerTaskAdapter()}, language="python",
        sandbox_runner=_AlwaysFailSandbox(), create_pr=False,
    )
    result = await runner.run_epic([_task("A")])
    assert result.blocked == ["A"]
    branches = subprocess.run(["git", "branch"], cwd=git_repo, capture_output=True, text=True).stdout
    assert "ai-os/A" in branches  # branch kept for inspection (not deleted)


def test_pr_body_includes_blocked_errors():
    from ai_os.core.scheduler import Assignment
    from ai_os.core.epic_runner import EpicRunResult
    from ai_os.core.task_runner import TaskRunResult

    runner = EpicRunner(
        repo_root="/tmp", scheduler=DynamicScheduler(ProtocolRouter({"gemini": _FilePerTaskAdapter()}), environ={}),
        adapters={"gemini": _FilePerTaskAdapter()}, language="python",
    )
    result = EpicRunResult()
    result.blocked = ["A"]
    result.assignments = {"A": Assignment("anthropic", "sonnet")}
    result.task_results = {"A": TaskRunResult(task_id="A", status="BLOCKED", attempts=4, final_output="ERROR TS2322: asChild does not exist")}
    tasks = [_task("A")]

    _title, body = runner._build_pr_content(result, tasks)
    assert "Blocked tasks" in body
    assert "ai-os/A" in body  # the branch to inspect
    assert "TS2322" in body  # the actual error surfaced


def test_pr_body_includes_dag_table():
    from ai_os.core.scheduler import Assignment
    from ai_os.core.epic_runner import EpicRunResult

    runner = EpicRunner(
        repo_root="/tmp", scheduler=DynamicScheduler(ProtocolRouter({"gemini": _FilePerTaskAdapter()}), environ={}),
        adapters={"gemini": _FilePerTaskAdapter()}, language="python",
    )
    result = EpicRunResult()
    result.raw_prompt = "add a landing page"
    result.completed = ["A", "B"]
    result.blocked = ["C"]
    result.assignments = {
        "A": Assignment("anthropic", "sonnet"),
        "B": Assignment("gemini", None),
        "C": Assignment("anthropic", "opus"),
    }
    tasks = [_task("A"), _task("B", deps=["A"]), _task("C", deps=["B"])]

    title, body = runner._build_pr_content(result, tasks)
    assert title == "AI-OS: add a landing page"
    # the DAG table + each task row + routing + outcome
    assert "| Task | Title | Risk | Routed to | Depends on | Result |" in body
    assert "anthropic → sonnet" in body
    assert "gemini → default" in body  # model=None -> "default"
    assert "✅ merged" in body and "⛔ blocked" in body
    assert "| B | B | LOW | gemini → default | A |" in body
    assert "> add a landing page" in body


async def test_resume_epic_reruns_only_incomplete_tasks(git_repo: Path, tmp_path: Path):
    from ai_os.core.persistence import Persistence

    persistence, engine = await Persistence.open(f"sqlite+aiosqlite:///{tmp_path / 'r.db'}")
    # Seed the state a crash would have left: epic E with A COMPLETED (its work
    # already merged to main) and B (depends on A) still PENDING.
    await persistence.create_epic("E", "demo", "do A then B", status="FAILED")
    a, b = _task("A"), _task("B", deps=["A"])
    await persistence.upsert_task(a, "E", assigned_model=None, status="COMPLETED")
    await persistence.upsert_task(b, "E", assigned_model=None, status="PENDING")
    # A's merged work is on main already:
    (git_repo / "A.py").write_text("TASK_ID = 'A'\n")
    subprocess.run(["git", "add", "."], cwd=git_repo, check=True)
    subprocess.run(["git", "commit", "-m", "A done"], cwd=git_repo, check=True)

    adapter = _FilePerTaskAdapter()
    router = ProtocolRouter({"gemini": adapter})
    scheduler = DynamicScheduler(router, environ={})
    runner = EpicRunner(
        repo_root=git_repo, scheduler=scheduler, adapters={"gemini": adapter},
        language="python", sandbox_runner=_AlwaysPassSandbox(), persistence=persistence,
    )
    result = await runner.resume_epic("E")

    # A was pre-seeded completed (not re-run); only B actually executed.
    assert set(result.completed) == {"A", "B"}
    assert adapter.task_ids_seen == ["B"]
    assert (git_repo / "B.py").read_text() == "TASK_ID = 'B'"
    # The epic is now COMPLETED in the DB.
    summaries = await persistence.epic_summaries()
    assert summaries[0].status == "COMPLETED"
    await engine.dispose()


async def test_resume_epic_unknown_id_raises(git_repo: Path, tmp_path: Path):
    from ai_os.core.persistence import Persistence

    persistence, engine = await Persistence.open(f"sqlite+aiosqlite:///{tmp_path / 'r2.db'}")
    adapter = _FilePerTaskAdapter()
    scheduler = DynamicScheduler(ProtocolRouter({"gemini": adapter}), environ={})
    runner = EpicRunner(
        repo_root=git_repo, scheduler=scheduler, adapters={"gemini": adapter},
        language="python", sandbox_runner=_AlwaysPassSandbox(), persistence=persistence,
    )
    with pytest.raises(ValueError):
        await runner.resume_epic("does-not-exist")
    await engine.dispose()


async def test_resume_epic_without_persistence_raises(git_repo: Path):
    adapter = _FilePerTaskAdapter()
    scheduler = DynamicScheduler(ProtocolRouter({"gemini": adapter}), environ={})
    runner = EpicRunner(
        repo_root=git_repo, scheduler=scheduler, adapters={"gemini": adapter},
        language="python", sandbox_runner=_AlwaysPassSandbox(),
    )
    with pytest.raises(ValueError):
        await runner.resume_epic("whatever")


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
