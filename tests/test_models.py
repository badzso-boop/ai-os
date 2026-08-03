"""Tests for `ai_os.core.models` (the pydantic in-memory planning contract)."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from ai_os.core.models import EpicNode, TaskNode


def _task(**overrides) -> dict:
    base = dict(id="TASK-1", title="t", description="d", risk_level="LOW")
    base.update(overrides)
    return base


def test_construction_with_defaults():
    t = TaskNode(**_task())
    assert t.target_files == []
    assert t.read_set == set()
    assert t.write_set == set()
    assert t.dependencies == []
    assert t.status == "PENDING"
    assert t.max_retries == 3
    assert t.retry_count == 0


def test_read_write_overlap_is_rejected():
    with pytest.raises(ValidationError):
        TaskNode(**_task(read_set={"a.py"}, write_set={"a.py"}))


def test_self_dependency_is_rejected():
    with pytest.raises(ValidationError):
        TaskNode(**_task(id="TASK-1", dependencies=["TASK-1"]))


def test_disjoint_read_write_is_fine():
    t = TaskNode(**_task(read_set={"a.py"}, write_set={"b.py"}))
    assert t.read_set == {"a.py"}
    assert t.write_set == {"b.py"}


def test_default_factories_are_not_shared_between_instances():
    t1 = TaskNode(**_task(id="TASK-1"))
    t2 = TaskNode(**_task(id="TASK-2"))
    t1.write_set.add("shared-mutation.py")
    t1.dependencies.append("nope")
    assert t2.write_set == set()
    assert t2.dependencies == []


def test_epic_node_defaults():
    e = EpicNode(id="EPIC-1", title="t", raw_user_prompt="build me a thing")
    assert e.status == "PLAN_REVIEW"
