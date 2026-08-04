"""Tests for the multi-provider autonomous tool-calling path (Phase 5 Stage 1).

Covers `build_tool_calling_agent_turn_executor` (task_runner.py) and its wiring
into `EpicRunner._build_executor`'s third branch. A FAKE tool-calling adapter
stands in for a real Gemini/OpenRouter/Anthropic-API model: its
`execute_with_tools` drives the SAME `dispatch` the real loop would — calling
`propose_file_patch` then `trigger_sandbox_validation` against the real MCP tool
implementations — so we prove the bridge (provider function-calling ->
`ai_os.mcp.mcp_server.dispatch_tool_call`) end-to-end without any real LLM,
network, or Docker. Real git worktrees + a fake sandbox, Phase 2's style.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from ai_os.core.epic_runner import EpicRunner
from ai_os.core.models import TaskNode
from ai_os.core.scheduler import DynamicScheduler
from ai_os.core.task_runner import (
    AgentTurnContext,
    build_tool_calling_agent_turn_executor,
)
from ai_os.knowledge.graph_engine import KnowledgeEngine
from ai_os.mcp.adapters.base_adapter import (
    BaseMCPAdapter,
    LLMTaskRequest,
    LLMTaskResponse,
    ToolDispatch,
    ToolSpec,
    TokenUsage,
)
from ai_os.mcp.protocol_router import ProtocolRouter
from ai_os.sandbox.container_runner import ValidationResult


# -- fakes -------------------------------------------------------------------


class _ToolUsingAdapter(BaseMCPAdapter):
    """A model that, given tools, writes `<task_id>.py` via propose_file_patch
    then validates via trigger_sandbox_validation — recording what it saw so
    the test can assert the loop really ran against the real tool dispatch."""

    def __init__(self) -> None:
        self.tool_names_offered: list[str] = []
        self.dispatch_calls: list[tuple[str, dict]] = []
        self.models_seen: list[str | None] = []

    def supports_tool_calling(self) -> bool:
        return True

    async def execute_task(self, request: LLMTaskRequest) -> LLMTaskResponse:  # pragma: no cover - unused
        raise AssertionError("tool-calling path should call execute_with_tools, not execute_task")

    async def execute_with_tools(
        self,
        request: LLMTaskRequest,
        tools: list[ToolSpec],
        dispatch: ToolDispatch,
        max_tool_iterations: int = 25,
    ) -> LLMTaskResponse:
        self.tool_names_offered = [t.name for t in tools]
        self.models_seen.append(request.model)

        filename = f"{request.task_id}.py"
        patch_args = {
            "filepath": filename,
            "content": f"TASK_ID = {request.task_id!r}\n",
            "is_new_file": True,
        }
        self.dispatch_calls.append(("propose_file_patch", patch_args))
        await dispatch("propose_file_patch", patch_args)

        self.dispatch_calls.append(("trigger_sandbox_validation", {}))
        await dispatch("trigger_sandbox_validation", {})

        return LLMTaskResponse(
            task_id=request.task_id,
            provider="gemini",
            model_name=request.model or "fake",
            generated_text="done",
            usage=TokenUsage(input_tokens=5, output_tokens=3),
        )


class _AlwaysPassSandbox:
    def __init__(self) -> None:
        self.calls = 0

    async def run_validation(self, worktree_path: Path, language: str) -> ValidationResult:
        self.calls += 1
        return ValidationResult(success=True, exit_code=0, summary="ok", output="ok")


@pytest.fixture()
def git_repo(tmp_path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@ai-os.local"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "AI-OS Test"], cwd=repo, check=True)
    (repo / "README.md").write_text("init\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True)
    return repo


# -- unit: the executor drives the real dispatch against a real worktree -----


async def test_executor_writes_file_and_validates_via_dispatch(tmp_path: Path):
    worktree = tmp_path / "wt"
    worktree.mkdir()
    adapter = _ToolUsingAdapter()
    sandbox = _AlwaysPassSandbox()
    executor = build_tool_calling_agent_turn_executor(
        adapter=adapter,
        model="gemini-flash",
        knowledge_engine=KnowledgeEngine(),
        sandbox_runner=sandbox,
        sandbox_language="python",
    )
    task = TaskNode(
        id="T1", title="t", description="write it", risk_level="LOW",
        target_files=["T1.py"], write_set={"T1.py"},
    )
    ctx = AgentTurnContext(
        task=task, worktree_path=worktree, context_cache="ctx",
        attempt=1, previous_validation_output=None,
    )

    await executor(ctx)

    # The tool dispatch really wrote into the worktree (via the MCP
    # propose_file_patch implementation, not any reimplementation).
    assert (worktree / "T1.py").read_text() == "TASK_ID = 'T1'\n"
    # The sandbox tool really ran through the shared runner.
    assert sandbox.calls == 1
    # The 3 real MCP tools were offered to the model, and the model saw the model override.
    assert set(adapter.tool_names_offered) == {
        "propose_file_patch", "fetch_symbol_definition", "trigger_sandbox_validation",
    }
    assert adapter.models_seen == ["gemini-flash"]


async def test_executor_fetch_symbol_definition_uses_shared_engine(tmp_path: Path):
    worktree = tmp_path / "wt"
    worktree.mkdir()

    captured: dict[str, str] = {}

    class _SymbolLookupAdapter(_ToolUsingAdapter):
        async def execute_with_tools(self, request, tools, dispatch, max_tool_iterations=25):
            captured["stub"] = await dispatch(
                "fetch_symbol_definition", {"symbol_id": "mod.py::Foo"}
            )
            return LLMTaskResponse(
                task_id=request.task_id, provider="gemini",
                model_name="fake", generated_text="ok", usage=TokenUsage(),
            )

    engine = KnowledgeEngine()
    engine.graph.add_node("mod.py::Foo", node_type="ClassNode", stub="class Foo: ...")

    executor = build_tool_calling_agent_turn_executor(
        adapter=_SymbolLookupAdapter(),
        model=None,
        knowledge_engine=engine,
        sandbox_runner=_AlwaysPassSandbox(),
        sandbox_language="python",
    )
    task = TaskNode(id="T1", title="t", description="d", risk_level="LOW", target_files=["a.py"], write_set={"a.py"})
    ctx = AgentTurnContext(
        task=task, worktree_path=worktree, context_cache="c",
        attempt=1, previous_validation_output=None,
    )
    await executor(ctx)
    assert captured["stub"] == "class Foo: ..."


# -- integration: EpicRunner picks the tool-calling branch for such an adapter


async def test_epic_runner_routes_tool_capable_adapter_through_tool_loop(git_repo: Path):
    adapter = _ToolUsingAdapter()
    router = ProtocolRouter(
        {"gemini": adapter},
        risk_provider_order={lvl: ["gemini"] for lvl in ("LOW", "MEDIUM", "HIGH", "CRITICAL")},
    )
    scheduler = DynamicScheduler(router, environ={})
    sandbox = _AlwaysPassSandbox()
    runner = EpicRunner(
        repo_root=git_repo, scheduler=scheduler, adapters={"gemini": adapter},
        language="python", sandbox_runner=sandbox,
    )

    task = TaskNode(
        id="ALPHA", title="alpha", description="write ALPHA.py", risk_level="LOW",
        target_files=["ALPHA.py"], write_set={"ALPHA.py"},
    )
    result = await runner.run_epic([task])

    assert result.completed == ["ALPHA"]
    # The tool-calling loop actually ran (execute_with_tools, not execute_task):
    assert adapter.dispatch_calls[0][0] == "propose_file_patch"
    # And its patch merged to main.
    assert (git_repo / "ALPHA.py").read_text() == "TASK_ID = 'ALPHA'\n"
