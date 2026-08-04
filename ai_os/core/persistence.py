"""Thin async repository over `ai_os.core.db` for Epic/Task lifecycle rows and
the two audit tables (`LockAuditModel`, `TokenCostModel`) whose schema has
existed since Phase 2 but which nothing populated until Phase 5 Stage 3.

Design constraints that shape this module:

- **Best-effort at the call site, not here.** The write methods do the real
  DB work and let SQLAlchemy errors propagate; the runner wiring
  (`EpicRunner`/`TaskRunner`) wraps them so a persistence hiccup logs a warning
  but never aborts a real code-generation run. That keeps these methods simple
  and directly testable (a test asserts rows really land), while the "optional,
  degrade gracefully" behavior lives in one place in the runners.
- **FK order is real.** `token_costs.task_id` -> `tasks.id` -> `epics.id` are
  enforced (`PRAGMA foreign_keys=ON`, see `db/database.py`), so a caller must
  `create_epic` then `upsert_task` before any `record_token_cost` /
  `record_lock_audit`. `EpicRunner` does exactly that at the start of a run.
- **Reads return plain dataclasses, not ORM instances**, so callers (the CLI)
  never touch a detached-instance lazy-load across the session boundary.
"""
from __future__ import annotations

import datetime
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from ai_os.core.db.database import init_db, make_engine, make_sessionmaker
from ai_os.core.db.models import (
    EpicModel,
    LockAuditModel,
    TaskModel,
    TokenCostModel,
)
from ai_os.core.models import TaskNode
from ai_os.mcp.adapters.base_adapter import TokenUsage


@dataclass
class EpicSummary:
    id: str
    title: str
    status: str
    created_at: datetime.datetime | None
    total_tasks: int
    completed_tasks: int
    input_tokens: int
    output_tokens: int
    total_usd: float


@dataclass
class ProviderSpend:
    provider: str
    model_name: str
    calls: int
    input_tokens: int
    output_tokens: int
    total_usd: float


@dataclass
class EpicRow:
    id: str
    title: str
    raw_user_prompt: str
    status: str


class Persistence:
    """Async repository. One instance wraps a session factory bound to one
    engine; construct it via `open(db_url)` (which creates the schema) or
    directly from an existing `async_sessionmaker` (tests do this against a
    shared in-memory engine)."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sf = session_factory

    @classmethod
    async def open(cls, db_url: str) -> tuple["Persistence", AsyncEngine]:
        """Build an engine for `db_url`, create the schema, and return a ready
        `Persistence` plus the engine (so the caller can `await engine.dispose()`
        when done). Used by the CLI."""
        engine = make_engine(db_url)
        await init_db(engine)
        return cls(make_sessionmaker(engine)), engine

    # -- writes --------------------------------------------------------------

    async def create_epic(
        self, epic_id: str, title: str, raw_user_prompt: str, status: str = "RUNNING"
    ) -> None:
        async with self._sf() as session:
            async with session.begin():
                session.add(
                    EpicModel(
                        id=epic_id, title=title, raw_user_prompt=raw_user_prompt, status=status
                    )
                )

    async def set_epic_status(self, epic_id: str, status: str) -> None:
        async with self._sf() as session:
            async with session.begin():
                epic = await session.get(EpicModel, epic_id)
                if epic is not None:
                    epic.status = status

    async def upsert_task(
        self, task: TaskNode, epic_id: str, assigned_model: str | None = None, status: str = "PENDING"
    ) -> None:
        async with self._sf() as session:
            async with session.begin():
                existing = await session.get(TaskModel, task.id)
                if existing is None:
                    session.add(
                        TaskModel(
                            id=task.id,
                            epic_id=epic_id,
                            title=task.title,
                            description=task.description,
                            risk_level=task.risk_level,
                            assigned_model=assigned_model,
                            status=status,
                            max_retries=task.max_retries,
                            target_files=list(task.target_files),
                            read_set=sorted(task.read_set),
                            write_set=sorted(task.write_set),
                            dependencies=list(task.dependencies),
                        )
                    )
                else:
                    if assigned_model is not None:
                        existing.assigned_model = assigned_model
                    existing.status = status

    async def update_task_status(self, task_id: str, status: str) -> None:
        async with self._sf() as session:
            async with session.begin():
                task = await session.get(TaskModel, task_id)
                if task is not None:
                    task.status = status

    async def record_lock_audit(
        self, task_id: str, filepath: str, lock_type: str, action: str
    ) -> None:
        async with self._sf() as session:
            async with session.begin():
                session.add(
                    LockAuditModel(
                        task_id=task_id, filepath=filepath, lock_type=lock_type, action=action
                    )
                )

    async def record_token_cost(
        self, task_id: str, provider: str, model_name: str, usage: TokenUsage
    ) -> None:
        async with self._sf() as session:
            async with session.begin():
                session.add(
                    TokenCostModel(
                        task_id=task_id,
                        provider=provider,
                        model_name=model_name,
                        input_tokens=usage.input_tokens,
                        output_tokens=usage.output_tokens,
                        usd_cost=usage.estimated_usd_cost,
                    )
                )

    # -- reads ---------------------------------------------------------------

    async def epic_summaries(self) -> list[EpicSummary]:
        """Every epic, newest first, with its task counts and rolled-up spend
        (joined across tasks -> token_costs)."""
        async with self._sf() as session:
            epics = (
                await session.execute(select(EpicModel).order_by(EpicModel.created_at.desc()))
            ).scalars().all()

            summaries: list[EpicSummary] = []
            for epic in epics:
                total_tasks = (
                    await session.execute(
                        select(func.count(TaskModel.id)).where(TaskModel.epic_id == epic.id)
                    )
                ).scalar_one()
                completed_tasks = (
                    await session.execute(
                        select(func.count(TaskModel.id)).where(
                            TaskModel.epic_id == epic.id, TaskModel.status == "COMPLETED"
                        )
                    )
                ).scalar_one()
                spend = (
                    await session.execute(
                        select(
                            func.coalesce(func.sum(TokenCostModel.input_tokens), 0),
                            func.coalesce(func.sum(TokenCostModel.output_tokens), 0),
                            func.coalesce(func.sum(TokenCostModel.usd_cost), 0.0),
                        )
                        .select_from(TokenCostModel)
                        .join(TaskModel, TokenCostModel.task_id == TaskModel.id)
                        .where(TaskModel.epic_id == epic.id)
                    )
                ).one()
                summaries.append(
                    EpicSummary(
                        id=epic.id,
                        title=epic.title,
                        status=epic.status,
                        created_at=epic.created_at,
                        total_tasks=total_tasks,
                        completed_tasks=completed_tasks,
                        input_tokens=spend[0],
                        output_tokens=spend[1],
                        total_usd=spend[2],
                    )
                )
            return summaries

    async def provider_breakdown(self, epic_id: str | None = None) -> list[ProviderSpend]:
        """Spend grouped by (provider, model), optionally scoped to one epic."""
        async with self._sf() as session:
            query = (
                select(
                    TokenCostModel.provider,
                    TokenCostModel.model_name,
                    func.count(TokenCostModel.id),
                    func.coalesce(func.sum(TokenCostModel.input_tokens), 0),
                    func.coalesce(func.sum(TokenCostModel.output_tokens), 0),
                    func.coalesce(func.sum(TokenCostModel.usd_cost), 0.0),
                )
                .group_by(TokenCostModel.provider, TokenCostModel.model_name)
                .order_by(func.sum(TokenCostModel.usd_cost).desc())
            )
            if epic_id is not None:
                query = query.join(TaskModel, TokenCostModel.task_id == TaskModel.id).where(
                    TaskModel.epic_id == epic_id
                )
            rows = (await session.execute(query)).all()
            return [
                ProviderSpend(
                    provider=r[0], model_name=r[1], calls=r[2],
                    input_tokens=r[3], output_tokens=r[4], total_usd=r[5],
                )
                for r in rows
            ]

    async def get_epic(self, epic_id: str) -> EpicRow | None:
        """The epic row, or `None` if no such epic — used by `epic resume` to
        validate the id before doing anything."""
        async with self._sf() as session:
            epic = await session.get(EpicModel, epic_id)
            if epic is None:
                return None
            return EpicRow(
                id=epic.id, title=epic.title,
                raw_user_prompt=epic.raw_user_prompt, status=epic.status,
            )

    async def load_epic_tasks(self, epic_id: str) -> list[tuple[TaskNode, str]]:
        """Reconstruct every task of an epic as a `(TaskNode, db_status)` pair,
        so `EpicRunner.resume_epic` can rebuild the DAG and re-run only the tasks
        that aren't already COMPLETED. The stored rows were validated at creation
        time, so reconstructing the `TaskNode` (which re-runs its validators)
        cannot fail on well-formed data."""
        async with self._sf() as session:
            rows = (
                await session.execute(
                    select(TaskModel).where(TaskModel.epic_id == epic_id).order_by(TaskModel.id)
                )
            ).scalars().all()
            out: list[tuple[TaskNode, str]] = []
            for r in rows:
                node = TaskNode(
                    id=r.id,
                    title=r.title,
                    description=r.description,
                    risk_level=r.risk_level,
                    target_files=list(r.target_files or []),
                    read_set=set(r.read_set or []),
                    write_set=set(r.write_set or []),
                    dependencies=list(r.dependencies or []),
                    max_retries=r.max_retries,
                )
                out.append((node, r.status))
            return out

    async def epic_total_usd(self, epic_id: str) -> float:
        """Total USD spent on one epic so far — the cost-cap check reads this
        (Stage 4)."""
        async with self._sf() as session:
            total = (
                await session.execute(
                    select(func.coalesce(func.sum(TokenCostModel.usd_cost), 0.0))
                    .select_from(TokenCostModel)
                    .join(TaskModel, TokenCostModel.task_id == TaskModel.id)
                    .where(TaskModel.epic_id == epic_id)
                )
            ).scalar_one()
            return float(total)


def default_db_path() -> Path:
    """The on-disk SQLite file AI-OS accounts to, under the same home dir the
    project registry uses (`AI_OS_HOME` or `~/.ai-os`)."""
    import os

    override = os.environ.get("AI_OS_HOME")
    home = Path(override) if override else Path.home() / ".ai-os"
    return home / "ai-os.db"


def default_db_url() -> str:
    path = default_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    return f"sqlite+aiosqlite:///{path}"
