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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

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
from ai_os.knowledge.graph_engine import KnowledgeEngine
from ai_os.mcp.adapters.base_adapter import BaseMCPAdapter
from ai_os.sandbox.container_runner import EphemeralSandboxRunner


@dataclass
class EpicRunResult:
    completed: list[str] = field(default_factory=list)
    blocked: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    task_results: dict[str, TaskRunResult] = field(default_factory=dict)
    # (task_id, provider, model|None) for each task actually started — lets the
    # CLI show/what confirm which model each task was routed to.
    assignments: dict[str, Assignment] = field(default_factory=dict)


class EpicRunner:
    def __init__(
        self,
        repo_root: Path,
        scheduler: DynamicScheduler,
        adapters: dict[str, BaseMCPAdapter],
        language: str,
        sandbox_runner=None,
        on_status_change: Optional[Callable[[str, str], None]] = None,
    ) -> None:
        self.repo_root = Path(repo_root)
        self.scheduler = scheduler
        self.adapters = adapters
        self.language = language
        self.sandbox_runner = sandbox_runner
        self.on_status_change = on_status_change
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
        self, task: TaskNode, assignment: Assignment, engine: KnowledgeEngine, graph_json_path: Path
    ) -> TaskRunResult:
        executor = self._build_executor(task, assignment, engine, graph_json_path)
        runner = TaskRunner(
            lock_manager=self.lock_manager,
            staging=self.staging,
            knowledge_engine=engine,
            agent_turn_executor=executor,
            sandbox_runner=self.sandbox_runner,
            on_status_change=self.on_status_change,
        )
        return await runner.run_task(task, language=self.language)

    async def run_epic(self, tasks: list[TaskNode]) -> EpicRunResult:
        graph = planner.build_graph(tasks)
        planner.validate_acyclic(graph)
        batches = planner.topological_batches(graph)
        by_id = {t.id: t for t in tasks}

        result = EpicRunResult()
        result.assignments = self.plan_assignments(tasks)

        for batch in batches:
            engine = self._build_engine()  # fresh scan: later batches see earlier merges
            with tempfile.TemporaryDirectory(prefix="ai-os-epic-graph-") as tmp_dir:
                graph_json_path = Path(tmp_dir) / "graph.json"
                engine.to_json(graph_json_path)

                runnable: list[TaskNode] = []
                for task_id in batch:
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
                        self._run_one(task, result.assignments[task.id], engine, graph_json_path)
                        for task in runnable
                    )
                )

            for task, run_result in zip(runnable, run_results):
                result.task_results[task.id] = run_result
                if run_result.status == "COMPLETED":
                    result.completed.append(task.id)
                else:
                    result.blocked.append(task.id)

        return result
