"""Executes a planned TaskNode DAG batch-by-batch (Phase 4a, doc 02 §1/§3.2).

Ties everything together: `planner.topological_batches` gives dependency
generations; tasks *within* a generation run concurrently via `asyncio.gather`
sharing ONE `LockManager` + ONE `GitStagingEngine`, so Phase 2's concurrency
guarantees apply directly (the Lock Manager serializes any two tasks whose
write sets collide; the staging engine's merge lock serializes merges and
real-rebases the second merger — exactly what its integration test proves).
Generations run strictly in order.

Each task is routed to a (provider, model) by the `DynamicScheduler` based on
its own `risk_level`, and gets the right kind of agent turn: the Anthropic
CLI-session path gets real autonomous MCP tool use
(`build_claude_cli_agent_turn_executor`), every other provider gets the
completion-based write-back executor (`build_completion_agent_turn_executor`).
Both feed the same `TaskRunner`, so retries + sandbox validation + HITL
escalation behave identically regardless of provider.

Between generations the repo is re-scanned so later tasks see earlier merged
work in their Context Cache — a full rescan (~2s for ~350 files) is cheap
relative to an LLM turn, and correctness (no stale context) beats shaving it.
"""
from __future__ import annotations

import asyncio
import tempfile
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Optional

from ai_os.analyzer.call_graph_builder import CallGraphBuilder
from ai_os.core import planner
from ai_os.core.lock_manager import LockManager
from ai_os.core.models import TaskNode
from ai_os.core.scheduler import Assignment, DynamicScheduler
from ai_os.core.staging import GitStagingEngine
from ai_os.core.task_runner import (
    AgentTurnExecutor,
    TaskRunner,
    TaskRunResult,
    build_claude_cli_agent_turn_executor,
    build_completion_agent_turn_executor,
    build_tool_calling_agent_turn_executor,
)
from ai_os.core.scheduling_policy import BudgetExceededError, SchedulingPolicy
from ai_os.knowledge.graph_engine import KnowledgeEngine
from ai_os.mcp.adapters.base_adapter import BaseMCPAdapter
from ai_os.sandbox.container_runner import EphemeralSandboxRunner

if TYPE_CHECKING:
    from ai_os.core.persistence import Persistence


@dataclass
class EpicRunResult:
    completed: list[str] = field(default_factory=list)
    blocked: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    task_results: dict[str, TaskRunResult] = field(default_factory=dict)
    # (task_id, provider, model|None) for each task actually started — lets the
    # CLI show/what confirm which model each task was routed to.
    assignments: dict[str, Assignment] = field(default_factory=dict)
    # The epic's DB id when persistence is enabled (else None) — lets the CLI
    # point `ai-os cost --epic <id>` at the run that just finished.
    epic_id: Optional[str] = None


class EpicRunner:
    def __init__(
        self,
        repo_root: Path,
        scheduler: DynamicScheduler,
        adapters: dict[str, BaseMCPAdapter],
        language: str,
        sandbox_runner=None,
        on_status_change: Optional[Callable[[str, str], None]] = None,
        persistence: Optional["Persistence"] = None,
        scheduling_policy: Optional[SchedulingPolicy] = None,
    ) -> None:
        self.repo_root = Path(repo_root)
        self.scheduler = scheduler
        self.adapters = adapters
        self.language = language
        self.sandbox_runner = sandbox_runner
        self.on_status_change = on_status_change
        # Optional accounting (Stage 3). When set, the epic + a row per task are
        # created up front (the audit tables FK to them), and each TaskRunner
        # records token cost + lock audit + status against those rows.
        self.persistence = persistence
        # Optional adaptive scheduling (Stage 4). When set, each task's agent
        # turn is wrapped with rate-limit backoff + provider fallback, and (if a
        # budget is configured) the epic aborts once spend hits the cap.
        self.scheduling_policy = scheduling_policy
        # Shared across every task in the epic — this is what makes Phase 2's
        # locking/merge serialization actually apply across concurrent tasks.
        self.lock_manager = LockManager()
        self.staging = GitStagingEngine(self.repo_root)

    def _build_engine(self) -> KnowledgeEngine:
        scan = CallGraphBuilder().scan(self.repo_root)
        engine = KnowledgeEngine()
        engine.build_from_scan(scan)
        return engine

    def plan_assignments(self, tasks: list[TaskNode]) -> dict[str, Assignment]:
        """Resolve every task's (provider, model) up front — used by the CLI's
        plan-review table so the human sees the routing before approving."""
        return {t.id: self.scheduler.assign(t.risk_level) for t in tasks}

    def _build_executor(
        self, task: TaskNode, assignment: Assignment, engine: KnowledgeEngine, graph_json_path: Path
    ) -> AgentTurnExecutor:
        # No adaptive policy -> a single executor bound to the primary
        # assignment, exactly as before Stage 4.
        if self.scheduling_policy is None:
            return self._build_single_executor(task, assignment, engine, graph_json_path)

        # With a policy: build one executor per configured provider for this
        # task's risk (in preference order, primary first) and compose them into
        # a fallback chain wrapped in rate-limit backoff. On persistent
        # rate-limiting from one provider, the next takes over — token accounting
        # stays correct because each executor reports the provider that actually
        # ran.
        assignments = self.scheduler.assignments_in_order(task.risk_level) or [assignment]
        attempts_builders = [
            (a.provider, self._build_single_executor(task, a, engine, graph_json_path))
            for a in assignments
        ]
        policy = self.scheduling_policy

        async def composite(ctx):
            return await policy.run_over_providers(
                [(prov, (lambda ex=ex: ex(ctx))) for prov, ex in attempts_builders]
            )

        return composite

    def _build_single_executor(
        self, task: TaskNode, assignment: Assignment, engine: KnowledgeEngine, graph_json_path: Path
    ) -> AgentTurnExecutor:
        adapter = self.adapters[assignment.provider]
        # Three execution paths, most-specific first:
        # 1. Anthropic CLI-session — the `claude` CLI owns its own MCP tool loop
        #    (spawned as a subprocess via --mcp-config).
        if assignment.provider == "anthropic" and getattr(adapter, "use_cli_session", False):
            return build_claude_cli_agent_turn_executor(
                repo_root=self.repo_root,
                graph_json_path=graph_json_path,
                sandbox_language=self.language,
                model=assignment.model or "claude-sonnet-4-5",
                claude_cli=getattr(adapter, "claude_cli", "claude"),
            )
        # 2. Any HTTP adapter with a native tool-calling loop (Gemini,
        #    OpenRouter, Anthropic API-key) — real autonomous tool use through
        #    the provider's own function-calling API, reusing the MCP tools.
        if adapter.supports_tool_calling():
            return build_tool_calling_agent_turn_executor(
                adapter=adapter,
                model=assignment.model,
                knowledge_engine=engine,
                sandbox_runner=self.sandbox_runner or EphemeralSandboxRunner(),
                sandbox_language=self.language,
            )
        # 3. Fallback — completion write-back (model returns whole files, AI-OS
        #    writes them). For adapters without a tool-calling loop.
        return build_completion_agent_turn_executor(adapter, model=assignment.model)

    async def _run_one(
        self,
        task: TaskNode,
        assignment: Assignment,
        engine: KnowledgeEngine,
        graph_json_path: Path,
        epic_id: Optional[str],
    ) -> TaskRunResult:
        executor = self._build_executor(task, assignment, engine, graph_json_path)
        runner = TaskRunner(
            lock_manager=self.lock_manager,
            staging=self.staging,
            knowledge_engine=engine,
            agent_turn_executor=executor,
            sandbox_runner=self.sandbox_runner,
            on_status_change=self.on_status_change,
            persistence=self.persistence,
            epic_id=epic_id,
        )
        return await runner.run_task(task, language=self.language)

    async def _epic_over_budget(self, epic_id: Optional[str]) -> bool:
        """True if a cost cap is configured and the epic's recorded spend has
        reached it. Needs persistence (the spend source) + a budget in the
        policy; otherwise always False."""
        if (
            self.scheduling_policy is None
            or self.scheduling_policy.budget_usd is None
            or self.persistence is None
            or epic_id is None
        ):
            return False
        spent = await self.persistence.epic_total_usd(epic_id)
        try:
            self.scheduling_policy.check_budget(spent)
        except BudgetExceededError:
            return True
        return False

    async def run_epic(
        self, tasks: list[TaskNode], epic_title: str = "epic", raw_prompt: str = ""
    ) -> EpicRunResult:
        result = EpicRunResult()
        result.assignments = self.plan_assignments(tasks)

        # Accounting (Stage 3): create the epic + a row per task BEFORE running,
        # since the token_cost/lock_audit tables FK to tasks -> epics.
        epic_id: Optional[str] = None
        if self.persistence is not None:
            epic_id = uuid.uuid4().hex
            result.epic_id = epic_id
            await self.persistence.create_epic(epic_id, epic_title, raw_prompt, status="RUNNING")
            for task in tasks:
                await self.persistence.upsert_task(
                    task, epic_id, assigned_model=result.assignments[task.id].model, status="PENDING"
                )

        return await self._execute_batches(tasks, result, epic_id)

    async def resume_epic(self, epic_id: str) -> EpicRunResult:
        """Resume a crashed/interrupted epic: reload its tasks + statuses from
        the accounting DB, treat the already-COMPLETED ones as done (their code
        is already merged to `main`), and run only the rest — respecting the DAG.

        Requires persistence (that's where the crash-surviving state lives). The
        per-task worktree policy stays "start fresh" (a half-done worktree from
        the crashed run is discarded and its task re-run cleanly); resume is the
        EPIC-level guarantee that completed work isn't redone.
        """
        if self.persistence is None:
            raise ValueError("resume_epic requires a persistence-backed EpicRunner")
        epic = await self.persistence.get_epic(epic_id)
        if epic is None:
            raise ValueError(f"No epic with id {epic_id!r} in the accounting DB")
        loaded = await self.persistence.load_epic_tasks(epic_id)
        if not loaded:
            raise ValueError(f"Epic {epic_id!r} has no recorded tasks to resume")

        tasks = [node for node, _status in loaded]
        result = EpicRunResult()
        result.epic_id = epic_id
        result.assignments = self.plan_assignments(tasks)
        # Pre-seed the completed set from the DB so already-done tasks are not
        # re-run and their dependents can proceed against the merged code.
        result.completed = [node.id for node, status in loaded if status == "COMPLETED"]

        await self.persistence.set_epic_status(epic_id, "RUNNING")
        return await self._execute_batches(tasks, result, epic_id)

    async def _execute_batches(
        self, tasks: list[TaskNode], result: EpicRunResult, epic_id: Optional[str]
    ) -> EpicRunResult:
        """The shared batch-execution core for both `run_epic` and `resume_epic`.
        Any task id already present in `result.completed` on entry (resume's
        pre-seed) is treated as done: not re-run, but counted as satisfying its
        dependents' dependencies."""
        graph = planner.build_graph(tasks)
        planner.validate_acyclic(graph)
        batches = planner.topological_batches(graph)
        by_id = {t.id: t for t in tasks}
        already_done = set(result.completed)

        budget_hit = False
        for batch in batches:
            # Cost cap (Stage 4): before starting a batch, if the epic's spend so
            # far has hit the budget, skip every remaining task and stop.
            if await self._epic_over_budget(epic_id):
                budget_hit = True
                break

            engine = self._build_engine()  # fresh scan: later batches see earlier merges
            with tempfile.TemporaryDirectory(prefix="ai-os-epic-graph-") as tmp_dir:
                graph_json_path = Path(tmp_dir) / "graph.json"
                engine.to_json(graph_json_path)

                runnable: list[TaskNode] = []
                for task_id in batch:
                    if task_id in already_done:
                        continue  # resume: already COMPLETED in a prior run — skip
                    task = by_id[task_id]
                    deps_ok = all(dep in result.completed for dep in task.dependencies)
                    if deps_ok:
                        runnable.append(task)
                    else:
                        # A dependency was blocked or itself skipped — this task
                        # can't run. Skip it (and, transitively, its own
                        # dependents in later batches, via the same check).
                        result.skipped.append(task_id)

                if not runnable:
                    continue

                run_results = await asyncio.gather(
                    *(
                        self._run_one(task, result.assignments[task.id], engine, graph_json_path, epic_id)
                        for task in runnable
                    )
                )

            for task, run_result in zip(runnable, run_results):
                result.task_results[task.id] = run_result
                if run_result.status == "COMPLETED":
                    result.completed.append(task.id)
                else:
                    result.blocked.append(task.id)

        if budget_hit:
            # Everything not already terminal is skipped due to the cost cap.
            for t in tasks:
                if t.id not in result.completed and t.id not in result.blocked and t.id not in result.skipped:
                    result.skipped.append(t.id)

        if self.persistence is not None and epic_id is not None:
            final_status = "COMPLETED" if not result.blocked and not result.skipped else "FAILED"
            await self.persistence.set_epic_status(epic_id, final_status)

        return result
