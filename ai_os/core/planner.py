"""Deterministic DAG Planner (doc 02 §1): dependency-graph construction, cycle
validation, and topological batching over an already-decomposed list of TaskNodes.

Explicitly out of scope for this module:
- LLM-based decomposition of an Epic into TaskNodes (Phase 3+, no LLM router exists
  yet). Callers are expected to hand in a `list[TaskNode]` that already exists.
- read_set/write_set conflict analysis. Two tasks with no dependency edge between
  them can legitimately land in the same batch even if their write_sets overlap;
  serializing that at runtime is `ai_os.core.lock_manager.LockManager`'s job, not
  this module's. The planner only reasons about *causal* (dependency) ordering.
"""
from __future__ import annotations

import networkx as nx

from ai_os.core.models import TaskNode


class UnknownDependencyError(ValueError):
    """A TaskNode.dependencies entry references a task id absent from the input list."""


class CyclicDependencyError(ValueError):
    """The task dependency graph is not a DAG.

    Carries the actual cycle as `.cycle` (the raw edge list from `nx.find_cycle`),
    so callers get a reportable, debuggable error instead of a bare "there's a
    cycle somewhere" boolean.
    """

    def __init__(self, cycle: list[tuple[str, str]]) -> None:
        self.cycle = cycle
        super().__init__(f"Cyclic task dependency detected: {cycle!r}")


def build_graph(tasks: list[TaskNode]) -> nx.DiGraph:
    """Builds the task dependency graph: one node per task.id (in input order),
    then one edge per dependency, oriented dep_id -> task.id (prerequisite ->
    dependent) so that `topological_generations` yields prerequisites first.

    Raises UnknownDependencyError immediately if a dependency references an id
    not present in `tasks` — dangling references never silently create a
    phantom node.
    """
    graph = nx.DiGraph()
    for task in tasks:
        graph.add_node(task.id)

    for task in tasks:
        for dep_id in task.dependencies:
            if dep_id not in graph:
                raise UnknownDependencyError(
                    f"TaskNode {task.id!r} declares dependency on unknown task id {dep_id!r}"
                )
            graph.add_edge(dep_id, task.id)
    return graph


def validate_acyclic(graph: nx.DiGraph) -> None:
    """Raises CyclicDependencyError (carrying the actual cycle from
    nx.find_cycle) if `graph` is not a DAG. Checks with
    nx.is_directed_acyclic_graph first since nx.find_cycle itself raises
    NetworkXNoCycle when called on an acyclic graph.
    """
    if not nx.is_directed_acyclic_graph(graph):
        cycle = nx.find_cycle(graph)
        raise CyclicDependencyError(cycle)


def topological_batches(graph: nx.DiGraph) -> list[list[str]]:
    """Returns the execution order as a list of "generations": each generation
    is a list of task ids whose prerequisites are all satisfied by earlier
    generations, so tasks within one generation may run in parallel (subject to
    the Lock Manager's runtime read/write serialization — see module docstring).

    Validates acyclicity first, raising the project-specific CyclicDependencyError
    rather than letting networkx's NetworkXUnfeasible leak out.

    Empirically (networkx 3.6.1): nx.topological_generations(graph) returns a
    generator of lists, but the order *within* a generation follows internal
    adjacency-dict iteration (effectively: the order dependency edges were added),
    not necessarily the order nodes/tasks first appeared in the input list — e.g.
    adding edges A->C before A->B can yield generation ['C', 'B'] even though B
    was inserted as a node before C. To guarantee reproducible, testable output
    regardless of edge insertion order, each generation is explicitly re-sorted
    here by each node's position in `graph`'s own node insertion order (which
    build_graph populates in input-task order).
    """
    validate_acyclic(graph)
    node_order = {node: index for index, node in enumerate(graph.nodes)}
    return [
        sorted(generation, key=lambda node: node_order[node])
        for generation in nx.topological_generations(graph)
    ]
