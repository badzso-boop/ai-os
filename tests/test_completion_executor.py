"""Tests for the completion-based agent turn executor + patch parser
(`ai_os.core.task_runner`), driven by a fake adapter — no real LLM calls."""
from __future__ import annotations

import pytest

from ai_os.core.task_runner import (
    AgentTurnContext,
    AgentTurnError,
    _EditAction,
    _WriteAction,
    build_completion_agent_turn_executor,
    parse_agent_actions,
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


async def test_executor_injects_project_conventions(tmp_path):
    adapter = _CannedAdapter("<<<AI_OS_FILE: a.py>>>\nx=1\n<<<AI_OS_END>>>")
    executor = build_completion_agent_turn_executor(adapter)
    ctx = AgentTurnContext(
        task=_task(), worktree_path=tmp_path, context_cache="ctx", attempt=1,
        previous_validation_output=None, project_conventions="- All strings via i18n t('key')",
    )
    await executor(ctx)
    assert "Project conventions" in adapter.requests[0].context_payload
    assert "i18n t('key')" in adapter.requests[0].context_payload


async def test_executor_includes_previous_output_on_retry(tmp_path):
    adapter = _CannedAdapter("<<<AI_OS_FILE: a.py>>>\nfixed=1\n<<<AI_OS_END>>>")
    executor = build_completion_agent_turn_executor(adapter)
    ctx = AgentTurnContext(
        task=_task(), worktree_path=tmp_path, context_cache="ctx", attempt=2,
        previous_validation_output="AssertionError: expected 5",
    )
    await executor(ctx)
    assert "AssertionError: expected 5" in adapter.requests[0].context_payload


# -- edit-block (search/replace) parsing + application (Stage 2) --------------


def test_parse_agent_actions_write_block():
    actions = parse_agent_actions("<<<AI_OS_FILE: a.py>>>\nA = 1\n<<<AI_OS_END>>>")
    assert actions == [_WriteAction(path="a.py", content="A = 1")]


def test_parse_agent_actions_edit_block():
    text = (
        "<<<AI_OS_EDIT: a.py>>>\n"
        "<<<AI_OS_SEARCH>>>\n"
        "old = 1\n"
        "<<<AI_OS_REPLACE>>>\n"
        "old = 2\n"
        "<<<AI_OS_END>>>"
    )
    assert parse_agent_actions(text) == [_EditAction(path="a.py", search="old = 1", replace="old = 2")]


def test_parse_agent_actions_preserves_document_order():
    text = (
        "<<<AI_OS_FILE: new.py>>>\nX = 1\n<<<AI_OS_END>>>\n"
        "<<<AI_OS_EDIT: old.py>>>\n<<<AI_OS_SEARCH>>>\nq\n<<<AI_OS_REPLACE>>>\nr\n<<<AI_OS_END>>>"
    )
    actions = parse_agent_actions(text)
    assert [type(a).__name__ for a in actions] == ["_WriteAction", "_EditAction"]
    assert actions[0].path == "new.py" and actions[1].path == "old.py"


async def test_executor_applies_edit_to_existing_file(tmp_path):
    (tmp_path / "a.py").write_text("value = 1\nkeep = 9\n")
    adapter = _CannedAdapter(
        "<<<AI_OS_EDIT: a.py>>>\n<<<AI_OS_SEARCH>>>\nvalue = 1\n<<<AI_OS_REPLACE>>>\nvalue = 2\n<<<AI_OS_END>>>"
    )
    executor = build_completion_agent_turn_executor(adapter)
    await executor(_ctx(tmp_path))
    assert (tmp_path / "a.py").read_text() == "value = 2\nkeep = 9\n"


async def test_executor_edit_missing_search_raises(tmp_path):
    (tmp_path / "a.py").write_text("value = 1\n")
    adapter = _CannedAdapter(
        "<<<AI_OS_EDIT: a.py>>>\n<<<AI_OS_SEARCH>>>\nnope\n<<<AI_OS_REPLACE>>>\nx\n<<<AI_OS_END>>>"
    )
    executor = build_completion_agent_turn_executor(adapter)
    with pytest.raises(AgentTurnError):
        await executor(_ctx(tmp_path))


async def test_executor_edit_on_missing_file_raises(tmp_path):
    adapter = _CannedAdapter(
        "<<<AI_OS_EDIT: ghost.py>>>\n<<<AI_OS_SEARCH>>>\na\n<<<AI_OS_REPLACE>>>\nb\n<<<AI_OS_END>>>"
    )
    executor = build_completion_agent_turn_executor(adapter)
    with pytest.raises(AgentTurnError):
        await executor(_ctx(tmp_path))


async def test_executor_write_then_edit_same_file_in_order(tmp_path):
    # A write creates the file, then an edit in the same turn modifies it.
    adapter = _CannedAdapter(
        "<<<AI_OS_FILE: a.py>>>\nx = 1\ny = 1\n<<<AI_OS_END>>>\n"
        "<<<AI_OS_EDIT: a.py>>>\n<<<AI_OS_SEARCH>>>\ny = 1\n<<<AI_OS_REPLACE>>>\ny = 2\n<<<AI_OS_END>>>"
    )
    executor = build_completion_agent_turn_executor(adapter)
    await executor(_ctx(tmp_path))
    # The AI_OS_FILE parser strips the trailing newline before AI_OS_END, so the
    # created file has no trailing "\n"; the edit then swaps y = 1 -> y = 2.
    assert (tmp_path / "a.py").read_text() == "x = 1\ny = 2"
