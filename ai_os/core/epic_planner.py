"""LLM-driven Epic -> TaskNode DAG decomposition (doc 02 §1, Phase 4a).

This is the "heuristic" half of the DAG Planner that `ai_os/core/planner.py`
deliberately left out: turning one high-level request ("add JWT auth") into a
list of concrete `TaskNode`s with dependencies, risk levels, and write sets.
The deterministic half — building/validating/ordering the graph — is reused
from `planner.py` here, so a plan the LLM produces is rejected before any
execution if it has cycles or dangling dependencies.

Robustness is the real work here: LLMs wrap JSON in markdown fences, add prose
preambles, and occasionally emit malformed JSON. `parse_task_plan` is
defensive (strips fences, extracts the outermost array, `json.loads`,
pydantic-validates each entry), and `decompose` retries once with the parse
error fed back to the model (mirroring the sandbox feedback loop in
`task_runner.py`) before giving up.

No real LLM call is made anywhere in this module's tests — `decompose` takes an
injected adapter, and the tests feed it canned plan JSON (including a malformed
first response, to exercise the retry).
"""
from __future__ import annotations

import json
import re
from typing import Any

from ai_os.core import planner
from ai_os.core.models import TaskNode
from ai_os.knowledge.graph_engine import KnowledgeEngine
from ai_os.mcp.adapters.base_adapter import BaseMCPAdapter, LLMTaskRequest

PLANNING_SYSTEM_PROMPT = (
    "You are the DAG Planner of an AI software-engineering orchestrator. You "
    "decompose a high-level request into a minimal set of concrete, atomic "
    "engineering tasks and their dependencies. Respond with ONLY a JSON array "
    "(no prose, no markdown fences) where each element is an object with keys:\n"
    '  "id": string, unique, e.g. "TASK-1"\n'
    '  "title": string, short\n'
    '  "description": string, a precise instruction the coding agent can act on '
    "alone\n"
    '  "risk_level": one of "LOW", "MEDIUM", "HIGH", "CRITICAL" (LOW = trivial/'
    "docs/style, CRITICAL = architecture/security/complex logic)\n"
    '  "target_files": array of POSIX repo-relative paths this task creates or '
    "edits (no leading './')\n"
    '  "write_set": array of the same paths this task will WRITE (usually equals '
    "target_files)\n"
    '  "dependencies": array of task ids that must finish before this one\n'
    "Keep tasks independent where possible so they can run in parallel. Two tasks "
    "that edit the same file MUST have a dependency between them. Do not invent a "
    "task that depends on a task id you did not define."
)

_TASK_FIELDS = {"id", "title", "description", "risk_level", "target_files", "write_set", "dependencies"}


class EpicPlanError(RuntimeError):
    """The LLM's plan could not be parsed or was not a valid DAG. Carries the
    raw model text (`.raw`) for debugging / feedback."""

    def __init__(self, message: str, raw: str = "") -> None:
        self.raw = raw
        super().__init__(message)


def build_repo_summary(engine: KnowledgeEngine, max_symbols: int = 200) -> str:
    """A compact, grounding summary of the repo (files + top symbols) so the
    planner proposes real paths, not imagined ones."""
    files: list[str] = []
    symbols: list[str] = []
    for node_id, data in engine.graph.nodes(data=True):
        node_type = data.get("node_type")
        if node_type == "FileNode":
            files.append(f"{node_id} ({data.get('language', '?')})")
        elif node_type in ("ClassNode", "FunctionNode", "TypeNode"):
            symbols.append(node_id)
    lines = ["## Existing files", *sorted(files)]
    if symbols:
        shown = sorted(symbols)[:max_symbols]
        lines += ["", "## Existing symbols (FQN = <relpath>::<QualifiedName>)", *shown]
        if len(symbols) > max_symbols:
            lines.append(f"... and {len(symbols) - max_symbols} more")
    return "\n".join(lines)


def build_planning_prompt(user_prompt: str, repo_summary: str, parse_feedback: str | None = None) -> str:
    parts = [
        "# High-level request",
        user_prompt,
        "",
        "# Repository structure (ground your task paths in these real files)",
        repo_summary,
    ]
    if parse_feedback:
        parts += [
            "",
            "# Your previous response could not be used",
            parse_feedback,
            "Respond again with ONLY the JSON array, no prose, no markdown fences.",
        ]
    return "\n".join(parts)


def _strip_code_fences(text: str) -> str:
    fence = re.match(r"\s*```(?:json)?\s*\n(.*)\n```\s*$", text, re.DOTALL)
    return fence.group(1) if fence else text


def _extract_json_array(text: str) -> str:
    """Best-effort extraction of the outermost JSON array from a model response
    that may have prose around it."""
    start = text.find("[")
    end = text.rfind("]")
    if start == -1 or end == -1 or end < start:
        raise EpicPlanError("no JSON array found in the model response", raw=text)
    return text[start : end + 1]


def parse_task_plan(text: str) -> list[TaskNode]:
    """Parses a model response into validated `TaskNode`s. Raises `EpicPlanError`
    (carrying the raw text) on malformed JSON or an entry that fails
    `TaskNode` validation."""
    candidate = _extract_json_array(_strip_code_fences(text.strip()))
    try:
        raw: Any = json.loads(candidate)
    except json.JSONDecodeError as exc:
        raise EpicPlanError(f"model response was not valid JSON: {exc}", raw=text) from exc
    if not isinstance(raw, list) or not raw:
        raise EpicPlanError("model response was not a non-empty JSON array of tasks", raw=text)

    tasks: list[TaskNode] = []
    for i, entry in enumerate(raw):
        if not isinstance(entry, dict):
            raise EpicPlanError(f"task {i} is not a JSON object", raw=text)
        unknown = set(entry) - _TASK_FIELDS
        if unknown:
            # Tolerate extra keys (some models add "rationale" etc.) rather than
            # hard-failing — just drop them before constructing the TaskNode.
            entry = {k: v for k, v in entry.items() if k in _TASK_FIELDS}
        try:
            tasks.append(TaskNode(**entry))
        except Exception as exc:  # pydantic ValidationError, missing keys, etc.
            raise EpicPlanError(f"task {i} failed validation: {exc}", raw=text) from exc
    return tasks


def validate_plan(tasks: list[TaskNode]) -> None:
    """Runs the plan through the deterministic planner to reject cycles and
    dangling dependency references before anything executes."""
    graph = planner.build_graph(tasks)  # raises UnknownDependencyError on dangling deps
    planner.validate_acyclic(graph)  # raises CyclicDependencyError on a cycle


async def decompose(
    user_prompt: str,
    engine: KnowledgeEngine,
    adapter: BaseMCPAdapter,
    model: str | None = None,
    max_parse_retries: int = 1,
) -> list[TaskNode]:
    """Decompose `user_prompt` into a validated `list[TaskNode]` using `adapter`.

    On a parse/validation failure, retries up to `max_parse_retries` more times
    with the error fed back to the model. Raises `EpicPlanError` if it still
    can't get a usable plan.
    """
    repo_summary = build_repo_summary(engine)
    feedback: str | None = None
    last_error: EpicPlanError | None = None

    for _attempt in range(max_parse_retries + 1):
        request = LLMTaskRequest(
            task_id="epic-plan",
            system_prompt=PLANNING_SYSTEM_PROMPT,
            context_payload=build_planning_prompt(user_prompt, repo_summary, feedback),
            model=model,
        )
        response = await adapter.execute_task(request)
        try:
            tasks = parse_task_plan(response.generated_text)
            validate_plan(tasks)
            return tasks
        except EpicPlanError as exc:
            last_error = exc
            feedback = str(exc)
        except Exception as exc:  # planner errors (cycle / dangling dep)
            last_error = EpicPlanError(str(exc), raw=response.generated_text)
            feedback = str(exc)

    assert last_error is not None
    raise last_error
