"""Real-SQLite tests for ai_os/core/db (doc 13).

Follows this project's "real behavior over mocks" convention: these tests hit
a real aiosqlite-backed SQLite file (`tmp_path / "test.db"`) for the bulk of
the coverage, plus one in-memory-engine test to exercise the StaticPool code
path. No mocked sessions/engines/connections anywhere.

`pyproject.toml` sets `asyncio_mode = "auto"`, so plain `async def test_...`
functions run under pytest-asyncio without any `@pytest.mark.asyncio` marker.
"""
from __future__ import annotations

import datetime

import pytest
from sqlalchemy import inspect, select, text
from sqlalchemy.exc import IntegrityError

from ai_os.core.db.database import init_db, make_engine, make_sessionmaker
from ai_os.core.db.models import (
    Base,
    EpicModel,
    GraphEdgeModel,
    GraphNodeModel,
    LockAuditModel,
    TaskModel,
    TokenCostModel,
)


def _file_url(tmp_path) -> str:
    return f"sqlite+aiosqlite:///{tmp_path / 'test.db'}"


async def test_init_db_creates_all_tables(tmp_path):
    engine = make_engine(_file_url(tmp_path))
    await init_db(engine)

    def _table_names(sync_conn):
        return set(inspect(sync_conn).get_table_names())

    async with engine.connect() as conn:
        table_names = await conn.run_sync(_table_names)

    expected = {t.name for t in Base.metadata.sorted_tables}
    assert expected == {
        "epics",
        "tasks",
        "lock_audits",
        "token_costs",
        "graph_nodes",
        "graph_edges",
    }
    assert expected <= table_names

    await engine.dispose()


async def test_journal_mode_is_wal_on_file_backed_engine(tmp_path):
    engine = make_engine(_file_url(tmp_path))
    await init_db(engine)

    async with engine.connect() as conn:
        result = await conn.execute(text("PRAGMA journal_mode"))
        mode = result.scalar_one()

    assert mode.lower() == "wal"

    await engine.dispose()


async def test_foreign_key_violation_raises(tmp_path):
    engine = make_engine(_file_url(tmp_path))
    await init_db(engine)
    session_factory = make_sessionmaker(engine)

    async with session_factory() as session:
        session.add(
            TaskModel(
                id="TASK-orphan",
                epic_id="EPIC-does-not-exist",
                title="Orphan task",
                description="References a non-existent epic",
            )
        )
        with pytest.raises(IntegrityError):
            await session.commit()

    await engine.dispose()


async def test_crud_epic_and_task_relationship(tmp_path):
    engine = make_engine(_file_url(tmp_path))
    await init_db(engine)
    session_factory = make_sessionmaker(engine)

    async with session_factory() as session:
        epic = EpicModel(
            id="EPIC-1",
            title="Build the thing",
            raw_user_prompt="Please build the thing",
        )
        task = TaskModel(
            id="TASK-1",
            epic_id="EPIC-1",
            title="Do the first step",
            description="Step one of the thing",
        )
        epic.tasks.append(task)
        session.add(epic)
        await session.commit()

    async with session_factory() as session:
        reloaded = await session.get(EpicModel, "EPIC-1")
        assert reloaded is not None
        assert reloaded.status == "PLAN_REVIEW"
        assert isinstance(reloaded.created_at, datetime.datetime)

        await session.refresh(reloaded, attribute_names=["tasks"])
        assert len(reloaded.tasks) == 1
        assert reloaded.tasks[0].id == "TASK-1"
        assert reloaded.tasks[0].epic_id == "EPIC-1"
        assert reloaded.tasks[0].status == "PENDING"
        assert reloaded.tasks[0].risk_level == "LOW"
        assert reloaded.tasks[0].max_retries == 3

    await engine.dispose()


async def test_cascade_delete_epic_removes_tasks(tmp_path):
    engine = make_engine(_file_url(tmp_path))
    await init_db(engine)
    session_factory = make_sessionmaker(engine)

    async with session_factory() as session:
        epic = EpicModel(
            id="EPIC-2",
            title="Disposable epic",
            raw_user_prompt="Ephemeral",
        )
        epic.tasks.append(
            TaskModel(
                id="TASK-2a",
                epic_id="EPIC-2",
                title="Task A",
                description="Will be cascade-deleted",
            )
        )
        epic.tasks.append(
            TaskModel(
                id="TASK-2b",
                epic_id="EPIC-2",
                title="Task B",
                description="Will also be cascade-deleted",
            )
        )
        session.add(epic)
        await session.commit()

    async with session_factory() as session:
        epic = await session.get(EpicModel, "EPIC-2")
        assert epic is not None
        await session.delete(epic)
        await session.commit()

    async with session_factory() as session:
        assert await session.get(EpicModel, "EPIC-2") is None
        remaining = (
            await session.execute(
                select(TaskModel).where(TaskModel.epic_id == "EPIC-2")
            )
        ).scalars().all()
        assert remaining == []

    await engine.dispose()


async def test_json_columns_round_trip(tmp_path):
    engine = make_engine(_file_url(tmp_path))
    await init_db(engine)
    session_factory = make_sessionmaker(engine)

    async with session_factory() as session:
        epic = EpicModel(
            id="EPIC-3",
            title="JSON round trip epic",
            raw_user_prompt="Check the JSON columns",
        )
        epic.tasks.append(
            TaskModel(
                id="TASK-3",
                epic_id="EPIC-3",
                title="JSON task",
                description="Has non-trivial JSON columns",
                target_files=["a.py", "b.py"],
                read_set=["c.py"],
                write_set=["a.py", "b.py"],
                dependencies=["TASK-0"],
            )
        )
        session.add(epic)
        await session.commit()

    async with session_factory() as session:
        reloaded_task = await session.get(TaskModel, ("TASK-3", "EPIC-3"))
        assert reloaded_task is not None
        assert reloaded_task.target_files == ["a.py", "b.py"]
        assert reloaded_task.read_set == ["c.py"]
        assert reloaded_task.write_set == ["a.py", "b.py"]
        assert reloaded_task.dependencies == ["TASK-0"]

    await engine.dispose()


async def test_json_column_default_is_not_a_shared_mutable_list(tmp_path):
    """Guards against `default=list` regressing to a shared mutable literal
    (e.g. `default=[]`, which SQLAlchemy/Python would evaluate exactly once
    and hand every row the *same* list object).

    `default=list` (a client-side column default) is only evaluated at flush
    time, so we must add+commit two distinct TaskModel instances without an
    explicit list value, then assert their python list attributes are distinct
    objects.
    """
    engine = make_engine(_file_url(tmp_path))
    await init_db(engine)
    session_factory = make_sessionmaker(engine)

    async with session_factory() as session:
        epic = EpicModel(id="EPIC-4", title="Defaults epic", raw_user_prompt="x")
        task_a = TaskModel(
            id="TASK-4a", epic_id="EPIC-4", title="A", description="A"
        )
        task_b = TaskModel(
            id="TASK-4b", epic_id="EPIC-4", title="B", description="B"
        )
        epic.tasks.extend([task_a, task_b])
        session.add(epic)
        await session.commit()

        # Both rows were inserted with defaults populated:
        assert task_a.target_files == []
        assert task_b.target_files == []
        # Crucially: they are distinct list instances, not one mutated in place.
        assert task_a.target_files is not task_b.target_files

    await engine.dispose()


async def test_lock_audit_and_token_cost_persist(tmp_path):
    engine = make_engine(_file_url(tmp_path))
    await init_db(engine)
    session_factory = make_sessionmaker(engine)

    async with session_factory() as session:
        epic = EpicModel(id="EPIC-5", title="Audit epic", raw_user_prompt="x")
        task = TaskModel(
            id="TASK-5", epic_id="EPIC-5", title="T", description="T"
        )
        epic.tasks.append(task)
        session.add(epic)
        await session.commit()

        session.add(
            LockAuditModel(
                task_id="TASK-5",
                epic_id="EPIC-5",
                filepath="src/a.py",
                lock_type="WRITE",
                action="ACQUIRE",
            )
        )
        session.add(
            TokenCostModel(
                task_id="TASK-5",
                epic_id="EPIC-5",
                provider="anthropic",
                model_name="claude-3-5-sonnet",
                input_tokens=100,
                output_tokens=50,
                usd_cost=0.0123,
            )
        )
        await session.commit()

    async with session_factory() as session:
        task = await session.get(TaskModel, ("TASK-5", "EPIC-5"))
        await session.refresh(task, attribute_names=["lock_audits", "token_costs"])
        assert len(task.lock_audits) == 1
        assert task.lock_audits[0].lock_type == "WRITE"
        assert task.lock_audits[0].action == "ACQUIRE"
        assert len(task.token_costs) == 1
        assert task.token_costs[0].usd_cost == pytest.approx(0.0123)

    await engine.dispose()


async def test_graph_node_and_edge_persist(tmp_path):
    engine = make_engine(_file_url(tmp_path))
    await init_db(engine)
    session_factory = make_sessionmaker(engine)

    async with session_factory() as session:
        session.add(
            GraphNodeModel(
                fqn="src/user.py::User",
                node_type="ClassNode",
                filepath="src/user.py",
                stub_code="class User: ...",
            )
        )
        session.add(
            GraphNodeModel(
                fqn="src/user.py::get_user",
                node_type="FunctionNode",
                filepath="src/user.py",
            )
        )
        session.add(
            GraphEdgeModel(
                source_fqn="src/user.py::get_user",
                target_fqn="src/user.py::User",
                relation_type="USES_TYPE",
            )
        )
        await session.commit()

    async with session_factory() as session:
        node = await session.get(GraphNodeModel, "src/user.py::User")
        assert node is not None
        assert node.stub_code == "class User: ..."

        edges = (await session.execute(select(GraphEdgeModel))).scalars().all()
        assert len(edges) == 1
        assert edges[0].source_fqn == "src/user.py::get_user"
        assert edges[0].target_fqn == "src/user.py::User"
        assert edges[0].relation_type == "USES_TYPE"

    await engine.dispose()


async def test_in_memory_engine_crud():
    """In-memory engine uses a different pooling code path (StaticPool);
    do NOT assert WAL mode here — SQLite silently stays in `memory` journal
    mode for `:memory:` databases regardless of the PRAGMA."""
    engine = make_engine("sqlite+aiosqlite:///:memory:")
    await init_db(engine)
    session_factory = make_sessionmaker(engine)

    async with session_factory() as session:
        epic = EpicModel(
            id="EPIC-MEM", title="In-memory epic", raw_user_prompt="x"
        )
        epic.tasks.append(
            TaskModel(
                id="TASK-MEM",
                epic_id="EPIC-MEM",
                title="Mem task",
                description="Lives only in memory",
                write_set=["mem.py"],
            )
        )
        session.add(epic)
        await session.commit()

    # A second, independent session must see the same in-memory database --
    # this is exactly what StaticPool + check_same_thread=False guarantees.
    async with session_factory() as session:
        reloaded = await session.get(EpicModel, "EPIC-MEM")
        assert reloaded is not None
        await session.refresh(reloaded, attribute_names=["tasks"])
        assert len(reloaded.tasks) == 1
        assert reloaded.tasks[0].write_set == ["mem.py"]

    await engine.dispose()
