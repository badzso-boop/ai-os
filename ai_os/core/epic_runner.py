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
import shutil
import tempfile
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Optional

from ai_os.analyzer.call_graph_builder import CallGraphBuilder
from ai_os.analyzer.languages import detect_language
from ai_os.core import planner
from ai_os.core.lock_manager import LockManager
from ai_os.core.models import TaskNode
from ai_os.core.scheduler import Assignment, DynamicScheduler
from ai_os.core.staging import GitStagingEngine
from ai_os.core.task_runner import (
    AgentTurnExecutor,
    TaskRunner,
    TaskRunResult,
    _extract_critic_verdict as _critic_verdict,
    build_claude_cli_agent_turn_executor,
    build_completion_agent_turn_executor,
    build_tool_calling_agent_turn_executor,
)
from ai_os.core.conventions import load_project_conventions
from ai_os.core.scheduling_policy import BudgetExceededError, SchedulingPolicy
from ai_os.knowledge.graph_engine import KnowledgeEngine
from ai_os.mcp.adapters.base_adapter import BaseMCPAdapter
from ai_os.sandbox.container_runner import SANDBOX_PROFILES, EphemeralSandboxRunner

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
    # Set in PR mode when a pull request was opened (else None). The integration
    # branch that gathered the epic's work (whether a PR was opened or it was
    # merged to main) is `integration_branch`.
    pull_request_url: Optional[str] = None
    integration_branch: Optional[str] = None
    # True if PR mode fell back to a local merge-to-main (no git remote / no gh).
    merged_to_main: bool = False
    # The original high-level prompt (for the PR title/body); empty on resume.
    raw_prompt: str = ""


def resolve_task_language(task: TaskNode, default_language: str) -> str:
    """The language a task validates as. Explicit `task.language` wins if it's
    one the sandbox actually has a profile for; else it's derived from the
    task's file extensions (majority vote across write_set + target_files,
    counting ONLY sandbox-supported languages); else the epic's default. This
    is what lets ONE epic span a Python backend + a TypeScript frontend — each
    task gets its own sandbox profile.

    `detect_language` (the Phase-1 analyzer) recognizes more languages (sql,
    html, css, ...) than `SANDBOX_PROFILES` has validation profiles for
    (java/javascript/python/typescript) — e.g. a schema-migration task whose
    only recognized-extension file is a `.sql` migration would otherwise "win"
    the vote with a language the sandbox can't run at all, crashing the whole
    epic run with SandboxLanguageNotSupportedError instead of just validating
    as the epic's actual (default) language. Votes for an unsupported language
    are dropped rather than counted, and an explicit-but-unsupported
    `task.language` is treated the same as if none were set.
    """
    if task.language and task.language in SANDBOX_PROFILES:
        return task.language
    from pathlib import Path as _P

    votes: dict[str, int] = {}
    for path in list(task.write_set) + list(task.target_files):
        lang = detect_language(_P(path))
        if lang is not None and lang in SANDBOX_PROFILES:
            votes[lang] = votes.get(lang, 0) + 1
    if votes:
        return max(votes, key=votes.get)
    return default_language


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
        create_pr: bool = True,
        pr_base_branch: str = "main",
        on_event: Optional[Callable[[dict], None]] = None,
        summarizer: Optional[Callable] = None,
        test_critic: Optional[Callable] = None,
    ) -> None:
        self.repo_root = Path(repo_root)
        self.scheduler = scheduler
        self.adapters = adapters
        self.language = language
        self.sandbox_runner = sandbox_runner
        self.on_status_change = on_status_change
        # PR mode (default): gather the epic's tasks on a per-epic integration
        # branch and open ONE pull request (base = pr_base_branch) at the end,
        # instead of merging each task straight to main. `create_pr=False`
        # (CLI `--merge-to-main`) restores the direct merge-to-main behavior.
        # If PR mode can't reach a git remote or `gh`, it falls back to a local
        # merge into pr_base_branch (so nothing is lost).
        self.create_pr = create_pr
        self.pr_base_branch = pr_base_branch
        # Structured observability hook threaded into every TaskRunner (see
        # TaskRunner.on_event) so the CLI can show what each task is doing.
        self.on_event = on_event
        # Repo-side `.ai-os/conventions.md`, injected into every task's prompt so
        # all providers follow the project's rules (i18n, UI library, …).
        self.project_conventions = load_project_conventions(self.repo_root)
        # Optional cheap-model summarizer for large validation failure logs.
        self.summarizer = summarizer
        # Optional cheap-model test critic (Phase 6, feature 1c): judges test
        # quality on each COMPLETED task's diff. Advisory — surfaced in the PR
        # body for the human reviewer, never blocks a merge.
        self.test_critic = test_critic
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
        language = resolve_task_language(task, self.language)
        # Three execution paths, most-specific first:
        # 1. Anthropic CLI-session — the `claude` CLI owns its own MCP tool loop
        #    (spawned as a subprocess via --mcp-config).
        if assignment.provider == "anthropic" and getattr(adapter, "use_cli_session", False):
            return build_claude_cli_agent_turn_executor(
                repo_root=self.repo_root,
                graph_json_path=graph_json_path,
                sandbox_language=language,
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
                sandbox_language=language,
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
            on_event=self.on_event,
            project_conventions=self.project_conventions,
            summarize_output=self.summarizer,
            test_critic=self.test_critic,
        )
        return await runner.run_task(task, language=resolve_task_language(task, self.language))

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
        result.raw_prompt = raw_prompt

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
        # reuse_branch=True: continue on the crashed run's integration branch
        # (which holds the completed tasks), instead of resetting it to main.
        return await self._execute_batches(tasks, result, epic_id, reuse_branch=True)

    async def _execute_batches(
        self, tasks: list[TaskNode], result: EpicRunResult, epic_id: Optional[str],
        reuse_branch: bool = False,
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

        # PR mode: gather the epic's work on a per-epic integration branch (tasks
        # merge into it, not straight to main), so one PR can be opened at the
        # end. In merge-to-main mode, staging keeps its default base ("main").
        if self.create_pr:
            branch = f"ai-os/epic-{epic_id or uuid.uuid4().hex[:12]}"
            await self.staging.create_integration_branch(
                branch, from_ref=self.pr_base_branch, reuse_existing=reuse_branch
            )
            self.staging.base_branch = branch
            result.integration_branch = branch

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

        # PR mode finalize: open one PR from the integration branch, or (no
        # remote / no gh) fall back to a local merge into the base branch.
        if self.create_pr and result.integration_branch and result.completed:
            await self._finalize_pr(result, tasks)

        if self.persistence is not None and epic_id is not None:
            final_status = "COMPLETED" if not result.blocked and not result.skipped else "FAILED"
            await self.persistence.set_epic_status(epic_id, final_status)

        return result

    def _build_quality_section(self, result: EpicRunResult) -> list[str]:
        """The '🔎 Validator quality' block of the PR body — a per-task readout of
        the advisory checks (test-presence + CI/sensitive flags + the cheap-model
        critic's verdict), so the human reviewer knows where to look hardest.
        Returns [] when nothing noteworthy surfaced (all tasks tested + strong)."""
        rows: list[str] = []
        for tid in result.completed:
            tr = result.task_results.get(tid)
            if tr is None:
                continue
            presence = tr.test_presence
            flags: list[str] = []
            if presence is not None and presence.missing_tests:
                flags.append("⚠️ code changed but **no test** was added")
            if presence is not None and presence.sensitive_files:
                flags.append(
                    "🔐 touches CI/sensitive config (a green result here is **not "
                    "self-certifying** — review the diff): "
                    + ", ".join(f"`{p}`" for p in presence.sensitive_files)
                )
            if tr.test_critique:
                verdict = _critic_verdict(tr.test_critique)
                if verdict in {"WEAK", "MISSING", "UNKNOWN"}:
                    icon = {"WEAK": "⚠️", "MISSING": "⛔", "UNKNOWN": "❔"}[verdict]
                    first = tr.test_critique.strip().splitlines()
                    detail = " ".join(ln.strip() for ln in first[1:4]).strip()
                    flags.append(f"{icon} test critic: **{verdict}** — {detail or '(see run log)'}")
            if flags:
                rows.append(f"- **{tid}**: " + "; ".join(flags))
        if not rows:
            return []
        return [
            "",
            "### 🔎 Validator quality (advisory — the sandbox is the pass/fail authority)",
            "These completed tasks warrant a closer human look:",
            "",
            *rows,
        ]

    async def _finalize_pr(self, result: EpicRunResult, tasks: list[TaskNode]) -> None:
        """Open a PR from the integration branch (base = pr_base_branch), or fall
        back to a local ff-merge into pr_base_branch when there's no git remote
        or no `gh` CLI — so the work is never lost, just not PR'd. The PR body
        includes the decomposed DAG (what AI-OS planned and did)."""
        branch = result.integration_branch
        gh_available = shutil.which("gh") is not None
        has_remote = await self.staging.has_remote()

        if gh_available and has_remote:
            title, body = self._build_pr_content(result, tasks)
            try:
                await self.staging.push_branch(branch)
                # Push BLOCKED task branches too, so their failing code is
                # inspectable on the remote (referenced in the PR body).
                for tid in result.blocked:
                    try:
                        await self.staging.push_branch(self.staging.task_branch_name(tid))
                    except Exception:
                        pass  # best-effort — the branch still exists locally
                result.pull_request_url = await self.staging.open_pull_request(
                    head=branch, base=self.pr_base_branch, title=title, body=body
                )
                return
            except Exception:
                # PR creation failed (auth, network, existing PR, …) — don't lose
                # the work; fall through to the local merge below.
                pass

        # Fallback: merge the integration branch into the base branch locally.
        await self.staging.merge_branch_ff(branch, target=self.pr_base_branch)
        result.merged_to_main = True

    def _build_pr_content(self, result: EpicRunResult, tasks: list[TaskNode]) -> tuple[str, str]:
        """Build the PR title + Markdown body — including the decomposed DAG as a
        table (id, title, risk, routed model, dependencies, per-task outcome) so a
        reviewer can see exactly what AI-OS planned and did."""
        prompt = (result.raw_prompt or "").strip()
        first_line = prompt.splitlines()[0] if prompt else f"{len(result.completed)} task(s)"
        title = f"AI-OS: {first_line[:100]}"

        status_by_id: dict[str, str] = {}
        for tid in result.completed:
            status_by_id[tid] = "✅ merged"
        for tid in result.blocked:
            status_by_id[tid] = "⛔ blocked"
        for tid in result.skipped:
            status_by_id[tid] = "⏭️ skipped"

        lines = ["## What AI-OS did", ""]
        if prompt:
            lines += ["> " + prompt.replace("\n", "\n> "), ""]
        lines += [
            "**Execution plan (decomposed DAG):**",
            "",
            "| Task | Title | Risk | Routed to | Depends on | Result |",
            "| --- | --- | --- | --- | --- | --- |",
        ]
        for task in tasks:
            assignment = result.assignments.get(task.id)
            routed = "—"
            if assignment is not None:
                routed = f"{assignment.provider} → {assignment.model or 'default'}"
            deps = ", ".join(task.dependencies) or "—"
            outcome = status_by_id.get(task.id, "—")
            title_cell = task.title.replace("|", "\\|")
            lines.append(f"| {task.id} | {title_cell} | {task.risk_level} | {routed} | {deps} | {outcome} |")

        if result.blocked:
            lines += ["", "### ⛔ Blocked tasks (validation failed — code kept for inspection)"]
            for tid in result.blocked:
                branch = self.staging.task_branch_name(tid)
                task_result = result.task_results.get(tid)
                out = ((task_result.final_output if task_result else "") or "").strip()
                tail = "\n".join(out.splitlines()[-15:]) or "(no validation output captured)"
                lines += ["", f"**{tid}** — inspect branch `{branch}`. Last validation output:", "```", tail, "```"]

        # Validator-quality section (Phase 6): surface, for the human reviewer,
        # where a completed task shipped code without tests, touched CI/sensitive
        # config, or drew a weak/missing verdict from the cheap-model test critic.
        quality_lines = self._build_quality_section(result)
        if quality_lines:
            lines += quality_lines

        lines += [
            "",
            f"**Completed:** {len(result.completed)}  ·  "
            f"**Blocked:** {len(result.blocked)}  ·  "
            f"**Skipped:** {len(result.skipped)}",
        ]
        if result.epic_id:
            lines += ["", f"<sub>AI-OS epic `{result.epic_id}` — see `ai-os cost --epic {result.epic_id}` for spend.</sub>"]
        lines += ["", "🤖 Generated by [AI-OS](https://github.com/badzso-boop/ai-os)."]
        return title, "\n".join(lines)
