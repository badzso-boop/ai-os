"""Tests for the operational-hardening pieces: sensitive-file detection and
the per-project cross-run lock."""
from __future__ import annotations

import os

import pytest

from ai_os.core.run_lock import RunLockError, acquire_epic_run_lock, epic_run_lock
from ai_os.core.sensitive_files import sensitive_paths


# -- sensitive-file detection ------------------------------------------------


def test_flags_github_workflows():
    flagged = sensitive_paths([".github/workflows/ci.yml", "src/app.py"])
    assert flagged == [".github/workflows/ci.yml"]


def test_flags_env_dockerfile_secrets():
    flagged = set(sensitive_paths([
        ".env", ".env.production", "Dockerfile", "backend/Dockerfile",
        "config/secrets.yml", "src/main.py",
    ]))
    assert ".env" in flagged and ".env.production" in flagged
    assert "Dockerfile" in flagged and "backend/Dockerfile" in flagged
    assert "config/secrets.yml" in flagged
    assert "src/main.py" not in flagged


def test_flags_ai_os_config_tampering():
    assert ".ai-os/sandbox.json" in sensitive_paths([".ai-os/sandbox.json"])


def test_clean_paths_return_empty():
    assert sensitive_paths(["src/a.py", "tests/b.py", "README.md", "frontend/App.tsx"]) == []


# -- cross-run lock ----------------------------------------------------------


def test_second_acquire_on_same_repo_fails(tmp_path, monkeypatch):
    monkeypatch.setenv("AI_OS_HOME", str(tmp_path / "home"))
    repo = tmp_path / "repo"
    repo.mkdir()
    fd = acquire_epic_run_lock(repo)
    try:
        with pytest.raises(RunLockError):
            acquire_epic_run_lock(repo)
    finally:
        os.close(fd)


def test_release_allows_reacquire(tmp_path, monkeypatch):
    monkeypatch.setenv("AI_OS_HOME", str(tmp_path / "home"))
    repo = tmp_path / "repo"
    repo.mkdir()
    with epic_run_lock(repo):
        pass  # released on exit
    fd = acquire_epic_run_lock(repo)  # can re-acquire now
    os.close(fd)


def test_different_repos_do_not_conflict(tmp_path, monkeypatch):
    monkeypatch.setenv("AI_OS_HOME", str(tmp_path / "home"))
    r1, r2 = tmp_path / "r1", tmp_path / "r2"
    r1.mkdir()
    r2.mkdir()
    fd1 = acquire_epic_run_lock(r1)
    fd2 = acquire_epic_run_lock(r2)  # different repo -> no conflict
    os.close(fd1)
    os.close(fd2)
