"""In-memory planning contract shared by the DAG Planner and Lock Manager (doc 02).

Deliberate deviation from docs/14_PROJECT_DIRECTORY_STRUCTURE.md, which only lists a
single `core/db/models.py` for SQLAlchemy models: `TaskNode`/`EpicNode` here are the
pydantic, in-memory planning representation, kept separate from the SQLAlchemy
persistence models in `ai_os/core/db/models.py`. Conflating the two would force the
planner/lock-manager to either depend on the DB layer or awkwardly reuse ORM instances
as plain data objects.

Path convention (binds this module to `lock_manager.py` and `staging.py`): every path in
`target_files`/`read_set`/`write_set` is POSIX-style, relative to the repo root, with no
leading "./".
"""
from __future__ import annotations

from typing import List, Literal, Set

from pydantic import BaseModel, Field, model_validator

RiskLevel = Literal["LOW", "MEDIUM", "HIGH", "CRITICAL"]
TaskStatus = Literal["PENDING", "READY", "RUNNING", "COMPLETED", "FAILED", "BLOCKED"]
EpicStatus = Literal["PLAN_REVIEW", "RUNNING", "COMPLETED", "FAILED", "PAUSED"]


class TaskNode(BaseModel):
    id: str
    title: str
    description: str
    risk_level: RiskLevel
    target_files: List[str] = Field(default_factory=list)
    read_set: Set[str] = Field(default_factory=set)
    write_set: Set[str] = Field(default_factory=set)
    dependencies: List[str] = Field(default_factory=list)
    status: TaskStatus = "PENDING"
    max_retries: int = 3
    retry_count: int = 0
    # Per-task language (python/typescript/javascript/java), letting one epic
    # span e.g. a Python backend + a TypeScript frontend — each task validates
    # with its own sandbox profile. `None` -> derived from the task's file
    # extensions, falling back to the epic's default language.
    language: str | None = None

    @model_validator(mode="after")
    def _read_write_disjoint(self) -> "TaskNode":
        overlap = self.read_set & self.write_set
        if overlap:
            raise ValueError(
                f"TaskNode {self.id!r}: read_set and write_set overlap: {sorted(overlap)}"
            )
        return self

    @model_validator(mode="after")
    def _no_self_dependency(self) -> "TaskNode":
        if self.id in self.dependencies:
            raise ValueError(f"TaskNode {self.id!r} cannot depend on itself")
        return self


class EpicNode(BaseModel):
    id: str
    title: str
    raw_user_prompt: str
    status: EpicStatus = "PLAN_REVIEW"
