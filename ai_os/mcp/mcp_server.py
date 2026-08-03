"""MCP server exposing the three agent-facing tools from doc 07 §3
(`propose_file_patch`, `fetch_symbol_definition`, `trigger_sandbox_validation`)
over the real `mcp` SDK's stdio transport (doc 07, Phase 3b).

Why the real `mcp` package instead of this project's usual raw-`httpx`
convention (see Phase 3a's provider adapters): the MCP wire protocol has real,
evolving complexity — several protocol revisions exist, and getting one
subtly wrong would be silently incompatible with whatever revision the real
`claude` CLI negotiates, with no live-LLM test available in this environment
to catch that. So this module leans on the SDK's own server-side machinery
for protocol correctness, and `tests/test_mcp_server.py` proves correctness
the same way: by driving this server through a real stdio round-trip using
the SDK's own *client*-side machinery (no LLM involved anywhere).

Installed `mcp` package: v2.0.0. Its low-level `Server` API is
constructor-based, not the decorator style (`@server.list_tools()`) from
older SDK versions/tutorials — handlers are passed as `on_list_tools=` /
`on_call_tool=` keyword arguments to `Server(...)`, each an
`async def (ctx, params) -> Result` callable. Tool/message types live in the
`mcp_types` package, re-exported unchanged (same classes) under `mcp.types`.

Configuration (env vars, read once at startup via `ServerConfig.from_env`,
per doc 07's "spawned fresh per session by `--mcp-config`" model):
    AI_OS_WORKTREE_PATH    — required; the task's git worktree root.
    AI_OS_GRAPH_JSON_PATH  — optional; a `KnowledgeEngine.to_json()` file.
                             Missing/absent -> `fetch_symbol_definition`
                             returns a clear tool error; the other two tools
                             are unaffected.
    AI_OS_SANDBOX_LANGUAGE — optional; language string passed to
                             `EphemeralSandboxRunner.run_validation`. Missing
                             -> `trigger_sandbox_validation` returns a clear
                             tool error; the other two tools are unaffected.

This module deliberately does NOT create or destroy git worktrees — that
lifecycle already exists correctly in `ai_os.core.staging.GitStagingEngine`
(Phase 2). It only ever writes into a worktree path it's handed.
"""
from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import mcp.types as types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

from ai_os.knowledge.graph_engine import KnowledgeEngine
from ai_os.sandbox.container_runner import EphemeralSandboxRunner, ValidationResult

SERVER_NAME = "ai-os-mcp-server"
SERVER_VERSION = "0.1.0"


class SandboxRunner(Protocol):
    """Structural type for `EphemeralSandboxRunner` — lets tests inject a
    fake with a scripted `run_validation` coroutine instead of the real
    Docker-backed implementation."""

    async def run_validation(self, worktree_path: Path, language: str) -> ValidationResult: ...


# -- configuration ---------------------------------------------------------------


@dataclass(frozen=True)
class ServerConfig:
    """Parsed, validated server configuration. Construct via `from_env()` for
    a real subprocess launch, or directly (bypassing env parsing entirely)
    for tests."""

    worktree_path: Path
    graph_json_path: Path | None = None
    sandbox_language: str | None = None

    @classmethod
    def from_env(cls, environ: dict[str, str] | None = None) -> "ServerConfig":
        """Reads `AI_OS_WORKTREE_PATH` / `AI_OS_GRAPH_JSON_PATH` /
        `AI_OS_SANDBOX_LANGUAGE` from `environ` (defaults to `os.environ`).

        Raises `ValueError` if `AI_OS_WORKTREE_PATH` is unset — both
        `propose_file_patch` and `trigger_sandbox_validation` are unusable
        without it, so failing fast at startup beats silently degrading two
        of the three tools. The graph path and sandbox language are genuinely
        optional (per-tool degradation, not a startup failure) — see the
        module docstring.
        """
        env = environ if environ is not None else os.environ
        worktree_raw = env.get("AI_OS_WORKTREE_PATH")
        if not worktree_raw:
            raise ValueError(
                "AI_OS_WORKTREE_PATH is required (the MCP server needs a task "
                "worktree to operate on) but was not set."
            )
        graph_raw = env.get("AI_OS_GRAPH_JSON_PATH")
        return cls(
            worktree_path=Path(worktree_raw),
            graph_json_path=Path(graph_raw) if graph_raw else None,
            sandbox_language=env.get("AI_OS_SANDBOX_LANGUAGE") or None,
        )


def load_knowledge_engine(graph_json_path: Path | None) -> tuple[KnowledgeEngine | None, str | None]:
    """Loads a `KnowledgeEngine` from `graph_json_path` once at startup.

    Returns `(engine, None)` on success or `(None, reason)` on any failure
    (unset path, missing file, malformed JSON) — never raises, since a
    missing/broken graph must not crash server startup (`fetch_symbol_definition`
    surfaces `reason` as a tool error; the other two tools are unaffected).
    """
    if graph_json_path is None:
        return None, "No knowledge graph configured (AI_OS_GRAPH_JSON_PATH is not set)."
    if not graph_json_path.exists():
        return None, f"No knowledge graph configured (file not found: {graph_json_path})."
    try:
        return KnowledgeEngine.from_json(graph_json_path), None
    except Exception as exc:  # malformed graph JSON, etc. — degrade, don't crash startup
        return None, f"Failed to load knowledge graph from {graph_json_path}: {exc}"


# -- runtime context (what the tool handlers actually operate against) -----------


@dataclass
class ToolContext:
    """Everything the three tool implementations need, gathered in one place
    so tests can construct it directly — real `tmp_path` worktree, a real (or
    fake) `KnowledgeEngine`, a fake `SandboxRunner` — without touching env
    vars or spawning a subprocess."""

    worktree_path: Path
    knowledge_engine: KnowledgeEngine | None
    graph_load_error: str | None
    sandbox_runner: SandboxRunner
    sandbox_language: str | None

    @classmethod
    def from_config(cls, config: ServerConfig, sandbox_runner: SandboxRunner | None = None) -> "ToolContext":
        engine, graph_error = load_knowledge_engine(config.graph_json_path)
        return cls(
            worktree_path=config.worktree_path,
            knowledge_engine=engine,
            graph_load_error=graph_error,
            sandbox_runner=sandbox_runner if sandbox_runner is not None else EphemeralSandboxRunner(),
            sandbox_language=config.sandbox_language,
        )


class PathTraversalError(ValueError):
    """Raised when a tool-supplied `filepath` would escape the worktree root."""


def _text_result(text: str, *, is_error: bool = False) -> types.CallToolResult:
    return types.CallToolResult(content=[types.TextContent(type="text", text=text)], is_error=is_error)


def _error_result(message: str) -> types.CallToolResult:
    return _text_result(message, is_error=True)


def _resolve_within_worktree(worktree_root: Path, filepath: str) -> Path:
    """Resolves `filepath` against `worktree_root` and verifies the result is
    still inside it. Handles both `../`-style traversal and absolute-path
    escapes: an absolute `filepath` makes `worktree_root / filepath` discard
    the root entirely (standard `pathlib` behavior), which the
    `is_relative_to` check below then rejects just like a `..` escape.
    """
    root_resolved = worktree_root.resolve()
    candidate = (worktree_root / filepath).resolve()
    if not candidate.is_relative_to(root_resolved):
        raise PathTraversalError(
            f"filepath {filepath!r} resolves outside the worktree root ({root_resolved})."
        )
    return candidate


# -- tool implementations ---------------------------------------------------------


async def propose_file_patch(ctx: ToolContext, filepath: str, content: str, is_new_file: bool = False) -> types.CallToolResult:
    """Writes `content` to `<worktree_root>/<filepath>`, creating parent
    directories as needed. Rejects any `filepath` that escapes the worktree
    root via `..` traversal or an absolute path, as an MCP tool error rather
    than a raised exception.
    """
    del is_new_file  # accepted per the doc 07 §3.1 schema; writing behavior doesn't differ by it
    try:
        target = _resolve_within_worktree(ctx.worktree_path, filepath)
    except PathTraversalError as exc:
        return _error_result(f"REJECTED: {exc}")

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return _text_result(f"SUCCESS: Patch applied to {filepath} in isolated worktree.")


async def fetch_symbol_definition(ctx: ToolContext, symbol_id: str) -> types.CallToolResult:
    """Looks up `symbol_id` (the `<relpath>::<QualifiedName>` FQN scheme from
    `ai_os.knowledge.graph_engine.KnowledgeEngine`) in the server's loaded
    graph and returns its skeleton stub.
    """
    if ctx.knowledge_engine is None:
        return _error_result(ctx.graph_load_error or "No knowledge graph configured.")

    graph = ctx.knowledge_engine.graph
    if symbol_id not in graph:
        return _error_result(f"Symbol not found in knowledge graph: {symbol_id!r}")

    node_data = graph.nodes[symbol_id]
    stub = node_data.get("stub")
    if not stub:
        node_type = node_data.get("node_type", "Unknown")
        return _error_result(
            f"No stub available for {symbol_id!r} (node_type={node_type!r}); "
            "it may be a FileNode or a symbol whose stub extraction failed."
        )
    return _text_result(stub)


async def trigger_sandbox_validation(ctx: ToolContext) -> types.CallToolResult:
    """Runs `EphemeralSandboxRunner.run_validation` against the server's
    configured worktree/language and formats the result.

    A validation *failure* (non-zero exit code) is a legitimate tool result
    the calling LLM needs to see and act on — it comes back as
    `isError: True` but does not raise. A genuine infra fault raised by the
    sandbox runner itself (e.g. `SandboxLanguageNotSupportedError`, `docker`
    missing) is caught here and returned as an MCP tool error instead of
    crashing the server.
    """
    if not ctx.sandbox_language:
        return _error_result("No sandbox language configured (AI_OS_SANDBOX_LANGUAGE is not set).")

    try:
        result = await ctx.sandbox_runner.run_validation(ctx.worktree_path, ctx.sandbox_language)
    except Exception as exc:
        return _error_result(f"Sandbox validation infrastructure error: {exc}")

    if result.success:
        return _text_result(f"VALIDATION PASSED\nExit Code: {result.exit_code}\nOutput:\n{result.output}")
    return _text_result(
        f"VALIDATION FAILED\nExit Code: {result.exit_code}\nOutput:\n{result.output}",
        is_error=True,
    )


# -- MCP tool catalog (doc 07 §3) --------------------------------------------------

TOOL_DEFINITIONS: list[types.Tool] = [
    types.Tool(
        name="propose_file_patch",
        description=(
            "Write file content to a path inside the task's isolated git worktree, "
            "creating parent directories as needed. Rejects paths that escape the "
            "worktree root."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "filepath": {
                    "type": "string",
                    "description": "Path relative to the worktree root (e.g. 'src/foo.py').",
                },
                "content": {
                    "type": "string",
                    "description": "Full file content to write.",
                },
                "is_new_file": {
                    "type": "boolean",
                    "description": "Whether this patch creates a new file (vs. modifying an existing one).",
                },
            },
            "required": ["filepath", "content", "is_new_file"],
        },
    ),
    types.Tool(
        name="fetch_symbol_definition",
        description=(
            "Look up a symbol's compressed skeleton stub in the project's Knowledge "
            "Graph by its fully-qualified name, formatted as '<relpath>::<QualifiedName>' "
            "(e.g. 'src/com/example/Helper.java::Helper.compute')."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "symbol_id": {
                    "type": "string",
                    "description": "The symbol's FQN in '<relpath>::<QualifiedName>' form.",
                },
            },
            "required": ["symbol_id"],
        },
    ),
    types.Tool(
        name="trigger_sandbox_validation",
        description=(
            "Run the task's configured build/test suite inside a hardened, ephemeral "
            "sandbox container against the current worktree state, and report whether "
            "validation passed."
        ),
        input_schema={"type": "object", "properties": {}},
    ),
]

_TOOL_NAMES = frozenset(tool.name for tool in TOOL_DEFINITIONS)


async def dispatch_tool_call(ctx: ToolContext, name: str, arguments: dict[str, object] | None) -> types.CallToolResult:
    """Dispatches a `tools/call` request to the matching tool implementation
    by name. Unknown tool names and argument-shape mismatches come back as
    MCP tool errors rather than raised exceptions, so a bad call never
    crashes the server.
    """
    arguments = arguments or {}
    try:
        if name == "propose_file_patch":
            return await propose_file_patch(ctx, **arguments)
        if name == "fetch_symbol_definition":
            return await fetch_symbol_definition(ctx, **arguments)
        if name == "trigger_sandbox_validation":
            return await trigger_sandbox_validation(ctx)
        return _error_result(f"Unknown tool: {name!r} (known tools: {sorted(_TOOL_NAMES)})")
    except TypeError as exc:
        # Wrong/missing arguments for an otherwise-known tool.
        return _error_result(f"Invalid arguments for tool {name!r}: {exc}")


# -- MCP server wiring --------------------------------------------------------------


def build_server(ctx: ToolContext) -> Server:
    """Constructs the low-level `mcp.server.lowlevel.Server`, wiring its
    `tools/list` and `tools/call` handlers to the tool catalog/dispatcher
    above. The `mcp` 2.0.0 low-level `Server` takes handlers as constructor
    keyword args (`on_list_tools=`, `on_call_tool=`) rather than the
    decorator style (`@server.list_tools()`) from older SDK versions.
    """

    async def handle_list_tools(_ctx, _params: types.PaginatedRequestParams | None) -> types.ListToolsResult:
        return types.ListToolsResult(tools=TOOL_DEFINITIONS)

    async def handle_call_tool(_ctx, params: types.CallToolRequestParams) -> types.CallToolResult:
        return await dispatch_tool_call(ctx, params.name, params.arguments)

    return Server(
        SERVER_NAME,
        version=SERVER_VERSION,
        instructions=(
            "Tools for an AI-OS execution-core agent: write files into the task's "
            "isolated git worktree, look up compressed symbol context from the "
            "Knowledge Graph, and trigger sandboxed build/test validation."
        ),
        on_list_tools=handle_list_tools,
        on_call_tool=handle_call_tool,
    )


async def run_stdio(ctx: ToolContext) -> None:
    """Runs `build_server(ctx)` over the real stdio transport until the
    client disconnects (its read side closes)."""
    server = build_server(ctx)
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


def main() -> None:
    """Entry point for `python -m ai_os.mcp.mcp_server` — reads configuration
    from the environment (per `--mcp-config`'s per-server `env` map) and
    serves over stdio until the client disconnects.
    """
    config = ServerConfig.from_env()
    ctx = ToolContext.from_config(config)
    asyncio.run(run_stdio(ctx))


if __name__ == "__main__":
    main()
