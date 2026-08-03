"""Tests for `ai_os.core.epic_planner` — parsing + decomposition against a
fake adapter feeding canned plan JSON. No real LLM calls."""
from __future__ import annotations

import json

import pytest

from ai_os.core.epic_planner import (
    EpicPlanError,
    build_repo_summary,
    decompose,
    parse_task_plan,
)
from ai_os.knowledge.graph_engine import KnowledgeEngine
from ai_os.mcp.adapters.base_adapter import BaseMCPAdapter, LLMTaskRequest, LLMTaskResponse, TokenUsage


class _ScriptedAdapter(BaseMCPAdapter):
    """Returns the given responses in order, one per call."""

    def __init__(self, *responses: str) -> None:
        self.responses = list(responses)
        self.requests: list[LLMTaskRequest] = []

    async def execute_task(self, request: LLMTaskRequest) -> LLMTaskResponse:
        self.requests.append(request)
        text = self.responses[min(len(self.requests) - 1, len(self.responses) - 1)]
        return LLMTaskResponse(
            task_id=request.task_id, provider="fake", model_name="fake", generated_text=text, usage=TokenUsage()
        )


_VALID_PLAN = json.dumps(
    [
        {"id": "T1", "title": "types", "description": "add types", "risk_level": "LOW",
         "target_files": ["types.py"], "write_set": ["types.py"], "dependencies": []},
        {"id": "T2", "title": "service", "description": "add service", "risk_level": "HIGH",
         "target_files": ["service.py"], "write_set": ["service.py"], "dependencies": ["T1"]},
    ]
)


def test_parse_valid_plan():
    tasks = parse_task_plan(_VALID_PLAN)
    assert [t.id for t in tasks] == ["T1", "T2"]
    assert tasks[1].dependencies == ["T1"]
    assert tasks[1].risk_level == "HIGH"


def test_parse_strips_markdown_fences_and_prose():
    text = "Here's the plan:\n```json\n" + _VALID_PLAN + "\n```\nHope that helps!"
    tasks = parse_task_plan(text)
    assert [t.id for t in tasks] == ["T1", "T2"]


def test_parse_tolerates_extra_keys():
    plan = json.dumps(
        [{"id": "T1", "title": "x", "description": "y", "risk_level": "LOW",
          "target_files": ["a.py"], "write_set": ["a.py"], "dependencies": [],
          "rationale": "some extra field the model added"}]
    )
    tasks = parse_task_plan(plan)
    assert tasks[0].id == "T1"


def test_parse_malformed_json_raises_with_raw():
    with pytest.raises(EpicPlanError) as exc:
        parse_task_plan("not json at all [oops")
    assert exc.value.raw  # raw text preserved for feedback


def test_parse_rejects_bad_task_entry():
    with pytest.raises(EpicPlanError):
        parse_task_plan(json.dumps([{"id": "T1"}]))  # missing required fields


def test_build_repo_summary_lists_files():
    engine = KnowledgeEngine()
    engine.add_file_node("src/foo.py", "python")
    summary = build_repo_summary(engine)
    assert "src/foo.py" in summary


async def test_decompose_happy_path():
    adapter = _ScriptedAdapter(_VALID_PLAN)
    engine = KnowledgeEngine()
    engine.add_file_node("types.py", "python")
    tasks = await decompose("build a thing", engine, adapter, model="opus")
    assert [t.id for t in tasks] == ["T1", "T2"]
    assert adapter.requests[0].model == "opus"


async def test_decompose_retries_after_malformed_then_succeeds():
    adapter = _ScriptedAdapter("garbage not json", _VALID_PLAN)
    tasks = await decompose("build a thing", KnowledgeEngine(), adapter, max_parse_retries=1)
    assert [t.id for t in tasks] == ["T1", "T2"]
    assert len(adapter.requests) == 2
    # the retry prompt fed the parse error back to the model
    assert "could not be used" in adapter.requests[1].context_payload


async def test_decompose_rejects_cyclic_plan():
    cyclic = json.dumps(
        [
            {"id": "A", "title": "a", "description": "a", "risk_level": "LOW",
             "target_files": [], "write_set": [], "dependencies": ["B"]},
            {"id": "B", "title": "b", "description": "b", "risk_level": "LOW",
             "target_files": [], "write_set": [], "dependencies": ["A"]},
        ]
    )
    adapter = _ScriptedAdapter(cyclic, cyclic)  # keeps returning the cyclic plan
    with pytest.raises(EpicPlanError):
        await decompose("build a thing", KnowledgeEngine(), adapter, max_parse_retries=1)


async def test_decompose_gives_up_after_retries():
    adapter = _ScriptedAdapter("garbage", "still garbage")
    with pytest.raises(EpicPlanError):
        await decompose("x", KnowledgeEngine(), adapter, max_parse_retries=1)
