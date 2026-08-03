"""Integration test tying `LockManager` and `GitStagingEngine` together.

Demonstrates doc 16's two Phase 2 acceptance criteria concretely, against a
real disposable git repository (no mocks, per this project's testing
philosophy):

  * Test A: two tasks with disjoint write_sets run their worktree/merge
    pipeline concurrently without a merge conflict — whichever merges
    second genuinely rebases onto the first's commit.
  * Test B: two tasks with the *same* write_set and no dependency edge
    between them are serialized end-to-end by the Lock Manager alone (not
    by anything in the DAG Planner) — their critical sections never
    overlap in wall-clock time, and because of that serialization the
    second task's git operations never actually race the first's, so no
    rebase conflict is even possible.
"""
from __future__ import annotations

import asyncio
import subprocess
import time
from pathlib import Path

import pytest

from ai_os.core.lock_manager import LockManager
from ai_os.core.staging import GitStagingEngine


@pytest.fixture()
def git_repo(tmp_path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@ai-os.local"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "AI-OS Test"], cwd=repo, check=True)
    (repo / "README.md").write_text("init\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True)
    return repo


async def _always_valid(_wt_path: Path) -> bool:
    return True


async def test_disjoint_tasks_merge_concurrently_with_real_rebase(git_repo: Path):
    lock_manager = LockManager()
    staging = GitStagingEngine(git_repo)

    async def run(task_id: str, filename: str) -> bool:
        async with lock_manager.locks(task_id, read_set=set(), write_set={filename}):
            wt_path = await staging.create_worktree(task_id)
            (wt_path / filename).write_text(f"content from {task_id}\n")
            return await staging.stage_and_merge_task(task_id, f"{task_id}: add {filename}", _always_valid)

    results = await asyncio.gather(
        run("TASK-A", "fileA.txt"),
        run("TASK-B", "fileB.txt"),
    )

    assert results == [True, True]

    assert (git_repo / "fileA.txt").read_text() == "content from TASK-A\n"
    assert (git_repo / "fileB.txt").read_text() == "content from TASK-B\n"

    log = subprocess.run(
        ["git", "log", "--oneline", "main"], cwd=git_repo, check=True, capture_output=True, text=True
    ).stdout
    assert "TASK-A" in log
    assert "TASK-B" in log

    # No leftover worktrees, no conflict markers in either merged file.
    wt_list = subprocess.run(
        ["git", "worktree", "list"], cwd=git_repo, check=True, capture_output=True, text=True
    ).stdout
    assert wt_list.strip().count("\n") == 0  # only the primary worktree line
    assert "<<<<<<<" not in (git_repo / "fileA.txt").read_text()
    assert "<<<<<<<" not in (git_repo / "fileB.txt").read_text()


async def test_same_file_tasks_are_serialized_by_lock_manager_not_the_planner(git_repo: Path):
    lock_manager = LockManager()
    staging = GitStagingEngine(git_repo)
    events: list[tuple[str, str, float]] = []

    # No dependency edge between these two tasks anywhere in this test — the
    # planner would happily place them in the same generation. It's the
    # Lock Manager's write_set conflict, not DAG topology, that must
    # prevent them from ever actually racing.
    async def run(task_id: str, content: str) -> bool:
        async with lock_manager.locks(task_id, read_set=set(), write_set={"shared.txt"}):
            events.append((task_id, "start", time.monotonic()))
            await asyncio.sleep(0.05)
            wt_path = await staging.create_worktree(task_id)
            (wt_path / "shared.txt").write_text(content)
            result = await staging.stage_and_merge_task(task_id, f"{task_id}: write shared.txt", _always_valid)
            events.append((task_id, "end", time.monotonic()))
            return result

    results = await asyncio.gather(
        run("TASK-C", "written by TASK-C\n"),
        run("TASK-D", "written by TASK-D\n"),
    )

    # Because the Lock Manager fully serializes them, neither task's git
    # operations ever race the other's, so both merges succeed cleanly —
    # there is no rebase conflict to even hit here.
    assert results == [True, True]

    intervals = {}
    for task_id, kind, ts in events:
        intervals.setdefault(task_id, {})[kind] = ts
    (start_c, end_c) = intervals["TASK-C"]["start"], intervals["TASK-C"]["end"]
    (start_d, end_d) = intervals["TASK-D"]["start"], intervals["TASK-D"]["end"]

    assert end_c <= start_d or end_d <= start_c, (
        "TASK-C and TASK-D critical sections overlapped in time — "
        "the Lock Manager failed to serialize same-file write access"
    )

    # The final content on main is whichever task merged second — a clean,
    # sequential last-write-wins, not a lost update or a conflict marker.
    final_content = (git_repo / "shared.txt").read_text()
    assert final_content in ("written by TASK-C\n", "written by TASK-D\n")
    assert "<<<<<<<" not in final_content
