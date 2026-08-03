"""Tests for ai_os.core.planner: pure networkx + TaskNode, no I/O, no asyncio."""
import pytest

from ai_os.core.models import TaskNode
from ai_os.core.planner import (
    CyclicDependencyError,
    UnknownDependencyError,
    build_graph,
    topological_batches,
    validate_acyclic,
)


def _task(id: str, dependencies: list[str] | None = None, write_set: set[str] | None = None) -> TaskNode:
    return TaskNode(
        id=id,
        title=f"Task {id}",
        description="",
        risk_level="LOW",
        target_files=[],
        write_set=write_set or set(),
        dependencies=dependencies or [],
    )


def test_linear_chain_produces_three_generations_in_causal_order():
    tasks = [_task("A"), _task("B", dependencies=["A"]), _task("C", dependencies=["B"])]
    batches = topological_batches(build_graph(tasks))
    assert batches == [["A"], ["B"], ["C"]]


def test_independent_tasks_land_in_a_single_generation():
    tasks = [_task("A"), _task("B"), _task("C")]
    batches = topological_batches(build_graph(tasks))
    assert batches == [["A", "B", "C"]]


def test_diamond_dependency_produces_three_generations():
    tasks = [
        _task("A"),
        _task("B", dependencies=["A"]),
        _task("C", dependencies=["A"]),
        _task("D", dependencies=["B", "C"]),
    ]
    batches = topological_batches(build_graph(tasks))
    assert len(batches) == 3
    assert batches[0] == ["A"]
    assert set(batches[1]) == {"B", "C"}
    assert batches[2] == ["D"]


def test_cycle_detection_raises_with_usable_cycle_info():
    tasks = [_task("A", dependencies=["B"]), _task("B", dependencies=["A"])]
    graph = build_graph(tasks)

    with pytest.raises(CyclicDependencyError) as excinfo:
        validate_acyclic(graph)
    assert excinfo.value.cycle  # not just a message string: actual edges are exposed
    involved_nodes = {node for edge in excinfo.value.cycle for node in edge[:2]}
    assert involved_nodes == {"A", "B"}

    with pytest.raises(CyclicDependencyError):
        topological_batches(graph)


def test_dangling_dependency_raises_unknown_dependency_error():
    tasks = [_task("A", dependencies=["GHOST"])]
    with pytest.raises(UnknownDependencyError):
        build_graph(tasks)


def test_topological_batches_is_deterministic_across_independent_identical_inputs():
    def make_tasks():
        return [
            _task("A"),
            _task("B", dependencies=["A"]),
            _task("C", dependencies=["A"]),
            _task("D", dependencies=["B", "C"]),
            _task("E"),
        ]

    batches_1 = topological_batches(build_graph(make_tasks()))
    batches_2 = topological_batches(build_graph(make_tasks()))
    assert batches_1 == batches_2


def test_planner_is_not_conflict_aware_overlapping_write_sets_share_a_generation():
    """The planner's job is purely causal ordering. Two tasks with overlapping
    write_sets but no dependency edge between them are intentionally allowed to
    land in the same generation — serializing that at runtime is the Lock
    Manager's responsibility, not the planner's."""
    tasks = [
        _task("A", write_set={"shared.py"}),
        _task("B", write_set={"shared.py"}),
    ]
    batches = topological_batches(build_graph(tasks))
    assert batches == [["A", "B"]]
