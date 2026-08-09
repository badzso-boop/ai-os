"""Unit tests for Safe Git MCP Tools and MCP Server Git integration."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
import subprocess
import pytest

from ai_os.mcp.git_tools import (
    git_create_branch,
    git_diff_summary,
    git_pull_main,
    git_status,
)
from ai_os.mcp.mcp_server import ToolContext, dispatch_tool_call


def _init_repo(path: Path) -> None:
    subprocess.run(["git", "init", "-b", "main"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "TestUser"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=path, check=True)


def _create_commit(path: Path, filename: str, content: str, msg: str = "commit") -> None:
    file_path = path / filename
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(content)
    subprocess.run(["git", "add", filename], cwd=path, check=True)
    subprocess.run(["git", "commit", "-m", msg], cwd=path, check=True)


def test_non_git_repository(tmp_path: Path) -> None:
    non_git = tmp_path / "empty_dir"
    non_git.mkdir()

    res_status = git_status(non_git)
    assert res_status["success"] is False
    assert "not a valid git repository" in res_status["error"] or "not a git repository" in res_status["error"]

    res_pull = git_pull_main(non_git)
    assert res_pull["success"] is False
    assert "not a valid git repository" in res_pull["error"] or "not a git repository" in res_pull["error"]

    res_branch = git_create_branch("feature", repo_path=non_git)
    assert res_branch["success"] is False
    assert "not a valid git repository" in res_branch["error"] or "not a git repository" in res_branch["error"]

    res_diff = git_diff_summary(non_git)
    assert res_diff["success"] is False
    assert "not a valid git repository" in res_diff["error"] or "not a git repository" in res_diff["error"]


def test_git_status_workflow(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    status = git_status(repo)
    assert status["success"] is True
    assert status["is_clean"] is True
    assert status["staged"] == []
    assert status["unstaged"] == []
    assert status["untracked"] == []

    _create_commit(repo, "initial.txt", "line 1\n", "initial commit")

    # Add untracked file
    (repo / "new_file.txt").write_text("hello\n")
    status = git_status(repo)
    assert status["is_clean"] is False
    assert status["untracked"] == ["new_file.txt"]

    # Stage file
    subprocess.run(["git", "add", "new_file.txt"], cwd=repo, check=True)
    status = git_status(repo)
    assert status["staged"] == ["new_file.txt"]

    # Modify staged file -> staged and unstaged
    (repo / "new_file.txt").write_text("hello world\n")
    status = git_status(repo)
    assert status["staged"] == ["new_file.txt"]
    assert status["unstaged"] == ["new_file.txt"]

    # Test renamed file
    subprocess.run(["git", "commit", "-am", "commit new_file"], cwd=repo, check=True)
    subprocess.run(["git", "mv", "new_file.txt", "renamed_file.txt"], cwd=repo, check=True)
    status = git_status(repo)
    assert status["staged"] == ["renamed_file.txt"]


def test_git_create_branch(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    _create_commit(repo, "file.txt", "v1\n", "initial")

    # Create branch with checkout=True
    res = git_create_branch("feature-1", repo_path=repo, checkout=True)
    assert res["success"] is True
    assert res["branch"] == "feature-1"
    assert git_status(repo)["branch"] == "feature-1"

    # Create branch with checkout=False
    res_no_checkout = git_create_branch("feature-2", repo_path=repo, checkout=False)
    assert res_no_checkout["success"] is True
    assert git_status(repo)["branch"] == "feature-1"

    # Duplicate branch error
    res_dup = git_create_branch("feature-1", repo_path=repo)
    assert res_dup["success"] is False
    assert "already exists" in res_dup["error"]

    # Ref starting with '-' error
    res_flag = git_create_branch("-b_bad", repo_path=repo)
    assert res_flag["success"] is False
    assert "cannot start with '-'" in res_flag["error"]

    # Invalid start point error
    res_sp = git_create_branch("feature-3", repo_path=repo, start_point="nonexistent_ref")
    assert res_sp["success"] is False
    assert "Invalid start point" in res_sp["error"]


def test_git_diff_summary(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    _create_commit(repo, "doc.txt", "line 1\nline 2\n", "initial")

    # Clean diff
    diff_clean = git_diff_summary(repo)
    assert diff_clean["success"] is True
    assert diff_clean["total_files_changed"] == 0

    # Unstaged modifications
    (repo / "doc.txt").write_text("line 1\nline 2 modified\nline 3\n")
    diff_unstaged = git_diff_summary(repo)
    assert diff_unstaged["total_files_changed"] == 1
    assert diff_unstaged["files"][0]["path"] == "doc.txt"
    assert diff_unstaged["files"][0]["status"] == "M"
    assert diff_unstaged["total_insertions"] == 2
    assert diff_unstaged["total_deletions"] == 1

    # Cached diff
    (repo / "staged.txt").write_text("staged content\n")
    subprocess.run(["git", "add", "staged.txt"], cwd=repo, check=True)

    diff_cached = git_diff_summary(repo, cached=True)
    assert diff_cached["total_files_changed"] == 1
    assert diff_cached["files"][0]["path"] == "staged.txt"
    assert diff_cached["files"][0]["status"] == "A"

    # Target ref starting with '-'
    diff_invalid_target = git_diff_summary(repo, target="-bad")
    assert diff_invalid_target["success"] is False
    assert "cannot start with '-'" in diff_invalid_target["error"]


def test_git_pull_main(tmp_path: Path) -> None:
    origin = tmp_path / "origin"
    clone = tmp_path / "clone"
    origin.mkdir()
    clone.mkdir()

    subprocess.run(["git", "init", "--bare", "-b", "main"], cwd=origin, check=True, capture_output=True)
    _init_repo(clone)
    subprocess.run(["git", "remote", "add", "origin", str(origin)], cwd=clone, check=True)
    _create_commit(clone, "main.txt", "main content\n", "initial")
    subprocess.run(["git", "push", "-u", "origin", "main"], cwd=clone, check=True)

    # Pull when clean and on main
    res_pull = git_pull_main(clone, main_branch="main", remote="origin")
    assert res_pull["success"] is True

    # Create feature branch
    git_create_branch("feature", repo_path=clone, checkout=True)

    # Update origin/main from outside
    other = tmp_path / "other"
    subprocess.run(["git", "clone", str(origin), str(other)], check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "OtherUser"], cwd=other, check=True)
    subprocess.run(["git", "config", "user.email", "other@example.com"], cwd=other, check=True)
    _create_commit(other, "main2.txt", "more content\n", "update main")
    subprocess.run(["git", "push", "origin", "main"], cwd=other, check=True)

    # Pull main while on feature branch in clone
    res_pull_feature = git_pull_main(clone, main_branch="main", remote="origin")
    assert res_pull_feature["success"] is True
    assert res_pull_feature["current_branch"] == "feature"
    assert res_pull_feature["main_branch"] == "main"

    # Dirty working tree error
    (clone / "dirty.txt").write_text("dirty\n")
    res_dirty = git_pull_main(clone, main_branch="main", remote="origin")
    assert res_dirty["success"] is False
    assert "uncommitted changes" in res_dirty["error"]

    # Invalid branch/remote argument
    res_bad_remote = git_pull_main(clone, remote="-invalid")
    assert res_bad_remote["success"] is False
    assert "cannot start with '-'" in res_bad_remote["error"]


def test_mcp_server_git_tools_dispatch(tmp_path: Path) -> None:
    async def _runner() -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        _init_repo(repo)
        _create_commit(repo, "README.md", "# AI-OS\n", "initial commit")

        ctx = ToolContext(worktree_path=repo, knowledge_engine=None, graph_load_error=None, sandbox_runner=None, sandbox_language=None)

        # Test dispatch git_status via mcp server
        status_res = await dispatch_tool_call(ctx, "git_status", {})
        assert status_res.is_error is False

        # Test dispatch git_create_branch
        create_res = await dispatch_tool_call(
            ctx, "git_create_branch", {"branch_name": "feature/mcp", "checkout": True}
        )
        assert create_res.is_error is False

        # Verify branch was checked out
        status_after = await dispatch_tool_call(ctx, "git_status", {})
        status_data_after = json.loads(status_after.content[0].text)
        assert status_data_after["branch"] == "feature/mcp"

        # Test dispatch git_create_branch missing required arg
        missing_arg_res = await dispatch_tool_call(ctx, "git_create_branch", {})
        assert missing_arg_res.is_error is True
        assert "Missing required argument 'branch_name'" in missing_arg_res.content[0].text

        # Test dispatch git_diff_summary
        (repo / "README.md").write_text("# AI-OS updated\n")
        diff_res = await dispatch_tool_call(ctx, "git_diff_summary", {})
        assert diff_res.is_error is False
        diff_data = json.loads(diff_res.content[0].text)
        assert diff_data["total_files_changed"] == 1

        # Test path traversal prevention for git tools
        traversal_res = await dispatch_tool_call(ctx, "git_status", {"repo_path": "../outside"})
        assert traversal_res.is_error is True
        assert "outside worktree root" in traversal_res.content[0].text

    asyncio.run(_runner())