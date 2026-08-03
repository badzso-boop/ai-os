"""Tests for the completion-based agent turn executor + patch parser
(`ai_os.core.task_runner`), driven by a fake adapter — no real LLM calls."""
from __future__ import annotations

import pytest

from ai_os.core.task_runner import (
    AgentTurnContext,
    AgentTurnError,
    build_completion_agent_turn_executor,
    parse_file_patches,
)
from ai_os.core.models import TaskNode
from ai_os.mcp.adapters.base_adapter import BaseMCPAdapter, LLMTaskRequest, LLMTaskResponse, TokenUsage


class _CannedAdapter(BaseMCPAdapter):
    def __init__(self, text: str) -> None:
        self.text = text
        self.requests: list[LLMTaskRequest] = []

    async def execute_task(self, request: LLMTaskRequest) -> LLMTaskResponse:
        self.requests.append(request)
        return LLMTaskResponse(
            task_id=request.task_id, provider="fake", model_name=request.model or "fake-model",
            generated_text=self.text, usage=TokenUsage(),
        )


def _task(**overrides) -> TaskNode:
    base = dict(id="T1", title="t", description="d", risk_level="LOW", target_files=["a.py"])
    base.update(overrides)
    return TaskNode(**base)


def _ctx(worktree, task=None) -> AgentTurnContext:
    return AgentTurnContext(
        task=task or _task(), worktree_path=worktree, context_cache="ctx", attempt=1,
        previous_validation_output=None,
    )


def test_parse_single_file():
    text = "<<<AI_OS_FILE: src/foo.py>>>\nprint('hi')\n<<<AI_OS_END>>>"
    assert parse_file_patches(text) == {"src/foo.py": "print('hi')"}


def test_parse_multiple_files_and_ignores_surrounding_prose():
    text = (
        "Here is my solution:\n"
        "<<<AI_OS_FILE: a.py>>>\nA = 1\n<<<AI_OS_END>>>\n"
        "and another:\n"
        "<<<AI_OS_FILE: pkg/b.py>>>\ndef b():\n    return 2\n<<<AI_OS_END>>>\n"
        "Done!"
    )
    assert parse_file_patches(text) == {"a.py": "A = 1", "pkg/b.py": "def b():\n    return 2"}


def test_parse_preserves_content_with_backticks():
    # Content containing markdown fences must survive (the reason we use
    # sentinels, not ``` fences).
    text = "<<<AI_OS_FILE: r.md>>>\n```python\nx=1\n```\n<<<AI_OS_END>>>"
    assert parse_file_patches(text) == {"r.md": "```python\nx=1\n```"}


def test_parse_returns_empty_when_no_blocks():
    assert parse_file_patches("just some prose, no blocks") == {}


async def test_executor_writes_files_into_worktree(tmp_path):
    adapter = _CannedAdapter("<<<AI_OS_FILE: sub/dir/foo.py>>>\nVALUE = 42\n<<<AI_OS_END>>>")
    executor = build_completion_agent_turn_executor(adapter, model="some-model")
    await executor(_ctx(tmp_path))
    assert (tmp_path / "sub/dir/foo.py").read_text() == "VALUE = 42"
    # the model override propagated into the request
    assert adapter.requests[0].model == "some-model"


async def test_executor_raises_when_no_blocks(tmp_path):
    executor = build_completion_agent_turn_executor(_CannedAdapter("I refuse to answer."))
    with pytest.raises(AgentTurnError):
        await executor(_ctx(tmp_path))


async def test_executor_rejects_path_traversal(tmp_path):
    adapter = _CannedAdapter("<<<AI_OS_FILE: ../../etc/evil.py>>>\nx=1\n<<<AI_OS_END>>>")
    executor = build_completion_agent_turn_executor(adapter)
    with pytest.raises(AgentTurnError):
        await executor(_ctx(tmp_path))
    assert not (tmp_path.parent.parent / "etc/evil.py").exists()


async def test_executor_includes_previous_output_on_retry(tmp_path):
    adapter = _CannedAdapter("<<<AI_OS_FILE: a.py>>>\nfixed=1\n<<<AI_OS_END>>>")
    executor = build_completion_agent_turn_executor(adapter)
    ctx = AgentTurnContext(
        task=_task(), worktree_path=tmp_path, context_cache="ctx", attempt=2,
        previous_validation_output="AssertionError: expected 5",
    )
    await executor(ctx)
    assert "AssertionError: expected 5" in adapter.requests[0].context_payload
