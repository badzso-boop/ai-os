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
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable, Optional

from ai_os.core.lock_manager import LockManager
from ai_os.core.models import TaskNode
from ai_os.core.staging import GitStagingEngine
from ai_os.knowledge.graph_engine import KnowledgeEngine
from ai_os.mcp.adapters.base_adapter import (
    BaseMCPAdapter,
    LLMTaskRequest,
    ToolSpec,
)
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


# -- completion-based agent turn (for providers WITHOUT autonomous tool use) -------

# Sentinel-delimited file format the model is asked to emit. Deliberately NOT
# markdown code fences: fenced blocks collide with any code the model writes
# that itself contains ``` (very common), which makes robust parsing
# impossible. These sentinels are vanishingly unlikely to appear in real source.
_FILE_PATCH_RE = re.compile(
    r"<<<AI_OS_FILE:\s*(?P<path>.+?)\s*>>>\n(?P<content>.*?)\n?<<<AI_OS_END>>>",
    re.DOTALL,
)

COMPLETION_SYSTEM_PROMPT = (
    "You are a software engineering agent. You are given a task and compressed "
    "context. Produce the COMPLETE new contents of every file you need to create "
    "or modify. For each such file, output EXACTLY this block and nothing else "
    "around it:\n"
    "<<<AI_OS_FILE: relative/path/from/repo/root.py>>>\n"
    "<the full file content>\n"
    "<<<AI_OS_END>>>\n"
    "Rules: emit one block per file; paths are POSIX, relative to the repo root, "
    "no leading './'; output the ENTIRE file content, not a diff or a snippet; do "
    "not wrap blocks in markdown code fences; write no prose outside the blocks."
)


class AgentTurnError(RuntimeError):
    """An agent turn produced no usable result (e.g. the model returned no
    parseable file blocks, or tried to write outside the worktree)."""


def parse_file_patches(text: str) -> dict[str, str]:
    """Parses the sentinel-delimited file blocks a completion agent emits into
    `{relative_path: content}`. Returns an empty dict if none are present (the
    caller decides whether that's an error)."""
    patches: dict[str, str] = {}
    for match in _FILE_PATCH_RE.finditer(text):
        patches[match.group("path").strip()] = match.group("content")
    return patches


def _write_patch_within_worktree(worktree_path: Path, relpath: str, content: str) -> None:
    """Writes `content` to `<worktree_path>/<relpath>`, rejecting any path that
    escapes the worktree (same defense as the MCP server's `propose_file_patch`:
    an absolute `relpath` makes `worktree / relpath` discard the root, which the
    `is_relative_to` check then catches, as does a `..` traversal)."""
    root = worktree_path.resolve()
    target = (worktree_path / relpath).resolve()
    if not target.is_relative_to(root):
        raise AgentTurnError(f"patch path {relpath!r} escapes the worktree root")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


def build_completion_agent_turn_executor(
    adapter: BaseMCPAdapter, model: str | None = None
) -> AgentTurnExecutor:
    """Builds an `AgentTurnExecutor` for providers that DON'T support
    autonomous MCP tool-calling (Gemini, OpenRouter, Anthropic API-key mode).

    Instead of letting the model call `propose_file_patch` itself, this sends
    the task + context + (on retries) the previous validation output as a plain
    completion, asks for the full new content of each file to change in a
    sentinel-delimited format, and AI-OS itself writes those files into the
    worktree. The existing `TaskRunner` sandbox-validation + retry-with-feedback
    loop then works identically — the only difference from the Anthropic-CLI
    executor is *who* writes the files (AI-OS here, the model-driven tool call
    there).

    Known limitation (flagged, doc'd in CLAUDE.md): asking for full file
    contents means very large files can hit the model's output limit / get
    truncated mid-file. Fine for the task sizes AI-OS targets (focused,
    single-responsibility tasks per the DAG decomposition); a future diff-based
    protocol would lift this, not built yet.
    """

    async def execute(ctx: AgentTurnContext) -> None:
        prompt_parts = [
            f"# Task: {ctx.task.title}",
            ctx.task.description,
            "",
            f"## Files you are expected to write: {', '.join(ctx.task.target_files) or '(infer from the task)'}",
            "",
            "## Compressed context (relevant symbols from the Knowledge Graph)",
            ctx.context_cache,
        ]
        if ctx.previous_validation_output:
            prompt_parts += [
                "",
                f"## Attempt {ctx.attempt}: previous attempt's validation output (it FAILED)",
                "Fix the code based on this output and re-emit the full corrected file(s).",
                ctx.previous_validation_output,
            ]
        request = LLMTaskRequest(
            task_id=ctx.task.id,
            system_prompt=COMPLETION_SYSTEM_PROMPT,
            context_payload="\n".join(prompt_parts),
            model=model,
        )
        response = await adapter.execute_task(request)
        patches = parse_file_patches(response.generated_text)
        if not patches:
            raise AgentTurnError(
                f"agent turn for task {ctx.task.id!r} produced no parseable file blocks "
                f"(model={response.model_name})"
            )
        for relpath, content in patches.items():
            _write_patch_within_worktree(ctx.worktree_path, relpath, content)

    return execute


# -- tool-calling agent turn (for HTTP providers WITH a native tool-use loop) -------

TOOL_CALLING_SYSTEM_PROMPT = (
    "You are a software engineering agent working inside an isolated git "
    "worktree. You have tools to do your job — use them, do not just describe "
    "changes in prose:\n"
    "  - propose_file_patch(filepath, content, is_new_file): write the full new "
    "contents of a file into the worktree.\n"
    "  - fetch_symbol_definition(symbol_id): look up a symbol's skeleton from the "
    "Knowledge Graph by its '<relpath>::<QualifiedName>' FQN.\n"
    "  - trigger_sandbox_validation(): run the build/test suite against the "
    "current worktree state and see whether it passes.\n"
    "Workflow: make your edits with propose_file_patch, then call "
    "trigger_sandbox_validation to confirm they pass. If validation fails, read "
    "the output, fix the code, and validate again. When validation passes, stop "
    "and briefly summarize what you changed."
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
            context_payload="\n".join(prompt_parts),
            model=model,
        )
        await adapter.execute_with_tools(request, tool_specs, dispatch)

    return execute
