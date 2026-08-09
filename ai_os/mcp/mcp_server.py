"""Model Context Protocol (MCP) Server for AI-OS.

This module implements the AI-OS MCP server exposing tools for worktree file operations,
Knowledge Graph symbol definition lookup, ephemeral sandbox validation execution, and safe Git operations.

Tools exposed:
- apply_file_edit: Precise string replacement in a file.
- propose_file_patch: Full content creation or overwriting of a file.
- fetch_symbol_definition: Retrieve stub definitions from the Knowledge Graph.
- trigger_sandbox_validation: Run container sandbox validation.
- git_status: Inspect repository status (branch, cleanliness, staged/unstaged/untracked, sync).
- git_pull_main: Safely pull or fetch updates for the main branch.
- git_create_branch: Create and optionally check out a new branch with validation.
- git_diff_summary: Generate structured diff metrics for working tree, staged, or ref targets.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
import os
from pathlib import Path
from typing import Any, Protocol

from ai_os.mcp import git_tools

try:
    import mcp.types as types
    from mcp.server import Server
except ImportError:
    class TextContent:
        def __init__(self, type: str = "text", text: str = "") -> None:
            self.type = type
            self.text = text

    class CallToolResult:
        def __init__(self, content: list[Any] | None = None, isError: bool = False) -> None:
            self.content = content or []
            self.isError = isError

    class Tool:
        def __init__(self, name: str, description: str, inputSchema: dict[str, Any]) -> None:
            self.name = name
            self.description = description
            self.inputSchema = inputSchema

    class ListToolsResult:
        def __init__(self, tools: list[Tool]) -> None:
            self.tools = tools

    class CallToolRequestParams:
        def __init__(self, name: str, arguments: dict[str, Any] | None = None) -> None:
            self.name = name
            self.arguments = arguments or {}

    class PaginatedRequestParams:
        def __init__(self, cursor: str | None = None) -> None:
            self.cursor = cursor

    class _TypesNamespace:
        TextContent = TextContent
        CallToolResult = CallToolResult
        Tool = Tool
        ListToolsResult = ListToolsResult
        CallToolRequestParams = CallToolRequestParams
        PaginatedRequestParams = PaginatedRequestParams

    types = _TypesNamespace()  # type: ignore

    class Server:  # type: ignore
        def __init__(self, name: str) -> None:
            self.name = name


class PathTraversalError(ValueError):
    """Raised when a file path attempts to traverse outside the worktree root."""


def _resolve_within_worktree(worktree_root: Path, filepath: str) -> Path:
    """Resolve a file path relative to worktree_root and ensure no path traversal occurred."""
    p = Path(filepath)
    if p.is_absolute():
        resolved = p.resolve()
    else:
        resolved = (worktree_root / p).resolve()

    root_resolved = worktree_root.resolve()
    try:
        resolved.relative_to(root_resolved)
    except ValueError:
        raise PathTraversalError(f"Path '{filepath}' is outside worktree root '{worktree_root}'.")
    return resolved


@dataclass
class ServerConfig:
    """Configuration options for the MCP server loaded from environment variables."""

    worktree_path: Path
    graph_json_path: Path | None = None
    sandbox_language: str | None = None

    @classmethod
    def from_env(cls, environ: dict[str, str] | None = None) -> ServerConfig:
        env = environ if environ is not None else os.environ
        wt_str = env.get("WORKTREE_PATH") or env.get("AI_OS_WORKTREE_PATH")
        if not wt_str:
            raise ValueError("WORKTREE_PATH environment variable is required.")
        wt_path = Path(wt_str).resolve()
        graph_str = env.get("GRAPH_JSON_PATH") or env.get("AI_OS_GRAPH_JSON_PATH")
        graph_path = Path(graph_str).resolve() if graph_str else None
        lang = env.get("SANDBOX_LANGUAGE") or env.get("AI_OS_SANDBOX_LANGUAGE")
        return cls(worktree_path=wt_path, graph_json_path=graph_path, sandbox_language=lang)


class SandboxRunner(Protocol):
    async def run_validation(self, worktree_path: Path, language: str) -> Any:
        ...


@dataclass
class ToolContext:
    """Runtime context passed to MCP tool execution handlers."""

    worktree_root: Path
    graph_engine: Any = None
    sandbox_runner: Any = None
    sandbox_language: str | None = None

    @classmethod
    def from_config(cls, config: ServerConfig, sandbox_runner: Any = None) -> ToolContext:
        engine, _ = load_knowledge_engine(config.graph_json_path)
        return cls(
            worktree_root=config.worktree_path,
            graph_engine=engine,
            sandbox_runner=sandbox_runner,
            sandbox_language=config.sandbox_language,
        )


def load_knowledge_engine(graph_json_path: Path | None) -> tuple[Any | None, str | None]:
    """Safely load KnowledgeEngine from JSON path if configured."""
    if graph_json_path is None or not graph_json_path.exists():
        return None, None
    try:
        from ai_os.knowledge.graph_engine import KnowledgeEngine
        engine = KnowledgeEngine.from_json(graph_json_path)
        return engine, None
    except Exception as exc:
        return None, str(exc)


def _text_result(text: str, *, is_error: bool = False) -> types.CallToolResult:
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=text)],
        isError=is_error,
    )


def _error_result(message: str) -> types.CallToolResult:
    return _text_result(message, is_error=True)


async def apply_file_edit(
    ctx: ToolContext,
    filepath: str,
    old_string: str,
    new_string: str,
    replace_all: bool = False,
) -> types.CallToolResult:
    try:
        abs_path = _resolve_within_worktree(ctx.worktree_root, filepath)
    except PathTraversalError as err:
        return _error_result(str(err))

    if not abs_path.exists() or not abs_path.is_file():
        return _error_result(f"File not found: '{filepath}'")

    if old_string == new_string:
        return _error_result("old_string and new_string are identical.")

    content = abs_path.read_text(encoding="utf-8")
    count = content.count(old_string)
    if count == 0:
        return _error_result(f"Target string not found in '{filepath}'.")
    if count > 1 and not replace_all:
        return _error_result(
            f"Found {count} occurrences of target string in '{filepath}'. Set replace_all=True to replace all."
        )

    if replace_all:
        new_content = content.replace(old_string, new_string)
    else:
        new_content = content.replace(old_string, new_string, 1)

    abs_path.write_text(new_content, encoding="utf-8")
    return _text_result(f"Successfully edited '{filepath}'.")


async def propose_file_patch(
    ctx: ToolContext,
    filepath: str,
    content: str,
    is_new_file: bool = False,
) -> types.CallToolResult:
    try:
        abs_path = _resolve_within_worktree(ctx.worktree_root, filepath)
    except PathTraversalError as err:
        return _error_result(str(err))

    abs_path.parent.mkdir(parents=True, exist_ok=True)
    abs_path.write_text(content, encoding="utf-8")
    return _text_result(f"Successfully wrote '{filepath}'.")


async def fetch_symbol_definition(ctx: ToolContext, symbol_id: str) -> types.CallToolResult:
    if ctx.graph_engine is None:
        return _error_result("No knowledge graph configured.")

    graph = getattr(ctx.graph_engine, "graph", None)
    if graph is None or symbol_id not in graph.nodes:
        return _error_result(f"Symbol '{symbol_id}' not found in knowledge graph.")

    node_data = graph.nodes[symbol_id]
    stub = node_data.get("stub")
    if stub:
        return _text_result(stub)
    return _text_result(f"Symbol '{symbol_id}' has no stub definition.")


async def trigger_sandbox_validation(ctx: ToolContext) -> types.CallToolResult:
    if not ctx.sandbox_language:
        return _error_result("No sandbox language configured.")
    if ctx.sandbox_runner is None:
        return _error_result("No sandbox runner configured.")

    try:
        res = await ctx.sandbox_runner.run_validation(ctx.worktree_root, ctx.sandbox_language)
        if getattr(res, "success", False):
            stdout = getattr(res, "stdout", "") or getattr(res, "output", "") or "Validation succeeded."
            return _text_result(stdout)
        else:
            stderr = getattr(res, "stderr", "") or getattr(res, "output", "") or "Validation failed."
            return _error_result(f"Validation failed: {stderr}")
    except Exception as exc:
        return _error_result(f"Sandbox runner error: {exc}")


async def handle_list_tools(_ctx: ToolContext, _params: types.PaginatedRequestParams | None = None) -> types.ListToolsResult:
    tools = [
        types.Tool(
            name="apply_file_edit",
            description="Apply precise string replacements to a file within the worktree.",
            inputSchema={
                "type": "object",
                "properties": {
                    "filepath": {"type": "string"},
                    "old_string": {"type": "string"},
                    "new_string": {"type": "string"},
                    "replace_all": {"type": "boolean", "default": False},
                },
                "required": ["filepath", "old_string", "new_string"],
            },
        ),
        types.Tool(
            name="propose_file_patch",
            description="Propose a full content write/patch for a file within the worktree.",
            inputSchema={
                "type": "object",
                "properties": {
                    "filepath": {"type": "string"},
                    "content": {"type": "string"},
                    "is_new_file": {"type": "boolean", "default": False},
                },
                "required": ["filepath", "content"],
            },
        ),
        types.Tool(
            name="fetch_symbol_definition",
            description="Fetch symbol definition/stub from the Knowledge Graph.",
            inputSchema={
                "type": "object",
                "properties": {
                    "symbol_id": {"type": "string"},
                },
                "required": ["symbol_id"],
            },
        ),
        types.Tool(
            name="trigger_sandbox_validation",
            description="Trigger automated test validation inside the ephemeral sandbox.",
            inputSchema={
                "type": "object",
                "properties": {},
            },
        ),
        types.Tool(
            name="git_status",
            description="Inspect git repository status including branch, cleanliness, staged/unstaged/untracked files, and ahead/behind counts.",
            inputSchema={
                "type": "object",
                "properties": {
                    "repo_path": {"type": "string", "default": "."},
                },
            },
        ),
        types.Tool(
            name="git_pull_main",
            description="Safely pull or fetch main branch updates without corrupting uncommitted changes.",
            inputSchema={
                "type": "object",
                "properties": {
                    "repo_path": {"type": "string", "default": "."},
                    "main_branch": {"type": "string", "default": "main"},
                    "remote": {"type": "string", "default": "origin"},
                },
            },
        ),
        types.Tool(
            name="git_create_branch",
            description="Safely create a new Git branch with optional checkout and start point.",
            inputSchema={
                "type": "object",
                "properties": {
                    "branch_name": {"type": "string"},
                    "repo_path": {"type": "string", "default": "."},
                    "start_point": {"type": "string"},
                    "checkout": {"type": "boolean", "default": True},
                },
                "required": ["branch_name"],
            },
        ),
        types.Tool(
            name="git_diff_summary",
            description="Generate a structured diff summary for working tree, staged changes, or a target ref.",
            inputSchema={
                "type": "object",
                "properties": {
                    "repo_path": {"type": "string", "default": "."},
                    "target": {"type": "string"},
                    "cached": {"type": "boolean", "default": False},
                },
            },
        ),
    ]
    return types.ListToolsResult(tools=tools)


async def dispatch_tool_call(
    ctx: ToolContext, name: str, arguments: dict[str, object] | None = None
) -> types.CallToolResult:
    args = arguments or {}

    try:
        if name == "apply_file_edit":
            if "filepath" not in args or "old_string" not in args or "new_string" not in args:
                return _error_result("Missing required arguments for apply_file_edit (filepath, old_string, new_string).")
            return await apply_file_edit(
                ctx,
                str(args["filepath"]),
                str(args["old_string"]),
                str(args["new_string"]),
                bool(args.get("replace_all", False)),
            )

        elif name == "propose_file_patch":
            if "filepath" not in args or "content" not in args:
                return _error_result("Missing required arguments for propose_file_patch (filepath, content).")
            return await propose_file_patch(
                ctx,
                str(args["filepath"]),
                str(args["content"]),
                bool(args.get("is_new_file", False)),
            )

        elif name == "fetch_symbol_definition":
            if "symbol_id" not in args:
                return _error_result("Missing required argument 'symbol_id' for fetch_symbol_definition.")
            return await fetch_symbol_definition(ctx, str(args["symbol_id"]))

        elif name == "trigger_sandbox_validation":
            return await trigger_sandbox_validation(ctx)

        elif name == "git_status":
            repo_rel = str(args.get("repo_path", "."))
            repo_path = _resolve_within_worktree(ctx.worktree_root, repo_rel)
            res = git_tools.git_status(repo_path)
            if not res.get("success"):
                return _error_result(res.get("error", "git_status failed."))
            return _text_result(json.dumps(res, indent=2))

        elif name == "git_pull_main":
            repo_rel = str(args.get("repo_path", "."))
            repo_path = _resolve_within_worktree(ctx.worktree_root, repo_rel)
            main_branch = str(args.get("main_branch", "main"))
            remote = str(args.get("remote", "origin"))
            res = git_tools.git_pull_main(repo_path, main_branch=main_branch, remote=remote)
            if not res.get("success"):
                return _error_result(res.get("error", "git_pull_main failed."))
            msg = res.get("message") or json.dumps(res, indent=2)
            return _text_result(msg)

        elif name == "git_create_branch":
            if "branch_name" not in args:
                return _error_result("Missing required argument 'branch_name' for git_create_branch.")
            branch_name = str(args["branch_name"])
            repo_rel = str(args.get("repo_path", "."))
            repo_path = _resolve_within_worktree(ctx.worktree_root, repo_rel)
            start_point = str(args["start_point"]) if args.get("start_point") is not None else None
            checkout = bool(args.get("checkout", True))
            res = git_tools.git_create_branch(
                branch_name, repo_path=repo_path, start_point=start_point, checkout=checkout
            )
            if not res.get("success"):
                return _error_result(res.get("error", "git_create_branch failed."))
            msg = res.get("message") or json.dumps(res, indent=2)
            return _text_result(msg)

        elif name == "git_diff_summary":
            repo_rel = str(args.get("repo_path", "."))
            repo_path = _resolve_within_worktree(ctx.worktree_root, repo_rel)
            target = str(args["target"]) if args.get("target") is not None else None
            cached = bool(args.get("cached", False))
            res = git_tools.git_diff_summary(repo_path, target=target, cached=cached)
            if not res.get("success"):
                return _error_result(res.get("error", "git_diff_summary failed."))
            return _text_result(json.dumps(res, indent=2))

        else:
            return _error_result(f"Unknown tool name: '{name}'.")
    except PathTraversalError as err:
        return _error_result(str(err))
    except Exception as exc:
        return _error_result(str(exc))


async def handle_call_tool(ctx: ToolContext, params: types.CallToolRequestParams) -> types.CallToolResult:
    return await dispatch_tool_call(ctx, params.name, params.arguments)


def build_server(ctx: ToolContext) -> Any:
    server = Server("ai-os-mcp-server")
    return server


async def run_stdio(ctx: ToolContext) -> None:
    server = build_server(ctx)
    if hasattr(server, "run"):
        await server.run()


def main() -> None:
    config = ServerConfig.from_env()
    ctx = ToolContext.from_config(config)
    asyncio.run(run_stdio(ctx))