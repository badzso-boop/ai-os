"""Orchestration loop for running one `TaskNode` end-to-end (Phase 3b).

Not listed in `docs/14_PROJECT_DIRECTORY_STRUCTURE.md`'s directory tree — a
deliberate addition, same treatment as Phase 2's `core/models.py` deviation:
this is the glue tying Phase 1 (`KnowledgeEngine`) + Phase 2 (`LockManager`,
`GitStagingEngine`) + Phase 3 (`EphemeralSandboxRunner`, `mcp_server.py`)
together for a single task: acquire locks -> create a worktree -> build a
Context Cache -> run the agent -> validate in the sandbox -> retry on
failure up to `task.max_retries` -> merge on success, or abandon the
worktree and report `BLOCKED` (HITL escalation, doc 16) on exhaustion.

The actual "let an LLM work on the task" step is the injectable
`AgentTurnExecutor` callable, not hardcoded — production code
(`build_claude_cli_agent_turn_executor`, below) shells out to `claude -p ...
--mcp-config ...` so the model can call the MCP tools (`propose_file_patch`,
`fetch_symbol_definition`, `trigger_sandbox_validation`) against this task's
worktree. Automated tests in `tests/test_task_runner.py` inject a fake
executor that deterministically simulates N failed attempts then a fix
(writing directly into the worktree, as a real successful turn would) —
proving the retry/HITL bookkeeping works correctly without any live model.
Per explicit instruction, no real `claude` CLI invocation or live LLM call
is made anywhere in this module's own test suite; `build_claude_cli_agent_turn_executor`
is built and ready, but its first real run is a deliberately manual,
human-run step (see CLAUDE.md).
"""
from __future__ import annotations

import asyncio
import json
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Awaitable, Callable, Optional

from ai_os.core.conventions import conventions_block
from ai_os.core.lock_manager import LockManager
from ai_os.core.models import TaskNode
from ai_os.core.staging import GitStagingEngine, ValidationCallbackError
from ai_os.core.test_quality import TestPresenceReport, assess_test_presence
from ai_os.knowledge.graph_engine import KnowledgeEngine
from ai_os.mcp.adapters.base_adapter import (
    BaseMCPAdapter,
    LLMTaskRequest,
    TokenUsage,
    ToolSpec,
)
from ai_os.sandbox.container_runner import EphemeralSandboxRunner

if TYPE_CHECKING:
    from ai_os.core.persistence import Persistence


@dataclass
class AgentTurnContext:
    """Everything one "agent turn" needs to know. `previous_validation_output`
    is `None` on the first attempt and the sandbox's (ANSI-clean) output from
    the prior attempt on every retry — this is the "prompt feedback loop"
    doc 05/16 describe: the model sees exactly what broke last time.
    """

    task: TaskNode
    worktree_path: Path
    context_cache: str
    attempt: int  # 1-based
    previous_validation_output: str | None
    # Repo-side `.ai-os/conventions.md` (or ""), injected into every executor's
    # prompt so all providers follow the project's rules (i18n, UI library, …).
    project_conventions: str = ""


@dataclass
class TaskRunResult:
    task_id: str
    status: str  # "COMPLETED" | "BLOCKED"
    attempts: int
    final_output: str | None = None
    # Validator-quality signals for a COMPLETED task (Phase 6). `test_presence`
    # is the deterministic classification of what the task changed (source vs
    # test); `test_critique` is the optional cheap-model judgement on test
    # quality. Both are advisory — surfaced to the human in the CLI + PR body,
    # never a hard block (the sandbox is the pass/fail authority).
    test_presence: Optional[TestPresenceReport] = None
    test_critique: Optional[str] = None
    changed_files: list[str] = field(default_factory=list)


@dataclass
class AgentTurnUsage:
    """What one agent turn cost, for accounting (Stage 3). Returned by an
    `AgentTurnExecutor` so `TaskRunner` can persist a `TokenCostModel` row per
    turn. `None` from an executor means "no usage to record" (e.g. a test fake,
    or a path that can't report usage) — accounting is skipped, not an error."""

    provider: str
    model_name: str
    usage: TokenUsage


AgentTurnExecutor = Callable[[AgentTurnContext], Awaitable[Optional["AgentTurnUsage"]]]
"""Perform one agent turn against `ctx.worktree_path` and return the turn's
usage (or `None` if it has none to report). Raising signals an infra fault in
the turn itself (e.g. the CLI subprocess crashed) — distinct from "the turn
completed but the code still fails validation", which is the normal, expected
retry path and is never an exception here.
"""


class AgentUsageLimitError(RuntimeError):
    """The agent hit a usage/rate limit (e.g. the Claude subscription's 5-hour
    cap) — as opposed to a transient crash. `TaskRunner` blocks the task
    immediately on this (no retry: the limit won't reset in-run) so the epic can
    still finalize the tasks that already completed, instead of the whole run
    dying with an unhandled exception."""


# Substrings that mark a usage/rate-limit failure in the `claude` CLI's output.
_USAGE_LIMIT_MARKERS = ("usage limit", "rate limit", "resets", "quota", "429", "too many requests")


class TaskRunner:
    """Runs one `TaskNode` to completion or exhaustion. One instance can be
    reused across tasks (it holds no per-task state itself).
    """

    def __init__(
        self,
        lock_manager: LockManager,
        staging: GitStagingEngine,
        knowledge_engine: KnowledgeEngine,
        agent_turn_executor: AgentTurnExecutor,
        sandbox_runner: Optional[EphemeralSandboxRunner] = None,
        on_status_change: Optional[Callable[[str, str], None]] = None,
        persistence: Optional["Persistence"] = None,
        epic_id: Optional[str] = None,
        on_event: Optional[Callable[[dict], None]] = None,
        project_conventions: str = "",
        summarize_output: Optional[Callable[[str], Awaitable[str]]] = None,
        summarize_threshold: int = 2000,
        test_critic: Optional[Callable[[str], Awaitable[str]]] = None,
        assess_test_quality: bool = True,
        triage_agent: Optional[Callable[[str, str], Awaitable[str]]] = None,
        max_triage_retries: int = 2,
    ) -> None:
        # Optional cheap-model summarizer for large validation failure logs — see
        # `build_output_summarizer`. Only invoked when the output exceeds
        # `summarize_threshold` chars, so short errors skip the extra call.
        self.summarize_output = summarize_output
        self.summarize_threshold = summarize_threshold
        # Validator-quality checks (Phase 6). `assess_test_quality` (default on)
        # runs the zero-LLM test-presence classification on a COMPLETED task's
        # real diff; `test_critic` (optional, cheap-model) adds a judgement on
        # whether the tests meaningfully exercise the change. Both are advisory.
        self.test_critic = test_critic
        self.assess_test_quality = assess_test_quality
        self.project_conventions = project_conventions
        self.lock_manager = lock_manager
        self.staging = staging
        self.knowledge_engine = knowledge_engine
        self.agent_turn_executor = agent_turn_executor
        self.sandbox_runner = sandbox_runner or EphemeralSandboxRunner()
        # 2-Tier Triage & Self-Healing: cheap LOW-model triage agent to analyze
        # failures/conflicts and provide actionable recommendations back to the main agent.
        self.triage_agent = triage_agent
        self.max_triage_retries = max_triage_retries
        # Optional structured observability hook: receives dict events describing
        # what's happening inside a task run (attempt start, agent turn + model,
        # sandbox validation result, merge outcome, retry). The CLI subscribes to
        # this to make the run debuggable instead of a black box. Never raises
        # into the run (a bad printer must not abort a generation).
        self.on_event = on_event
        # Optional, deliberately thin status-transition hook (task_id, status)
        # -> None. Kept as a plain callback rather than a direct SQLAlchemy
        # dependency so this module doesn't need to assume a TaskModel row
        # already exists (Phase 2's schema requires a non-null epic_id FK) —
        # callers who want DB persistence wire this to their own upsert.
        self.on_status_change = on_status_change
        # Optional accounting (Stage 3). When `persistence` is injected (only
        # `EpicRunner` does, AFTER creating the epic+task rows the audit tables
        # FK to), TaskRunner records a TokenCostModel row per agent turn, a
        # LockAuditModel row per lock acquire/release, and mirrors status
        # changes into the TaskModel row. All best-effort: a DB error here logs
        # and is swallowed, never aborting a real generation run.
        self.persistence = persistence
        self.epic_id = epic_id

    async def _report_status(self, task_id: str, status: str) -> None:
        if self.on_status_change is not None:
            self.on_status_change(task_id, status)
        if self.persistence is not None:
            await self._safe_persist(
                self.persistence.update_task_status(task_id, status, epic_id=self.epic_id)
            )

    def _emit(self, **event) -> None:
        """Fire a structured observability event, swallowing any printer error
        (observability must never break a run)."""
        if self.on_event is None:
            return
        try:
            self.on_event(event)
        except Exception:  # pragma: no cover - defensive
            pass

    @staticmethod
    async def _safe_persist(coro: Awaitable[None]) -> None:
        """Await a persistence write, swallowing (but reporting) any error so
        accounting never breaks a real run."""
        try:
            await coro
        except Exception as exc:  # pragma: no cover - defensive best-effort
            import sys

            print(f"[ai-os] persistence write failed (ignored): {exc}", file=sys.stderr)

    def _lock_audit_callback(self, task_id: str):
        """An async (filepath, lock_type, action) -> None callback for
        `LockManager.locks`, or `None` when no persistence is configured (so the
        lock manager skips auditing entirely)."""
        if self.persistence is None:
            return None

        async def audit(filepath: str, lock_type: str, action: str) -> None:
            await self._safe_persist(
                self.persistence.record_lock_audit(
                    task_id, filepath, lock_type, action, epic_id=self.epic_id
                )
            )

        return audit

    async def _assess_validator_quality(
        self, task: TaskNode, changed_files: list[str], diff: str
    ) -> tuple[Optional[TestPresenceReport], Optional[str]]:
        """Run the advisory validator-quality checks on a COMPLETED task's real
        diff: the deterministic test-presence classification (always, cheap) and
        the optional cheap-model test critic. Emits observability events and
        returns `(presence_report, critique_text)`. Never raises into the run —
        a check failure just yields `None` for that signal."""
        presence: Optional[TestPresenceReport] = None
        critique: Optional[str] = None
        if self.assess_test_quality:
            try:
                presence = assess_test_presence(changed_files)
                self._emit(
                    type="test_quality", task_id=task.id,
                    source_files=list(presence.source_files),
                    test_files=list(presence.test_files),
                    sensitive_files=list(presence.sensitive_files),
                    missing_tests=presence.missing_tests,
                    summary=presence.summary_line(),
                )
            except Exception:
                presence = None
        if self.test_critic is not None and diff.strip():
            try:
                critique = await self.test_critic(diff[:_TEST_CRITIC_DIFF_CAP])
                if critique and critique.strip():
                    self._emit(
                        type="test_critique", task_id=task.id,
                        verdict=_extract_critic_verdict(critique), critique=critique.strip(),
                    )
                else:
                    critique = None
            except Exception:
                critique = None
        return presence, critique

    async def run_task(self, task: TaskNode, language: str, max_hops: int = 2) -> TaskRunResult:
        async with self.lock_manager.locks(
            task.id, task.read_set, task.write_set, audit=self._lock_audit_callback(task.id)
        ):
            await self._report_status(task.id, "RUNNING")
            worktree_path = await self.staging.create_worktree(task.id)
            context_cache = self.knowledge_engine.build_context_cache(
                task.target_files, max_hops=max_hops
            )

            last_output: str | None = None
            last_changed_files: list[str] = []
            last_diff: str = ""
            max_attempts = task.max_retries + 1
            attempt = 0
            for attempt in range(1, max_attempts + 1):
                self._emit(
                    type="attempt", task_id=task.id, attempt=attempt, max_attempts=max_attempts,
                    title=task.title, target_files=list(task.target_files),
                )
                turn_ctx = AgentTurnContext(
                    task=task,
                    worktree_path=worktree_path,
                    context_cache=context_cache,
                    attempt=attempt,
                    previous_validation_output=last_output,
                    project_conventions=self.project_conventions,
                )
                try:
                    turn_usage = await self.agent_turn_executor(turn_ctx)
                except AgentUsageLimitError as exc:
                    # A usage/rate limit (e.g. the Claude subscription's 5h cap):
                    # retrying won't help in-run. Emit + go straight to BLOCKED so
                    # the epic still finalizes the tasks that DID complete.
                    self._emit(type="agent_error", task_id=task.id, attempt=attempt, error=str(exc), fatal=True)
                    last_output = f"Agent unavailable (usage/rate limit): {exc}"
                    break
                except Exception as exc:
                    # Any other agent-turn infra crash: don't kill the whole epic
                    # — treat it as a failed attempt (retry), then BLOCK on exhaustion.
                    self._emit(type="agent_error", task_id=task.id, attempt=attempt, error=str(exc), fatal=False)
                    last_output = f"Agent turn failed: {exc}"
                    if attempt < max_attempts:
                        self._emit(type="retry", task_id=task.id, next_attempt=attempt + 1)
                    continue
                if turn_usage is not None:
                    self._emit(
                        type="agent_turn", task_id=task.id, attempt=attempt,
                        provider=turn_usage.provider, model=turn_usage.model_name,
                        input_tokens=turn_usage.usage.input_tokens,
                        output_tokens=turn_usage.usage.output_tokens,
                        usd=turn_usage.usage.estimated_usd_cost,
                    )
                if self.persistence is not None and turn_usage is not None:
                    await self._safe_persist(
                        self.persistence.record_token_cost(
                            task.id, turn_usage.provider, turn_usage.model_name, turn_usage.usage, epic_id=self.epic_id
                        )
                    )

                # Snapshot what the agent has changed in the worktree so far (for
                # the validator-quality checks if this attempt merges). Captured
                # BEFORE the merge tears the worktree down; best-effort.
                try:
                    last_changed_files, last_diff = await self.staging.worktree_changes(task.id)
                except Exception:
                    last_changed_files, last_diff = [], ""

                validator_ran = False

                async def validator(wt_path: Path) -> bool:
                    nonlocal validator_ran, last_output
                    validator_ran = True
                    result = await self.sandbox_runner.run_validation(wt_path, language)
                    last_output = result.output
                    self._emit(
                        type="validation", task_id=task.id, attempt=attempt,
                        success=result.success, exit_code=result.exit_code,
                        summary=result.summary, output=result.output,
                    )
                    return result.success

                try:
                    merged = await self.staging.stage_and_merge_task(
                        task.id, f"{task.id}: attempt {attempt}", validator
                    )
                except ValidationCallbackError as exc:
                    # The validator itself broke (infra fault, e.g. an
                    # unsupported sandbox language) rather than reporting a
                    # normal pass/fail — this is deterministic, so retrying
                    # the same task would just fail identically. Go straight
                    # to BLOCKED (same policy as AgentUsageLimitError above)
                    # instead of letting it crash the whole epic run and take
                    # down any sibling tasks running concurrently in this
                    # batch.
                    detail = str(exc.__cause__) if exc.__cause__ else str(exc)
                    self._emit(
                        type="agent_error", task_id=task.id, attempt=attempt,
                        error=detail, fatal=True,
                    )
                    last_output = f"Validation infra error: {detail}"
                    break
                if merged:
                    self._emit(type="merged", task_id=task.id, attempt=attempt)
                    await self._report_status(task.id, "COMPLETED")
                    presence, critique = await self._assess_validator_quality(
                        task, last_changed_files, last_diff
                    )
                    return TaskRunResult(
                        task_id=task.id, status="COMPLETED", attempts=attempt,
                        final_output=last_output, test_presence=presence,
                        test_critique=critique, changed_files=last_changed_files,
                    )
                if not validator_ran:
                    # stage_and_merge_task returned False before the validator
                    # ever ran - a rebase conflict against main, not a
                    # validation failure. Give the next attempt an honest
                    # signal instead of silently reusing stale output.
                    last_output = (
                        "Merge failed before validation ran (likely a rebase "
                        "conflict against the base branch)."
                    )
                    self._emit(
                        type="merge_conflict", task_id=task.id, attempt=attempt, output=last_output
                    )
                # Cheap-model summary of a LARGE failure log before the next
                # attempt's (expensive) model sees it — saves tokens/context. Only
                # kicks in past the threshold, so short errors are fed back as-is.
                if (
                    not merged and validator_ran and self.summarize_output is not None
                    and last_output and len(last_output) > self.summarize_threshold
                ):
                    try:
                        summary = await self.summarize_output(last_output)
                        if summary and summary.strip():
                            self._emit(
                                type="summarized", task_id=task.id, attempt=attempt,
                                original_len=len(last_output), summary_len=len(summary),
                            )
                            last_output = summary
                    except Exception:
                        pass  # keep the raw output if the summarizer fails
                if attempt < max_attempts:
                    self._emit(type="retry", task_id=task.id, next_attempt=attempt + 1)

            # 2-Tier Triage & Self-Healing Loop:
            # If standard attempts are exhausted or a fatal block occurred, but a triage_agent
            # is configured and max_triage_retries > 0, run cheap LOW-model triage analysis
            # and attempt self-healing turns with the main agent.
            if self.triage_agent is not None and self.max_triage_retries > 0 and last_output:
                triage_attempt = 0
                for triage_attempt in range(1, self.max_triage_retries + 1):
                    self._emit(
                        type="triage_analysis", task_id=task.id,
                        triage_attempt=triage_attempt, max_triage_retries=self.max_triage_retries,
                    )
                    try:
                        recommendation = await self.triage_agent(last_output, last_diff)
                    except Exception as exc:
                        self._emit(type="agent_error", task_id=task.id, attempt=attempt, error=f"Triage agent error: {exc}", fatal=False)
                        break

                    if not recommendation or not recommendation.strip():
                        break

                    self._emit(
                        type="triage_recommendation", task_id=task.id,
                        triage_attempt=triage_attempt, recommendation=recommendation.strip(),
                    )
                    triage_feedback = (
                        f"[TRIAGE AGENT ANALYSIS & RECOMMENDATION]\n"
                        f"The triage agent analyzed the failure and produced the following fix recommendation:\n"
                        f"{recommendation.strip()}\n\n"
                        f"[RAW FAILURE OUTPUT]\n{last_output}"
                    )

                    triage_turn_ctx = AgentTurnContext(
                        task=task,
                        worktree_path=worktree_path,
                        context_cache=context_cache,
                        attempt=attempt + triage_attempt,
                        previous_validation_output=triage_feedback,
                        project_conventions=self.project_conventions,
                    )

                    try:
                        turn_usage = await self.agent_turn_executor(triage_turn_ctx)
                    except Exception as exc:
                        self._emit(type="agent_error", task_id=task.id, attempt=attempt, error=f"Self-healing turn failed: {exc}", fatal=False)
                        break

                    if turn_usage is not None:
                        self._emit(
                            type="agent_turn", task_id=task.id, attempt=attempt + triage_attempt,
                            provider=turn_usage.provider, model=turn_usage.model_name,
                            input_tokens=turn_usage.usage.input_tokens,
                            output_tokens=turn_usage.usage.output_tokens,
                            usd=turn_usage.usage.estimated_usd_cost,
                        )

                    try:
                        last_changed_files, last_diff = await self.staging.worktree_changes(task.id)
                    except Exception:
                        pass

                    async def triage_validator(wt_path: Path) -> bool:
                        nonlocal last_output
                        result = await self.sandbox_runner.run_validation(wt_path, language)
                        last_output = result.output
                        self._emit(
                            type="validation", task_id=task.id, attempt=attempt + triage_attempt,
                            success=result.success, exit_code=result.exit_code,
                            summary=result.summary, output=result.output,
                        )
                        return result.success

                    try:
                        triage_merged = await self.staging.stage_and_merge_task(
                            task.id, f"{task.id}: triage attempt {triage_attempt}", triage_validator
                        )
                    except ValidationCallbackError as exc:
                        detail = str(exc.__cause__) if exc.__cause__ else str(exc)
                        last_output = f"Validation infra error during triage: {detail}"
                        break

                    if triage_merged:
                        self._emit(type="merged", task_id=task.id, attempt=attempt + triage_attempt, triage_healed=True)
                        await self._report_status(task.id, "COMPLETED")
                        presence, critique = await self._assess_validator_quality(
                            task, last_changed_files, last_diff
                        )
                        return TaskRunResult(
                            task_id=task.id, status="COMPLETED", attempts=attempt + triage_attempt,
                            final_output=last_output, test_presence=presence,
                            test_critique=critique, changed_files=last_changed_files,
                        )

            # BLOCKED: remove the worktree dir but KEEP the branch so the
            # failing code stays inspectable (git checkout ai-os/<task-id>, or a
            # pushed branch + the error in the PR body) — instead of discarding it.
            await self.staging.cleanup_worktree(task.id, keep_branch=True)
            await self._report_status(task.id, "BLOCKED")
            return TaskRunResult(
                task_id=task.id, status="BLOCKED", attempts=attempt, final_output=last_output
            )


SUMMARIZER_SYSTEM_PROMPT = (
    "You are a build/test log summarizer. Given raw compiler/test/linter output, "
    "produce a concise summary (at most ~15 lines) of what FAILED and the exact "
    "errors to fix. KEEP verbatim: file paths, line/column numbers, error codes, "
    "and the key error messages. DROP: progress bars, timing, stack-trace noise, "
    "passing lines. Output only the summary, no preamble."
)


def build_output_summarizer(
    adapter: BaseMCPAdapter, model: str | None = None
) -> Callable[[str], Awaitable[str]]:
    """Build a cheap-model summarizer for large validation failure logs: it
    compresses the raw output down to the actionable errors, so the (expensive)
    task model gets a tight diagnosis on retry instead of the whole log. Wired to
    a LOW-risk (cheap) provider+model by the CLI. Returns the raw output
    unchanged if the summary comes back empty."""

    async def summarize(output: str) -> str:
        request = LLMTaskRequest(
            task_id="summarize-validation",
            system_prompt=SUMMARIZER_SYSTEM_PROMPT,
            context_payload=output,
            model=model,
        )
        response = await adapter.execute_task(request)
        text = (response.generated_text or "").strip()
        return text or output

    return summarize


TRIAGE_AGENT_SYSTEM_PROMPT = (
    "You are a Senior Software & DevOps Triage Specialist. An automated task execution "
    "attempt hit a failure (Git merge conflict, validation failure, or compilation error).\n"
    "Your job is to diagnose the root cause and provide a clear, step-by-step recommendation "
    "for the primary coding agent on how to resolve the issue.\n\n"
    "Format your output as:\n"
    "1. DIAGNOSIS: (What went wrong, exact conflict or failing assertion)\n"
    "2. PROPOSED FIX: (Concrete steps and precise code/git changes recommended)"
)


def build_triage_agent(
    adapter: BaseMCPAdapter, model: str | None = None
) -> Callable[[str, str], Awaitable[str]]:
    """Build a cheap LOW-model triage agent for 2-tier self-healing: given the raw failure
    output and git diff, produces a structured diagnosis and actionable recommendation
    for the primary agent."""

    async def triage(failure_output: str, git_diff: str) -> str:
        payload = f"=== FAILURE OUTPUT ===\n{failure_output}\n\n=== RECENT GIT DIFF ===\n{git_diff}"
        request = LLMTaskRequest(
            task_id="triage-analysis",
            system_prompt=TRIAGE_AGENT_SYSTEM_PROMPT,
            context_payload=payload,
            model=model,
        )
        response = await adapter.execute_task(request)
        return (response.generated_text or "").strip()

    return triage


# -- validator-quality: cheap-model test critic (Phase 6, feature 1c) -------------

# Cap the diff we hand the critic so a huge change can't blow the cheap model's
# context / cost. The head of a diff (the code + first tests) is the most telling.
_TEST_CRITIC_DIFF_CAP = 12000

TEST_CRITIC_SYSTEM_PROMPT = (
    "You are a senior engineer reviewing the TEST QUALITY of a code change, given "
    "its git diff. The change already passed its test suite — your job is to judge "
    "whether those tests are meaningful, because an automated agent may have "
    "written weak or tautological tests just to make the suite green.\n"
    "\n"
    "Assess ONLY test quality, not code style. Look for: (a) is there a test at all "
    "for the new/changed behavior? (b) do the tests actually call the changed code "
    "with meaningful inputs and assert on real outputs/effects — or are they "
    "tautological (assert True, asserting a mock returns what it was told to, "
    "asserting a constant equals itself, no assertions)? (c) are the important "
    "edge/error cases covered?\n"
    "\n"
    "Respond in this exact shape, and be terse:\n"
    "VERDICT: STRONG | WEAK | MISSING\n"
    "then at most 4 short bullet lines ('- ...') naming the concrete gap(s), or a "
    "single '- tests meaningfully exercise the change' line if STRONG. No preamble."
)


def _extract_critic_verdict(text: str) -> str:
    """Pull the STRONG/WEAK/MISSING token out of the critic's reply for the
    structured event; falls back to 'UNKNOWN' if the model didn't follow format."""
    for line in text.splitlines():
        stripped = line.strip().upper()
        if stripped.startswith("VERDICT:"):
            token = stripped.split(":", 1)[1].strip().split()[0] if ":" in stripped else ""
            if token in {"STRONG", "WEAK", "MISSING"}:
                return token
    upper = text.upper()
    for token in ("MISSING", "WEAK", "STRONG"):
        if token in upper:
            return token
    return "UNKNOWN"


def build_test_critic(
    adapter: BaseMCPAdapter, model: str | None = None
) -> Callable[[str], Awaitable[str]]:
    """Build a cheap-model 'test critic': given a completed task's git diff, it
    judges whether the tests meaningfully exercise the change (vs weak/tautological
    /absent tests an agent may have written just to pass validation). Wired to a
    LOW-risk (cheap) provider+model by the CLI, and run only AFTER a task passes
    the sandbox — so it's one extra cheap call per completed task, never on the
    hot retry path. Advisory only: its verdict is surfaced to the human reviewer
    in the PR body, it does not block the merge."""

    async def critique(diff: str) -> str:
        request = LLMTaskRequest(
            task_id="test-critic",
            system_prompt=TEST_CRITIC_SYSTEM_PROMPT,
            context_payload="Git diff of the change under review:\n\n" + diff,
            model=model,
        )
        response = await adapter.execute_task(request)
        return (response.generated_text or "").strip()

    return critique


def build_claude_cli_agent_turn_executor(
    repo_root: Path,
    graph_json_path: Path,
    sandbox_language: str,
    model: str = "claude-sonnet-4-5",
    claude_cli: str = "claude",
) -> AgentTurnExecutor:
    """Builds the REAL, production `AgentTurnExecutor`: spawns the `claude`
    CLI in non-interactive mode with a generated `--mcp-config` pointing at
    `ai_os.mcp.mcp_server` (via `python -m`), so the model can call
    `propose_file_patch`/`fetch_symbol_definition`/`trigger_sandbox_validation`
    against this task's own worktree.

    Unlike Phase 3a's `AnthropicAdapter` (which locks the CLI down to zero
    tools, since it's a pure "text in, text out" completion), this executor
    grants exactly the 3 AI-OS MCP tools and nothing else — no Bash/Edit/Write
    on the real filesystem, no web access — via `--allowedTools` naming them
    under the `mcp__<server-name>__<tool>` convention Claude Code uses for
    custom MCP servers (the server is registered under the name "ai_os" in
    the generated config, so the allowed tools are
    "mcp__ai_os__propose_file_patch" etc.).

    NOT run by this project's own automated tests or by me during
    development (per explicit instruction — real LLM calls cost real usage);
    this is the piece the user runs first by hand via `ai-os task run`.
    """

    async def execute(ctx: AgentTurnContext) -> None:
        mcp_config = {
            "mcpServers": {
                "ai_os": {
                    "command": "python3",
                    "args": ["-m", "ai_os.mcp.mcp_server"],
                    "env": {
                        "AI_OS_WORKTREE_PATH": str(ctx.worktree_path),
                        "AI_OS_GRAPH_JSON_PATH": str(graph_json_path),
                        "AI_OS_SANDBOX_LANGUAGE": sandbox_language,
                    },
                }
            }
        }

        prompt_parts = [
            f"# Task: {ctx.task.title}",
            ctx.task.description,
            "",
            "## Compressed context (relevant symbols from the Knowledge Graph)",
            ctx.context_cache,
        ]
        if ctx.previous_validation_output:
            prompt_parts += [
                "",
                f"## Attempt {ctx.attempt}: previous attempt's validation output",
                "The previous attempt failed sandbox validation. Fix the code based on "
                "this output, then call trigger_sandbox_validation again to confirm.",
                ctx.previous_validation_output,
            ]
        prompt = "\n".join(prompt_parts) + conventions_block(ctx.project_conventions)

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", prefix="ai-os-mcp-config-", delete=False
        ) as f:
            json.dump(mcp_config, f)
            mcp_config_path = f.name

        try:
            argv = [
                claude_cli,
                "-p",
                prompt,
                "--output-format",
                "json",
                "--model",
                model,
                "--permission-mode",
                "acceptEdits",
                "--mcp-config",
                mcp_config_path,
                "--strict-mcp-config",
                "--allowedTools",
                "mcp__ai_os__propose_file_patch mcp__ai_os__apply_file_edit "
                "mcp__ai_os__fetch_symbol_definition mcp__ai_os__trigger_sandbox_validation",
            ]
            proc = await asyncio.create_subprocess_exec(
                *argv,
                cwd=str(ctx.worktree_path),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await proc.communicate()
            if proc.returncode != 0:
                combined = (stdout.decode(errors="replace") + "\n" + stderr.decode(errors="replace"))
                lowered = combined.lower()
                if any(marker in lowered for marker in _USAGE_LIMIT_MARKERS):
                    raise AgentUsageLimitError(
                        f"claude CLI reported a usage/rate limit: {combined.strip()[:500]}"
                    )
                raise RuntimeError(
                    f"claude CLI agent turn failed (exit {proc.returncode}): "
                    f"{(stderr.decode(errors='replace') or stdout.decode(errors='replace'))[:2000]}"
                )
        finally:
            Path(mcp_config_path).unlink(missing_ok=True)

        # Parse the CLI's JSON usage for accounting (Stage 3). Best-effort: an
        # unparseable/partial payload just means no cost row for this turn, not
        # a failed turn (the code changes already landed in the worktree).
        try:
            payload = json.loads(stdout.decode())
            usage = payload.get("usage") or {}
            return AgentTurnUsage(
                provider="anthropic",
                model_name=model,
                usage=TokenUsage(
                    input_tokens=usage.get("input_tokens", 0),
                    output_tokens=usage.get("output_tokens", 0),
                    estimated_usd_cost=payload.get("total_cost_usd", 0.0),
                ),
            )
        except (json.JSONDecodeError, AttributeError):
            return None

    return execute


# -- completion-based agent turn (for providers WITHOUT autonomous tool use) -------

# Sentinel-delimited formats the model is asked to emit. Deliberately NOT
# markdown code fences: fenced blocks collide with any code the model writes
# that itself contains ``` (very common), which makes robust parsing
# impossible. These sentinels are vanishingly unlikely to appear in real source.
#
# Two block kinds, mirroring the two MCP tools (Stage 2):
#   AI_OS_FILE — whole-file write (creating a file, or a full rewrite).
#   AI_OS_EDIT — targeted search/replace edit of an EXISTING file, so the model
#     only re-sends the changed snippet, not the whole file — cutting output
#     tokens dramatically on large files and lifting the "large files truncate"
#     limitation the whole-file-only format had.
_FILE_PATCH_RE = re.compile(
    r"<<<AI_OS_FILE:\s*(?P<path>.+?)\s*>>>\n(?P<content>.*?)\n?<<<AI_OS_END>>>",
    re.DOTALL,
)
# One combined, position-ordered scan over both block kinds, so writes and edits
# are applied in the exact order the model emitted them (an edit may target a
# file an earlier block just wrote). The FILE branch is anchored on its own
# `<<<AI_OS_FILE:` header and the EDIT branch on `<<<AI_OS_EDIT:`, so the two
# never cross-match despite sharing the `<<<AI_OS_END>>>` terminator.
_ACTION_RE = re.compile(
    r"<<<AI_OS_FILE:\s*(?P<wpath>.+?)\s*>>>\n(?P<wcontent>.*?)\n?<<<AI_OS_END>>>"
    r"|"
    r"<<<AI_OS_EDIT:\s*(?P<epath>.+?)\s*>>>\n"
    r"<<<AI_OS_SEARCH>>>\n(?P<search>.*?)\n?"
    r"<<<AI_OS_REPLACE>>>\n?(?P<replace>.*?)\n?"
    r"<<<AI_OS_END>>>",
    re.DOTALL,
)

COMPLETION_SYSTEM_PROMPT = (
    "You are a software engineering agent. You are given a task and compressed "
    "context. Emit your changes as sentinel-delimited blocks and NOTHING else "
    "around them. Two block kinds:\n"
    "\n"
    "To EDIT an existing file (PREFER THIS — much cheaper than resending a whole "
    "file), emit one block per edit with an EXACT snippet to find and its "
    "replacement:\n"
    "<<<AI_OS_EDIT: relative/path/from/repo/root.py>>>\n"
    "<<<AI_OS_SEARCH>>>\n"
    "<the exact current text to replace — must match uniquely, include enough "
    "surrounding lines>\n"
    "<<<AI_OS_REPLACE>>>\n"
    "<the new text>\n"
    "<<<AI_OS_END>>>\n"
    "\n"
    "To CREATE a new file (or fully rewrite one), emit its complete contents:\n"
    "<<<AI_OS_FILE: relative/path/from/repo/root.py>>>\n"
    "<the full file content>\n"
    "<<<AI_OS_END>>>\n"
    "\n"
    "Rules: paths are POSIX, relative to the repo root, no leading './'; the "
    "SEARCH text must match the file's current content EXACTLY (whitespace and "
    "indentation included) and uniquely; do not wrap blocks in markdown code "
    "fences; write no prose outside the blocks. If you introduce a new "
    "third-party dependency, also update the project's dependency manifest "
    "(requirements.txt / package.json / pom.xml) in a block — the sandbox "
    "installs those before running the tests. If your change alters the "
    "database schema, also update the schema migration AND the reference-data "
    "seed the sandbox runs (the seed script named in .ai-os/sandbox.json's "
    "setup_commands, or a new seed migration) so the DB-backed tests still pass.\n"
    "\n"
    "TESTS ARE MANDATORY, NOT OPTIONAL: for any behavior you add or change, add or "
    "update a real test that calls the changed code with meaningful inputs and "
    "asserts on real outputs/effects — never a tautological test (assert True, "
    "asserting a mock returns what you told it to) written just to make the suite "
    "green. A code change with no accompanying test is flagged for human review.\n"
    "You MAY also add CI workflow files (e.g. .github/workflows/ci.yml) so the "
    "project is complete and self-testing — but such files run with the repo's "
    "real secrets after merge, so they are always routed to human PR review, never "
    "auto-merged; keep them honest (run the real build + tests, do not weaken them)."
)


class AgentTurnError(RuntimeError):
    """An agent turn produced no usable result (e.g. the model returned no
    parseable file blocks, tried to write outside the worktree, or emitted an
    edit whose search text didn't match the target file)."""


@dataclass
class _WriteAction:
    path: str
    content: str


@dataclass
class _EditAction:
    path: str
    search: str
    replace: str


def parse_file_patches(text: str) -> dict[str, str]:
    """Parses the whole-file `<<<AI_OS_FILE>>>` blocks into `{relpath: content}`.
    Kept for the whole-file path (and its existing tests); the executor uses the
    richer `parse_agent_actions` (which also understands edit blocks)."""
    patches: dict[str, str] = {}
    for match in _FILE_PATCH_RE.finditer(text):
        patches[match.group("path").strip()] = match.group("content")
    return patches


def parse_agent_actions(text: str) -> list[_WriteAction | _EditAction]:
    """Parses both whole-file writes and search/replace edits, in the order the
    model emitted them. Returns an empty list if none are present (the caller
    decides whether that's an error)."""
    actions: list[_WriteAction | _EditAction] = []
    for match in _ACTION_RE.finditer(text):
        if match.group("wpath") is not None:
            actions.append(
                _WriteAction(path=match.group("wpath").strip(), content=match.group("wcontent"))
            )
        else:
            actions.append(
                _EditAction(
                    path=match.group("epath").strip(),
                    search=match.group("search"),
                    replace=match.group("replace"),
                )
            )
    return actions


def _resolve_within_worktree(worktree_path: Path, relpath: str) -> Path:
    """Resolves `<worktree_path>/<relpath>` and rejects any path that escapes
    the worktree (same defense as the MCP server's tools: an absolute `relpath`
    makes `worktree / relpath` discard the root, which the `is_relative_to`
    check then catches, as does a `..` traversal)."""
    root = worktree_path.resolve()
    target = (worktree_path / relpath).resolve()
    if not target.is_relative_to(root):
        raise AgentTurnError(f"patch path {relpath!r} escapes the worktree root")
    return target


def _write_patch_within_worktree(worktree_path: Path, relpath: str, content: str) -> None:
    """Writes `content` to `<worktree_path>/<relpath>`, creating parent dirs."""
    target = _resolve_within_worktree(worktree_path, relpath)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


def _apply_edit_within_worktree(worktree_path: Path, action: _EditAction) -> None:
    """Applies a search/replace edit to an existing worktree file. Mirrors the
    MCP `apply_file_edit` semantics: the search text must exist and (barring a
    unique-by-construction match) be unambiguous — a zero- or multi-match is an
    `AgentTurnError` so the retry loop feeds the model a clear reason."""
    target = _resolve_within_worktree(worktree_path, action.path)
    if not target.is_file():
        raise AgentTurnError(
            f"edit target {action.path!r} does not exist in the worktree "
            "(use an AI_OS_FILE block to create a new file)"
        )
    original = target.read_text(encoding="utf-8")
    count = original.count(action.search)
    if count == 0:
        raise AgentTurnError(
            f"edit for {action.path!r}: search text not found (it must match the "
            "file's current content exactly, including whitespace)"
        )
    if count > 1:
        raise AgentTurnError(
            f"edit for {action.path!r}: search text is not unique ({count} matches); "
            "include more surrounding context"
        )
    target.write_text(original.replace(action.search, action.replace), encoding="utf-8")


def build_completion_agent_turn_executor(
    adapter: BaseMCPAdapter, model: str | None = None
) -> AgentTurnExecutor:
    """Builds an `AgentTurnExecutor` for providers that DON'T support
    autonomous MCP tool-calling (e.g. a future/local adapter whose
    `supports_tool_calling()` is False).

    Instead of letting the model call the MCP tools itself, this sends the task
    + context + (on retries) the previous validation output as a plain
    completion, asks for its changes as sentinel-delimited blocks — either a
    search/replace `AI_OS_EDIT` (preferred for modifying files) or a whole-file
    `AI_OS_FILE` (for new files) — and AI-OS itself applies them to the
    worktree. The existing `TaskRunner` sandbox-validation + retry-with-feedback
    loop then works identically — the only difference from the Anthropic-CLI
    executor is *who* applies the changes (AI-OS here, the model-driven tool
    call there).

    Stage 2 note: edit blocks fix the earlier whole-file-only limitation where
    very large files could hit the model's output limit / truncate mid-file —
    now the model only re-sends the changed snippet.
    """

    async def execute(ctx: AgentTurnContext) -> None:
        prompt_parts = [
            f"# Task: {ctx.task.title}",
            ctx.task.description,
            "",
            f"## Files you are expected to change: {', '.join(ctx.task.target_files) or '(infer from the task)'}",
            "",
            "## Compressed context (relevant symbols from the Knowledge Graph)",
            ctx.context_cache,
        ]
        if ctx.previous_validation_output:
            prompt_parts += [
                "",
                f"## Attempt {ctx.attempt}: previous attempt's validation output (it FAILED)",
                "Fix the code based on this output and re-emit the corrected block(s).",
                ctx.previous_validation_output,
            ]
        request = LLMTaskRequest(
            task_id=ctx.task.id,
            system_prompt=COMPLETION_SYSTEM_PROMPT,
            context_payload="\n".join(prompt_parts) + conventions_block(ctx.project_conventions),
            model=model,
        )
        response = await adapter.execute_task(request)
        actions = parse_agent_actions(response.generated_text)
        if not actions:
            raise AgentTurnError(
                f"agent turn for task {ctx.task.id!r} produced no parseable file blocks "
                f"(model={response.model_name})"
            )
        for action in actions:
            if isinstance(action, _WriteAction):
                _write_patch_within_worktree(ctx.worktree_path, action.path, action.content)
            else:
                _apply_edit_within_worktree(ctx.worktree_path, action)
        return AgentTurnUsage(
            provider=response.provider, model_name=response.model_name, usage=response.usage
        )

    return execute


# -- tool-calling agent turn (for HTTP providers WITH a native tool-use loop) -------

TOOL_CALLING_SYSTEM_PROMPT = (
    "You are a software engineering agent working inside an isolated git "
    "worktree. You have tools to do your job — use them, do not just describe "
    "changes in prose:\n"
    "  - apply_file_edit(filepath, old_string, new_string, replace_all): make a "
    "targeted search/replace edit to an EXISTING file — prefer this for "
    "modifying files (much cheaper than resending the whole file).\n"
    "  - propose_file_patch(filepath, content, is_new_file): write the full "
    "contents of a file — use this to CREATE a new file.\n"
    "  - fetch_symbol_definition(symbol_id): look up a symbol's skeleton from the "
    "Knowledge Graph by its '<relpath>::<QualifiedName>' FQN.\n"
    "  - trigger_sandbox_validation(): run the build/test suite against the "
    "current worktree state and see whether it passes.\n"
    "Workflow: make your edits with propose_file_patch, then call "
    "trigger_sandbox_validation to confirm they pass. If validation fails, read "
    "the output, fix the code, and validate again. When validation passes, stop "
    "and briefly summarize what you changed.\n"
    "IMPORTANT: if your change introduces a new third-party dependency, add it to "
    "the project's dependency manifest (requirements.txt for Python, package.json "
    "for Node, pom.xml for Java) — the sandbox installs those before running the "
    "tests, so an unlisted dependency will make validation fail. Likewise, if you "
    "change the database schema, update the schema migration AND the "
    "reference-data seed the sandbox runs (the seed named in .ai-os/sandbox.json's "
    "setup_commands, or a new seed migration) so the DB-backed tests still pass.\n"
    "TESTS ARE MANDATORY: for any behavior you add or change, add/update a real "
    "test that exercises the changed code with meaningful inputs and asserts on "
    "real outputs — never a tautological test written just to pass validation (a "
    "code change with no test is flagged for human review). You MAY also add CI "
    "workflow files (.github/workflows/) to make the project complete and "
    "self-testing; those are always routed to human PR review (they run with the "
    "repo's real secrets after merge), so keep them honest — never weaken them."
)


def _calltool_result_to_text(result) -> str:
    """Flatten an MCP `CallToolResult` into the plain text a `ToolDispatch`
    must return: concatenate every `TextContent` block's `.text`. Error results
    (`is_error=True`) are returned as text too — the model needs to *see* a
    rejection or a failed validation to react to it, exactly as it would over a
    real MCP transport (where the error text is delivered as the tool result)."""
    parts = [
        getattr(block, "text", "")
        for block in (result.content or [])
        if getattr(block, "type", None) == "text"
    ]
    return "\n".join(parts)


def build_tool_calling_agent_turn_executor(
    adapter: BaseMCPAdapter,
    model: str | None,
    knowledge_engine: KnowledgeEngine,
    sandbox_runner: EphemeralSandboxRunner,
    sandbox_language: str,
) -> AgentTurnExecutor:
    """Builds an `AgentTurnExecutor` for HTTP providers that DO support a native
    autonomous tool-calling loop (Gemini, OpenRouter, Anthropic API-key mode) —
    i.e. any adapter whose `supports_tool_calling()` is True.

    Unlike the completion executor (one-shot: model returns whole files, AI-OS
    writes them), this lets the model iterate: call `propose_file_patch` /
    `fetch_symbol_definition` / `trigger_sandbox_validation`, see each result,
    and keep working — the same agentic loop the Anthropic CLI-session path
    gets, but driven through the provider's own function-calling API instead of
    the `claude` CLI's built-in MCP client.

    Crucially it reuses the EXACT SAME tool implementations as the MCP server
    (`ai_os.mcp.mcp_server.dispatch_tool_call` against a `ToolContext`) — no
    per-provider tool reimplementation. A fresh `ToolContext` is built per turn
    because the worktree path is task-specific and only known at run time; the
    Knowledge Graph engine + sandbox runner are shared in-process (no subprocess,
    no graph JSON round-trip, unlike the CLI executor).
    """
    # Imported here (not at module top) to avoid a core -> mcp import at load
    # time; the MCP tool catalog is only needed when this executor is actually
    # built for a tool-capable provider.
    from ai_os.mcp.mcp_server import (
        TOOL_DEFINITIONS,
        ToolContext,
        dispatch_tool_call,
    )

    tool_specs = [
        ToolSpec(
            name=tool.name,
            description=tool.description or "",
            json_schema=tool.input_schema,
        )
        for tool in TOOL_DEFINITIONS
    ]

    async def execute(ctx: AgentTurnContext) -> None:
        tool_context = ToolContext(
            worktree_path=ctx.worktree_path,
            knowledge_engine=knowledge_engine,
            graph_load_error=None,
            sandbox_runner=sandbox_runner,
            sandbox_language=sandbox_language,
        )

        async def dispatch(name: str, arguments: dict) -> str:
            result = await dispatch_tool_call(tool_context, name, arguments)
            return _calltool_result_to_text(result)

        prompt_parts = [
            f"# Task: {ctx.task.title}",
            ctx.task.description,
            "",
            f"## Files you are expected to change: {', '.join(ctx.task.target_files) or '(infer from the task)'}",
            "",
            "## Compressed context (relevant symbols from the Knowledge Graph)",
            ctx.context_cache,
        ]
        if ctx.previous_validation_output:
            prompt_parts += [
                "",
                f"## Attempt {ctx.attempt}: previous attempt's validation output (it FAILED)",
                "Fix the code based on this output, then call "
                "trigger_sandbox_validation again to confirm.",
                ctx.previous_validation_output,
            ]
        request = LLMTaskRequest(
            task_id=ctx.task.id,
            system_prompt=TOOL_CALLING_SYSTEM_PROMPT,
            context_payload="\n".join(prompt_parts) + conventions_block(ctx.project_conventions),
            model=model,
        )
        response = await adapter.execute_with_tools(request, tool_specs, dispatch)
        return AgentTurnUsage(
            provider=response.provider, model_name=response.model_name, usage=response.usage
        )

    return execute
