"""Tests for `ai-os clean` — the git side against a real disposable repo, and
the Docker side degrading gracefully when `docker` isn't present."""
from __future__ import annotations

import subprocess
from pathlib import Path

from ai_os.core.cleaner import (
    delete_branches,
    list_ai_os_branches,
    list_docker_artifacts,
    prune_worktrees,
)


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@ai-os.local"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True)
    (repo / "a.txt").write_text("x\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True)
    return repo


def test_list_ai_os_branches_only_matches_ai_os(tmp_path):
    repo = _repo(tmp_path)
    for b in ("ai-os/TASK-1", "ai-os/epic-abc", "feature/x", "main-backup"):
        subprocess.run(["git", "branch", b], cwd=repo, check=True)
    got = set(list_ai_os_branches(repo))
    assert got == {"ai-os/TASK-1", "ai-os/epic-abc"}


def test_delete_branches_removes_them(tmp_path):
    repo = _repo(tmp_path)
    subprocess.run(["git", "branch", "ai-os/TASK-1"], cwd=repo, check=True)
    subprocess.run(["git", "branch", "ai-os/epic-x"], cwd=repo, check=True)
    deleted = delete_branches(repo, ["ai-os/TASK-1", "ai-os/epic-x"])
    assert set(deleted) == {"ai-os/TASK-1", "ai-os/epic-x"}
    assert list_ai_os_branches(repo) == []


def test_prune_worktrees_is_safe_noop(tmp_path):
    repo = _repo(tmp_path)
    prune_worktrees(repo)  # nothing to prune -> must not raise


def test_docker_artifacts_empty_without_docker(tmp_path):
    # A non-existent docker binary -> discovery degrades to empty, not a crash.
    art = list_docker_artifacts(docker_cli="ai-os-nonexistent-docker-binary")
    assert art.is_empty()
    assert art.images == [] and art.containers == [] and art.networks == []
