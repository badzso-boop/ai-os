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
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable, Optional

from ai_os.core.lock_manager import LockManager
from ai_os.core.models import TaskNode
from ai_os.core.staging import GitStagingEngine
from ai_os.knowledge.graph_engine import KnowledgeEngine
from ai_os.sandbox.container_runner import EphemeralSandboxRunner


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


@dataclass
class TaskRunResult:
    task_id: str
    status: str  # "COMPLETED" | "BLOCKED"
    attempts: int
    final_output: str | None = None


AgentTurnExecutor = Callable[[AgentTurnContext], Awaitable[None]]
"""Perform one agent turn against `ctx.worktree_path` and return. Raising
signals an infra fault in the turn itself (e.g. the CLI subprocess crashed) —
distinct from "the turn completed but the code still fails validation",
which is the normal, expected retry path and is never an exception here.
"""


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
    ) -> None:
        self.lock_manager = lock_manager
        self.staging = staging
        self.knowledge_engine = knowledge_engine
        self.agent_turn_executor = agent_turn_executor
        self.sandbox_runner = sandbox_runner or EphemeralSandboxRunner()
        # Optional, deliberately thin status-transition hook (task_id, status)
        # -> None. Kept as a plain callback rather than a direct SQLAlchemy
        # dependency so this module doesn't need to assume a TaskModel row
        # already exists (Phase 2's schema requires a non-null epic_id FK) —
        # callers who want DB persistence wire this to their own upsert.
        self.on_status_change = on_status_change

    def _report_status(self, task_id: str, status: str) -> None:
        if self.on_status_change is not None:
            self.on_status_change(task_id, status)

    async def run_task(self, task: TaskNode, language: str, max_hops: int = 2) -> TaskRunResult:
        async with self.lock_manager.locks(task.id, task.read_set, task.write_set):
            self._report_status(task.id, "RUNNING")
            worktree_path = await self.staging.create_worktree(task.id)
            context_cache = self.knowledge_engine.build_context_cache(
                task.target_files, max_hops=max_hops
            )

            last_output: str | None = None
            max_attempts = task.max_retries + 1
            attempt = 0
            for attempt in range(1, max_attempts + 1):
                turn_ctx = AgentTurnContext(
                    task=task,
                    worktree_path=worktree_path,
                    context_cache=context_cache,
                    attempt=attempt,
                    previous_validation_output=last_output,
                )
                await self.agent_turn_executor(turn_ctx)

                validator_ran = False

                async def validator(wt_path: Path) -> bool:
                    nonlocal validator_ran, last_output
                    validator_ran = True
                    result = await self.sandbox_runner.run_validation(wt_path, language)
                    last_output = result.output
                    return result.success

                merged = await self.staging.stage_and_merge_task(
                    task.id, f"{task.id}: attempt {attempt}", validator
                )
                if merged:
                    self._report_status(task.id, "COMPLETED")
                    return TaskRunResult(
                        task_id=task.id, status="COMPLETED", attempts=attempt, final_output=last_output
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

            await self.staging.abandon_task(task.id)
            self._report_status(task.id, "BLOCKED")
            return TaskRunResult(
                task_id=task.id, status="BLOCKED", attempts=attempt, final_output=last_output
            )


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
        prompt = "\n".join(prompt_parts)

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
                "mcp__ai_os__propose_file_patch mcp__ai_os__fetch_symbol_definition "
                "mcp__ai_os__trigger_sandbox_validation",
            ]
            proc = await asyncio.create_subprocess_exec(
                *argv,
                cwd=str(ctx.worktree_path),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await proc.communicate()
            if proc.returncode != 0:
                raise RuntimeError(
                    f"claude CLI agent turn failed (exit {proc.returncode}): {stderr.decode()[:2000]}"
                )
        finally:
            Path(mcp_config_path).unlink(missing_ok=True)

    return execute
