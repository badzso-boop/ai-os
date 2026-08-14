"""Tests for `ai_os.core.staging.GitStagingEngine`.

Per this project's testing philosophy (real behavior over mocks), these
tests run real `git init`/`worktree`/`rebase`/`merge` subprocess calls
against a disposable throwaway repository created fresh under `tmp_path`
for every test — never against the actual ai-os repository.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from ai_os.core.staging import (
    GitCommandError,
    GitStagingEngine,
    ValidationCallbackError,
)


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


async def _always_invalid(_wt_path: Path) -> bool:
    return False


def _branch_exists(repo: Path, branch: str) -> bool:
    res = subprocess.run(
        ["git", "branch", "--list", branch],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return bool(res.stdout.strip())


def _rebase_in_progress(repo: Path) -> bool:
    res = subprocess.run(
        ["git", "status"], cwd=repo, check=True, capture_output=True, text=True
    )
    return "rebase in progress" in res.stdout.lower()


async def test_create_worktree_fresh(git_repo):
    engine = GitStagingEngine(git_repo)
    wt_path = await engine.create_worktree("TASK-1")

    assert wt_path.exists()
    assert (wt_path / ".git").exists()
    res = subprocess.run(
        ["git", "branch", "--show-current"],
        cwd=wt_path,
        check=True,
        capture_output=True,
        text=True,
    )
    assert res.stdout.strip() == "ai-os/TASK-1"


async def test_create_worktree_idempotent_crash_recovery(git_repo):
    engine = GitStagingEngine(git_repo)

    wt_path_1 = await engine.create_worktree("TASK-2")
    assert wt_path_1.exists()

    # Simulate a crashed/retried task: create_worktree called again for the
    # same task_id must succeed, not raise on "path already exists" /
    # "branch already exists".
    wt_path_2 = await engine.create_worktree("TASK-2")
    assert wt_path_2 == wt_path_1
    assert wt_path_2.exists()


async def test_cleanup_keep_branch_preserves_commits(git_repo):
    # A BLOCKED task keeps its branch (with the committed, failing code) for
    # inspection, even though the worktree directory is removed.
    engine = GitStagingEngine(git_repo)
    wt_path = await engine.create_worktree("TASK-B")
    (wt_path / "wip.txt").write_text("failing attempt\n")
    subprocess.run(["git", "add", "."], cwd=wt_path, check=True)
    subprocess.run(["git", "commit", "-m", "wip"], cwd=wt_path, check=True)

    await engine.cleanup_worktree("TASK-B", keep_branch=True)
    assert not (git_repo / ".ai-os" / "worktrees" / "TASK-B").exists()  # worktree gone
    assert _branch_exists(git_repo, "ai-os/TASK-B")  # branch kept
    # the committed code is recoverable from the branch
    show = subprocess.run(["git", "show", "ai-os/TASK-B:wip.txt"], cwd=git_repo, capture_output=True, text=True)
    assert show.stdout == "failing attempt\n"


async def test_create_integration_branch_reuse_preserves_work(git_repo):
    # resume must continue on the existing integration branch (with its completed
    # commits), not reset it to main.
    engine = GitStagingEngine(git_repo)
    await engine.create_integration_branch("ai-os/epic-x", from_ref="main")
    (git_repo / "done.txt").write_text("completed task\n")
    subprocess.run(["git", "add", "."], cwd=git_repo, check=True)
    subprocess.run(["git", "commit", "-m", "a completed task"], cwd=git_repo, check=True)
    subprocess.run(["git", "checkout", "main"], cwd=git_repo, check=True)

    # reuse: the branch's commit must survive (NOT reset to main).
    await engine.create_integration_branch("ai-os/epic-x", from_ref="main", reuse_existing=True)
    assert (git_repo / "done.txt").read_text() == "completed task\n"

    # non-reuse (fresh run): resets from main -> the file is gone.
    subprocess.run(["git", "checkout", "main"], cwd=git_repo, check=True)
    await engine.create_integration_branch("ai-os/epic-x", from_ref="main")
    assert not (git_repo / "done.txt").exists()


async def test_stage_and_merge_happy_path(git_repo):
    engine = GitStagingEngine(git_repo)
    wt_path = await engine.create_worktree("TASK-3")

    (wt_path / "feature.txt").write_text("hello from TASK-3\n")

    result = await engine.stage_and_merge_task("TASK-3", "add feature.txt", _always_valid)

    assert result is True
    assert (git_repo / "feature.txt").read_text() == "hello from TASK-3\n"

    # Worktree and branch should be gone (cleanup ran).
    assert not (git_repo / ".ai-os" / "worktrees" / "TASK-3").exists()
    assert not _branch_exists(git_repo, "ai-os/TASK-3")


async def test_disjoint_concurrent_merge_with_real_rebase(git_repo):
    engine = GitStagingEngine(git_repo)

    wt_a = await engine.create_worktree("TASK-A")
    wt_b = await engine.create_worktree("TASK-B")

    (wt_a / "file_a.txt").write_text("from A\n")
    (wt_b / "file_b.txt").write_text("from B\n")

    # Merge A first: fast-forward, no rebase conflict possible (main hasn't
    # moved since A's worktree was created).
    result_a = await engine.stage_and_merge_task("TASK-A", "add file_a", _always_valid)
    assert result_a is True
    assert (git_repo / "file_a.txt").exists()

    # Now merge B: main has moved (A's merge), so this must perform a real
    # `git rebase main` inside wt_b and still succeed since the changes are
    # disjoint.
    result_b = await engine.stage_and_merge_task("TASK-B", "add file_b", _always_valid)
    assert result_b is True

    assert (git_repo / "file_a.txt").read_text() == "from A\n"
    assert (git_repo / "file_b.txt").read_text() == "from B\n"


async def test_rebase_conflict_returns_false_and_leaves_clean_state(git_repo):
    # Shared pre-existing file both worktrees will edit on the same line.
    shared = git_repo / "shared.txt"
    shared.write_text("original line\n")
    subprocess.run(["git", "add", "."], cwd=git_repo, check=True)
    subprocess.run(["git", "commit", "-m", "add shared.txt"], cwd=git_repo, check=True)

    engine = GitStagingEngine(git_repo)
    wt_a = await engine.create_worktree("TASK-C")
    wt_b = await engine.create_worktree("TASK-D")

    (wt_a / "shared.txt").write_text("changed by C\n")
    (wt_b / "shared.txt").write_text("changed by D\n")

    result_a = await engine.stage_and_merge_task("TASK-C", "edit shared.txt (C)", _always_valid)
    assert result_a is True

    result_b = await engine.stage_and_merge_task("TASK-D", "edit shared.txt (D)", _always_valid)

    assert result_b is False
    # git rebase --abort must have actually run: repo is clean, not mid-rebase.
    assert not _rebase_in_progress(wt_b)
    # No cleanup on failure: worktree/branch for the failed task remain.
    wt_d_path = git_repo / ".ai-os" / "worktrees" / "TASK-D"
    assert wt_d_path.exists()
    assert _branch_exists(git_repo, "ai-os/TASK-D")


async def test_commit_with_nothing_to_commit_does_not_raise(git_repo):
    engine = GitStagingEngine(git_repo)
    wt_path = await engine.create_worktree("TASK-E")

    # No file changes at all in the worktree.
    result = await engine.stage_and_merge_task("TASK-E", "no-op commit", _always_valid)

    # Must not raise GitCommandError from the commit step; whatever the
    # outcome, rebase/merge/validator path should run cleanly.
    assert result is True
    assert not (git_repo / ".ai-os" / "worktrees" / "TASK-E").exists()


async def test_abandon_task_removes_worktree_and_branch(git_repo):
    engine = GitStagingEngine(git_repo)
    wt_path = await engine.create_worktree("TASK-F")
    assert wt_path.exists()
    assert _branch_exists(git_repo, "ai-os/TASK-F")

    await engine.abandon_task("TASK-F")

    res = subprocess.run(
        ["git", "worktree", "list"],
        cwd=git_repo,
        check=True,
        capture_output=True,
        text=True,
    )
    assert str(wt_path) not in res.stdout
    assert not _branch_exists(git_repo, "ai-os/TASK-F")


async def test_validator_exception_surfaces_as_validation_callback_error(git_repo):
    async def _boom(_wt_path: Path) -> bool:
        raise RuntimeError("sandbox crashed")

    engine = GitStagingEngine(git_repo)
    wt_path = await engine.create_worktree("TASK-G")
    (wt_path / "risky.txt").write_text("should not reach main\n")

    with pytest.raises(ValidationCallbackError):
        await engine.stage_and_merge_task("TASK-G", "add risky.txt", _boom)

    # Nothing merged into main.
    assert not (git_repo / "risky.txt").exists()


async def test_validator_returns_false_leaves_worktree_for_retry(git_repo):
    engine = GitStagingEngine(git_repo)
    wt_path = await engine.create_worktree("TASK-H")
    (wt_path / "unwanted.txt").write_text("fails validation\n")

    result = await engine.stage_and_merge_task("TASK-H", "add unwanted.txt", _always_invalid)

    assert result is False
    assert not (git_repo / "unwanted.txt").exists()
    assert (git_repo / ".ai-os" / "worktrees" / "TASK-H").exists()


async def test_final_checkout_merge_git_error_returns_false(git_repo, monkeypatch):
    engine = GitStagingEngine(git_repo)
    wt_path = await engine.create_worktree("TASK-I")
    (wt_path / "final.txt").write_text("content\n")

    original_run_git = engine._run_git

    async def mock_run_git(args, cwd, check=True):
        if args[:2] == ["merge", "--ff-only"]:
            raise GitCommandError(args, 1, "fatal: Not possible to fast-forward, aborting.")
        return await original_run_git(args, cwd, check=check)

    monkeypatch.setattr(engine, "_run_git", mock_run_git)

    result = await engine.stage_and_merge_task("TASK-I", "add final.txt", _always_valid)

    assert result is False
    assert (git_repo / ".ai-os" / "worktrees" / "TASK-I").exists()


async def test_concurrent_pre_validation(git_repo):
    import asyncio
    engine = GitStagingEngine(git_repo)
    wt_a = await engine.create_worktree("TASK-CONC-A")
    wt_b = await engine.create_worktree("TASK-CONC-B")

    (wt_a / "file_a.txt").write_text("from conc A\n")
    (wt_b / "file_b.txt").write_text("from conc B\n")

    validation_started = []

    async def slow_validator(wt_path: Path) -> bool:
        validation_started.append(wt_path.name)
        await asyncio.sleep(0.1)
        return True

    # Launch both stage_and_merge_task calls concurrently
    res_a, res_b = await asyncio.gather(
        engine.stage_and_merge_task("TASK-CONC-A", "add file_a", slow_validator),
        engine.stage_and_merge_task("TASK-CONC-B", "add file_b", slow_validator),
    )

    assert res_a is True
    assert res_b is True
    # Initial validation starts for both before any merge lock is acquired, then second task re-validates after rebase
    assert validation_started[:2] == ["TASK-CONC-A", "TASK-CONC-B"] or validation_started[:2] == ["TASK-CONC-B", "TASK-CONC-A"]
    assert len(validation_started) == 3 or len(validation_started) == 4

