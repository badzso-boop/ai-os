"""Tests for `ai_os.mcp.mcp_server`.

Two layers, per the module's own strategy note: fast in-process tests
against the plain async tool functions (most of the coverage), plus one real
stdio protocol round-trip that spawns the actual server module as a
subprocess and drives it with the `mcp` SDK's own client machinery
(`mcp.client.stdio` + `mcp.client.session.ClientSession`) — the genuine
two-sided protocol conversation that proves wire compatibility with whatever
the real `claude` CLI would negotiate. No LLM is involved anywhere; the
"agent" side of this conversation is the test itself.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from ai_os.analyzer.tree_sitter_engine import Symbol
from ai_os.knowledge.graph_engine import KnowledgeEngine
from ai_os.mcp.mcp_server import (
    ServerConfig,
    ToolContext,
    dispatch_tool_call,
    fetch_symbol_definition,
    propose_file_patch,
    trigger_sandbox_validation,
)
from ai_os.sandbox.container_runner import ValidationResult
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client


class _FakeSandboxRunner:
    """Scripted stand-in for `EphemeralSandboxRunner` — no real Docker
    involved. Either returns a fixed `ValidationResult` or raises a fixed
    exception, and records every call it received."""

    def __init__(self, result: ValidationResult | None = None, exc: Exception | None = None) -> None:
        self.result = result
        self.exc = exc
        self.calls: list[tuple[Path, str]] = []

    async def run_validation(self, worktree_path: Path, language: str) -> ValidationResult:
        self.calls.append((worktree_path, language))
        if self.exc is not None:
            raise self.exc
        assert self.result is not None
        return self.result


def _make_symbol(fqn: str = "src/foo.py::Foo.bar", relpath: str = "src/foo.py") -> Symbol:
    return Symbol(
        kind="function",
        name="bar",
        fqn=fqn,
        language="python",
        relpath=relpath,
        start_line=1,
        end_line=2,
        start_byte=0,
        end_byte=10,
        body_start_byte=5,
    )


def _engine_with_symbol(stub: str | None = "def bar(): ...") -> KnowledgeEngine:
    engine = KnowledgeEngine()
    engine.add_file_node("src/foo.py", "python")
    engine.add_symbol_node(_make_symbol(), stub=stub)
    return engine


def _ctx(
    tmp_path: Path,
    *,
    engine: KnowledgeEngine | None = None,
    sandbox_runner: _FakeSandboxRunner | None = None,
    sandbox_language: str | None = "python",
) -> ToolContext:
    return ToolContext(
        worktree_path=tmp_path,
        knowledge_engine=engine,
        graph_load_error=None if engine is not None else "No knowledge graph configured (test fixture).",
        sandbox_runner=sandbox_runner or _FakeSandboxRunner(),
        sandbox_language=sandbox_language,
    )


# -- propose_file_patch --------------------------------------------------------------


async def test_propose_file_patch_writes_new_file(tmp_path):
    ctx = _ctx(tmp_path)
    result = await propose_file_patch(ctx, "new_file.py", "print('hi')\n", True)

    assert result.is_error is False
    assert "SUCCESS" in result.content[0].text
    assert (tmp_path / "new_file.py").read_text(encoding="utf-8") == "print('hi')\n"


async def test_propose_file_patch_creates_parent_directories(tmp_path):
    ctx = _ctx(tmp_path)
    result = await propose_file_patch(ctx, "a/b/c/d.txt", "data", False)

    assert result.is_error is False
    assert (tmp_path / "a" / "b" / "c" / "d.txt").read_text(encoding="utf-8") == "data"


async def test_propose_file_patch_overwrites_existing_file(tmp_path):
    (tmp_path / "existing.txt").write_text("old", encoding="utf-8")
    ctx = _ctx(tmp_path)
    result = await propose_file_patch(ctx, "existing.txt", "new", False)

    assert result.is_error is False
    assert (tmp_path / "existing.txt").read_text(encoding="utf-8") == "new"


@pytest.mark.parametrize(
    "bad_path",
    [
        "../escape.txt",
        "../../etc/passwd",
        "a/../../escape.txt",
        "a/b/../../../escape.txt",
    ],
)
async def test_propose_file_patch_rejects_relative_traversal(tmp_path, bad_path):
    ctx = _ctx(tmp_path)
    result = await propose_file_patch(ctx, bad_path, "malicious content", False)

    assert result.is_error is True
    assert "REJECTED" in result.content[0].text
    # Nothing escaped into the parent of tmp_path.
    assert not (tmp_path.parent / "escape.txt").exists()


async def test_propose_file_patch_rejects_absolute_path(tmp_path):
    ctx = _ctx(tmp_path)
    result = await propose_file_patch(ctx, "/etc/passwd", "malicious content", False)

    assert result.is_error is True
    assert "REJECTED" in result.content[0].text


# -- fetch_symbol_definition ----------------------------------------------------------


async def test_fetch_symbol_definition_hit_returns_stub(tmp_path):
    ctx = _ctx(tmp_path, engine=_engine_with_symbol(stub="def bar(): ..."))
    result = await fetch_symbol_definition(ctx, "src/foo.py::Foo.bar")

    assert result.is_error is False
    assert result.content[0].text == "def bar(): ..."


async def test_fetch_symbol_definition_miss_returns_clear_error(tmp_path):
    ctx = _ctx(tmp_path, engine=_engine_with_symbol())
    result = await fetch_symbol_definition(ctx, "src/foo.py::DoesNotExist")

    assert result.is_error is True
    assert "not found" in result.content[0].text.lower()


async def test_fetch_symbol_definition_symbol_with_no_stub(tmp_path):
    ctx = _ctx(tmp_path, engine=_engine_with_symbol(stub=None))
    result = await fetch_symbol_definition(ctx, "src/foo.py::Foo.bar")

    assert result.is_error is True
    assert "no stub" in result.content[0].text.lower()


async def test_fetch_symbol_definition_file_node_has_no_stub(tmp_path):
    ctx = _ctx(tmp_path, engine=_engine_with_symbol())
    result = await fetch_symbol_definition(ctx, "src/foo.py")  # a FileNode, not a symbol

    assert result.is_error is True


async def test_fetch_symbol_definition_no_graph_configured(tmp_path):
    ctx = _ctx(tmp_path, engine=None)
    result = await fetch_symbol_definition(ctx, "anything::at.all")

    assert result.is_error is True
    assert "no knowledge graph" in result.content[0].text.lower()


# -- trigger_sandbox_validation --------------------------------------------------------


async def test_trigger_sandbox_validation_success(tmp_path):
    fake = _FakeSandboxRunner(
        result=ValidationResult(success=True, exit_code=0, summary="ok", output="all tests passed")
    )
    ctx = _ctx(tmp_path, sandbox_runner=fake, sandbox_language="python")
    result = await trigger_sandbox_validation(ctx)

    assert result.is_error is False
    text = result.content[0].text
    assert "VALIDATION PASSED" in text
    assert "Exit Code: 0" in text
    assert "all tests passed" in text
    assert fake.calls == [(tmp_path, "python")]


async def test_trigger_sandbox_validation_failure_is_tool_error(tmp_path):
    fake = _FakeSandboxRunner(
        result=ValidationResult(success=False, exit_code=1, summary="fail", output="1 test failed")
    )
    ctx = _ctx(tmp_path, sandbox_runner=fake)
    result = await trigger_sandbox_validation(ctx)

    assert result.is_error is True
    text = result.content[0].text
    assert "VALIDATION FAILED" in text
    assert "Exit Code: 1" in text
    assert "1 test failed" in text


async def test_trigger_sandbox_validation_infra_fault_becomes_tool_error_not_crash(tmp_path):
    fake = _FakeSandboxRunner(exc=RuntimeError("docker executable not found"))
    ctx = _ctx(tmp_path, sandbox_runner=fake)
    result = await trigger_sandbox_validation(ctx)

    assert result.is_error is True
    assert "docker executable not found" in result.content[0].text


async def test_trigger_sandbox_validation_no_language_configured(tmp_path):
    ctx = _ctx(tmp_path, sandbox_language=None)
    result = await trigger_sandbox_validation(ctx)

    assert result.is_error is True
    assert "AI_OS_SANDBOX_LANGUAGE" in result.content[0].text


# -- dispatch_tool_call ----------------------------------------------------------------


async def test_dispatch_unknown_tool_name(tmp_path):
    ctx = _ctx(tmp_path)
    result = await dispatch_tool_call(ctx, "not_a_real_tool", {})

    assert result.is_error is True
    assert "Unknown tool" in result.content[0].text


async def test_dispatch_missing_required_arguments_is_tool_error_not_crash(tmp_path):
    ctx = _ctx(tmp_path)
    result = await dispatch_tool_call(ctx, "propose_file_patch", {"filepath": "x.txt"})

    assert result.is_error is True
    assert "Invalid arguments" in result.content[0].text


# -- ServerConfig.from_env --------------------------------------------------------------


def test_server_config_from_env_requires_worktree_path():
    with pytest.raises(ValueError):
        ServerConfig.from_env(environ={})


def test_server_config_from_env_parses_all_fields(tmp_path):
    graph_path = tmp_path / "graph.json"
    config = ServerConfig.from_env(
        environ={
            "AI_OS_WORKTREE_PATH": str(tmp_path),
            "AI_OS_GRAPH_JSON_PATH": str(graph_path),
            "AI_OS_SANDBOX_LANGUAGE": "typescript",
        }
    )

    assert config.worktree_path == Path(tmp_path)
    assert config.graph_json_path == graph_path
    assert config.sandbox_language == "typescript"


def test_server_config_from_env_graph_and_language_are_optional(tmp_path):
    config = ServerConfig.from_env(environ={"AI_OS_WORKTREE_PATH": str(tmp_path)})

    assert config.worktree_path == Path(tmp_path)
    assert config.graph_json_path is None
    assert config.sandbox_language is None


# -- real MCP protocol round-trip (subprocess + real client) ----------------------------


async def test_real_stdio_protocol_round_trip(tmp_path):
    """Spawns the actual `python -m ai_os.mcp.mcp_server` module as a real
    subprocess, drives it through `initialize` -> `list_tools` -> `call_tool`
    over real stdio using the SDK's own client transport/session, and
    verifies the file genuinely landed on disk. This is the load-bearing
    test for this module: it exercises the real `mcp` client/server code
    paths end to end, not anything mocked."""
    worktree = tmp_path / "worktree"
    worktree.mkdir()

    graph_path = tmp_path / "graph.json"
    _engine_with_symbol(stub="def bar(): ...").to_json(graph_path)

    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "ai_os.mcp.mcp_server"],
        env={
            "AI_OS_WORKTREE_PATH": str(worktree),
            "AI_OS_GRAPH_JSON_PATH": str(graph_path),
            "AI_OS_SANDBOX_LANGUAGE": "python",
        },
    )

    async with stdio_client(params) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()

            tools_result = await session.list_tools()
            tool_names = {tool.name for tool in tools_result.tools}
            assert tool_names == {
                "propose_file_patch",
                "fetch_symbol_definition",
                "trigger_sandbox_validation",
            }
            for tool in tools_result.tools:
                assert tool.description and len(tool.description) > 10

            call_result = await session.call_tool(
                "propose_file_patch",
                {"filepath": "hello.txt", "content": "hi from a real mcp round-trip\n", "is_new_file": True},
            )
            assert not call_result.is_error
            assert (worktree / "hello.txt").read_text(encoding="utf-8") == "hi from a real mcp round-trip\n"

            symbol_result = await session.call_tool(
                "fetch_symbol_definition", {"symbol_id": "src/foo.py::Foo.bar"}
            )
            assert not symbol_result.is_error
            assert symbol_result.content[0].text == "def bar(): ..."

            missing_result = await session.call_tool(
                "fetch_symbol_definition", {"symbol_id": "does::not.exist"}
            )
            assert missing_result.is_error
