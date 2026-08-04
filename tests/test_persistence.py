"""Real-SQLite tests for `ai_os.core.persistence` (Phase 5 Stage 3).

Same "real behavior over mocks" convention as `test_db.py`: a real
aiosqlite-backed file (`tmp_path`), no mocked sessions. Proves rows land with
the right FK chain (token_cost -> task -> epic), that FK enforcement is really
on (a cost row for an unknown task is rejected), and that the read-side
rollups the CLI uses are correct.
"""
from __future__ import annotations

import pytest
from sqlalchemy.exc import IntegrityError

from ai_os.core.models import TaskNode
from ai_os.core.persistence import Persistence
from ai_os.mcp.adapters.base_adapter import TokenUsage


def _file_url(tmp_path) -> str:
    return f"sqlite+aiosqlite:///{tmp_path / 'acct.db'}"


def _task(tid="T1", risk="LOW", write=None, deps=None) -> TaskNode:
    files = write or [f"{tid}.py"]
    return TaskNode(
        id=tid, title=f"title {tid}", description=f"desc {tid}", risk_level=risk,
        target_files=files, write_set=set(files), dependencies=deps or [],
    )


async def _open(tmp_path) -> Persistence:
    persistence, _engine = await Persistence.open(_file_url(tmp_path))
    return persistence


async def test_full_chain_epic_task_cost_and_lock_audit(tmp_path):
    p = await _open(tmp_path)
    await p.create_epic("E1", "add auth", "add JWT auth")
    await p.upsert_task(_task("T1", risk="HIGH"), "E1", assigned_model="sonnet", status="RUNNING")

    await p.record_token_cost(
        "T1", "anthropic", "sonnet", TokenUsage(input_tokens=100, output_tokens=40, estimated_usd_cost=0.02)
    )
    await p.record_lock_audit("T1", "src/auth.py", "WRITE", "ACQUIRE")
    await p.record_lock_audit("T1", "src/auth.py", "WRITE", "RELEASE")

    summaries = await p.epic_summaries()
    assert len(summaries) == 1
    s = summaries[0]
    assert s.id == "E1" and s.title == "add auth"
    assert s.total_tasks == 1
    assert s.input_tokens == 100 and s.output_tokens == 40
    assert s.total_usd == pytest.approx(0.02)


async def test_token_cost_requires_existing_task_fk(tmp_path):
    p = await _open(tmp_path)
    # No epic/task rows exist -> the FK to tasks.id must reject this.
    with pytest.raises(IntegrityError):
        await p.record_token_cost("GHOST", "gemini", "flash", TokenUsage(input_tokens=1))


async def test_upsert_task_updates_status_and_model(tmp_path):
    p = await _open(tmp_path)
    await p.create_epic("E1", "t", "p")
    await p.upsert_task(_task("T1"), "E1", assigned_model=None, status="PENDING")
    await p.update_task_status("T1", "COMPLETED")
    await p.upsert_task(_task("T1"), "E1", assigned_model="haiku", status="COMPLETED")

    summaries = await p.epic_summaries()
    assert summaries[0].completed_tasks == 1


async def test_provider_breakdown_groups_and_sums(tmp_path):
    p = await _open(tmp_path)
    await p.create_epic("E1", "t", "p")
    await p.upsert_task(_task("T1"), "E1")
    await p.upsert_task(_task("T2", write=["T2.py"]), "E1")

    await p.record_token_cost("T1", "gemini", "flash", TokenUsage(input_tokens=10, output_tokens=5, estimated_usd_cost=0.001))
    await p.record_token_cost("T2", "gemini", "flash", TokenUsage(input_tokens=20, output_tokens=8, estimated_usd_cost=0.002))
    await p.record_token_cost("T1", "anthropic", "sonnet", TokenUsage(input_tokens=100, output_tokens=50, estimated_usd_cost=0.05))

    rows = await p.provider_breakdown()
    by_key = {(r.provider, r.model_name): r for r in rows}
    assert by_key[("gemini", "flash")].calls == 2
    assert by_key[("gemini", "flash")].input_tokens == 30
    assert by_key[("anthropic", "sonnet")].total_usd == pytest.approx(0.05)
    # ordered by spend desc: anthropic/sonnet (0.05) before gemini/flash (0.003)
    assert rows[0].provider == "anthropic"


async def test_epic_total_usd_scoped_to_epic(tmp_path):
    p = await _open(tmp_path)
    await p.create_epic("E1", "t", "p")
    await p.create_epic("E2", "t2", "p2")
    await p.upsert_task(_task("A"), "E1")
    await p.upsert_task(_task("B"), "E2")
    await p.record_token_cost("A", "gemini", "flash", TokenUsage(estimated_usd_cost=0.10))
    await p.record_token_cost("B", "gemini", "flash", TokenUsage(estimated_usd_cost=0.99))

    assert await p.epic_total_usd("E1") == pytest.approx(0.10)
    assert await p.epic_total_usd("E2") == pytest.approx(0.99)


async def test_set_epic_status(tmp_path):
    p = await _open(tmp_path)
    await p.create_epic("E1", "t", "p", status="RUNNING")
    await p.set_epic_status("E1", "COMPLETED")
    assert (await p.epic_summaries())[0].status == "COMPLETED"


async def test_get_epic_returns_none_for_missing(tmp_path):
    p = await _open(tmp_path)
    assert await p.get_epic("nope") is None
    await p.create_epic("E1", "the title", "the prompt", status="RUNNING")
    row = await p.get_epic("E1")
    assert row is not None
    assert row.id == "E1" and row.title == "the title" and row.status == "RUNNING"


async def test_load_epic_tasks_reconstructs_tasknodes_and_status(tmp_path):
    p = await _open(tmp_path)
    await p.create_epic("E1", "t", "p")
    task = TaskNode(
        id="T1", title="ti", description="de", risk_level="HIGH",
        target_files=["a.py", "b.py"], read_set={"c.py"}, write_set={"a.py"},
        dependencies=["T0"], max_retries=5,
    )
    # T0 must exist as a task too for a realistic epic, but load doesn't validate
    # deps — reconstruct fidelity is what we assert here.
    await p.upsert_task(task, "E1", assigned_model="sonnet", status="COMPLETED")

    loaded = await p.load_epic_tasks("E1")
    assert len(loaded) == 1
    node, status = loaded[0]
    assert status == "COMPLETED"
    assert node.id == "T1" and node.risk_level == "HIGH" and node.max_retries == 5
    assert node.target_files == ["a.py", "b.py"]
    assert node.read_set == {"c.py"} and node.write_set == {"a.py"}
    assert node.dependencies == ["T0"]
