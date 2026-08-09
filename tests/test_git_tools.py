"""Unit tests for Safe Git MCP Tools and MCP Server Git integration."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
import shutil
import subprocess

import pytest

from ai_os.mcp import git_tools
from ai_os.mcp.git_tools import (
    git_create_branch,
    git_diff_summary,
    git_pull_main,
    git_status,
)
from ai_os.mcp.mcp_server import ToolContext, dispatch_tool_call, handle_list_tools

HAS_GIT = shutil.which("git") is not None

MOCK_GIT_REPOS: dict[Path, dict] = {}


def _mock_run_git(args: list[str], cwd: str | Path) -> tuple[int, str, str]:
    path = Path(cwd).resolve()
    found = None
    for p in MOCK_GIT_REPOS:
        try:
            path.relative_to(p)
            found = p
            break
        except ValueError:
            pass

    if not found:
        return (128, "", f"fatal: not a git repository (or any of the parent directories): {cwd}")

    repo_path = found
    repo = MOCK_GIT_REPOS[repo_path]

    cmd = args[0] if args else ""

    if args == ["rev-parse", "--is-inside-work-tree"]:
        return (0, "true", "")

    if cmd == "status":
        lines = [
            f"# branch.head {repo['branch']}",
            "# branch.oid 1234567890123456789012345678901234567890",
            "# branch.ab +0 -0",
        ]
        for f in repo_path.glob("**/*"):
            if f.is_file():
                rel = str(f.relative_to(repo_path))
                if (
                    rel not in repo["files"]
                    and rel not in repo["staged"]
                    and rel not in repo["untracked"]
                ):
                    repo["untracked"].append(rel)

        for u in repo["untracked"]:
            lines.append(f"? {u}")
        for s in repo["staged"]:
            if s in repo["unstaged"]:
                lines.append(f"1 MM ... {s}")
            else:
                lines.append(f"1 M. ... {s}")
        for un in repo["unstaged"]:
            if un not in repo["staged"]:
                lines.append(f"1 .M ... {un}")
        return (0, "\n".join(lines), "")

    if cmd == "branch" and len(args) >= 2 and args[1] == "--show-current":
        return (0, repo["branch"], "")

    if cmd == "rev-parse":
        ref = args[-1]
        if ref.startswith("refs/heads/"):
            bname = ref[len("refs/heads/") :]
            if bname in repo["branches"]:
                return (0, "1234567890123456789012345678901234567890", "")
            return (1, "", "fatal: Needed a single revision")
        if ref in repo["branches"] or ref in ("main", "HEAD", repo["branch"]):
            return (0, "1234567890123456789012345678901234567890", "")
        return (1, "", f"fatal: Needed a single revision '{ref}'")

    if cmd == "checkout":
        if "-b" in args:
            idx = args.index("-b")
            bname = args[idx + 1]
            repo["branches"].add(bname)
            repo["branch"] = bname
            return (0, f"Switched to a new branch '{bname}'", "")

    if cmd == "branch":
        if len(args) >= 2 and not args[1].startswith("-"):
            bname = args[1]
            repo["branches"].add(bname)
            return (0, "", "")

    if cmd == "diff":
        is_stat = "--stat" in args
        is_numstat = "--numstat" in args
        is_name_status = "--name-status" in args
        is_cached = "--cached" in args

        diff_files = []
        if is_cached:
            for s in repo["staged"]:
                diff_files.append((s, "A", 1, 0))
        else:
            for f_path in repo_path.glob("**/*"):
                if f_path.is_file():
                    rel = str(f_path.relative_to(repo_path))
                    if rel in repo["files"]:
                        curr = f_path.read_text()
                        old = repo["files"][rel]
                        if curr != old:
                            diff_files.append((rel, "M", 2, 1))

        if is_stat:
            if not diff_files:
                return (0, "", "")
            lines = [f"{p} | 3 ++-" for p, s, i, d in diff_files]
            lines.append(f" {len(diff_files)} file changed, 2 insertions(+), 1 deletion(-)")
            return (0, "\n".join(lines), "")
        elif is_numstat:
            lines = [f"{i}\t{d}\t{p}" for p, s, i, d in diff_files]
            return (0, "\n".join(lines), "")
        elif is_name_status:
            lines = [f"{s}\t{p}" for p, s, i, d in diff_files]
            return (0, "\n".join(lines), "")

    if cmd == "pull":
        return (0, "Already up to date.", "")

    if cmd == "fetch":
        return (0, "Fetch completed.", "")

    if cmd == "add":
        filename = args[1]
        if filename in repo["untracked"]:
            repo["untracked"].remove(filename)
        if filename not in repo["staged"]:
            repo["staged"].append(filename)
        return (0, "", "")

    if cmd == "commit":
        for s in list(repo["staged"]):
            file_p = repo_path / s
            if file_p.exists():
                repo["files"][s] = file_p.read_text()
        repo["staged"].clear()
        return (0, "[main 1234567] commit", "")

    return (0, "", "")


def _mock_subprocess_run(cmd: list[str] | str, cwd: str | Path | None = None, **kwargs) -> Any:
    if isinstance(cmd, list) and cmd and cmd[0] == "git":
        args = cmd[1:]
        rc, stdout, stderr = _mock_run_git(args, cwd or ".")
        if kwargs.get("check") and rc != 0:
            raise subprocess.CalledProcessError(rc, cmd, output=stdout, stderr=stderr)

        class MockProc:
            def __init__(self, code: int, out: str, err: str) -> None:
                self.returncode = code
                self.stdout = out
                self.stderr = err

        return MockProc(rc, stdout, stderr)

    return subprocess.run(cmd, cwd=cwd, **kwargs)


@pytest.fixture(autouse=True)
def _patch_git_if_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    if not HAS_GIT:
        monkeypatch.setattr(git_tools, "_run_git", _mock_run_git)
        monkeypatch.setattr(subprocess, "run", _mock_subprocess_run)


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    if HAS_GIT:
        subprocess.run(["git", "init", "-b", "main"], cwd=path, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "TestUser"], cwd=path, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=path, check=True)
    else:
        MOCK_GIT_REPOS[path.resolve()] = {
            "branch": "main",
            "branches": {"main"},
            "staged": [],
            "unstaged": [],
            "untracked": [],
            "files": {},
            "commits": [],
        }


def _create_commit(path: Path, filename: str, content: str, msg: str = "commit") -> None:
    file_path = path / filename
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(content)
    if HAS_GIT:
        subprocess.run(["git", "add", filename], cwd=path, check=True)
        subprocess.run(["git", "commit", "-m", msg], cwd=path, check=True)
    else:
        repo = MOCK_GIT_REPOS[path.resolve()]
        if filename in repo["untracked"]:
            repo["untracked"].remove(filename)
        repo["files"][filename] = content


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

    if HAS_GIT:
        subprocess.run(["git", "init", "--bare", "-b", "main"], cwd=origin, check=True, capture_output=True)
    _init_repo(clone)
    if HAS_GIT:
        subprocess.run(["git", "remote", "add", "origin", str(origin)], cwd=clone, check=True)
    _create_commit(clone, "main.txt", "main content\n", "initial")
    if HAS_GIT:
        subprocess.run(["git", "push", "-u", "origin", "main"], cwd=clone, check=True)

    # Pull when clean and on main
    res_pull = git_pull_main(clone, main_branch="main", remote="origin")
    assert res_pull["success"] is True

    # Create feature branch
    git_create_branch("feature", repo_path=clone, checkout=True)

    # Update origin/main from outside
    other = tmp_path / "other"
    if HAS_GIT:
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

        ctx = ToolContext(worktree_root=repo)

        # Test list_tools contains safe git tools
        tools_res = await handle_list_tools(ctx)
        tool_names = [t.name for t in tools_res.tools]
        assert "git_status" in tool_names
        assert "git_pull_main" in tool_names
        assert "git_create_branch" in tool_names
        assert "git_diff_summary" in tool_names

        # Test dispatch git_status
        status_res = await dispatch_tool_call(ctx, "git_status", {"repo_path": "."})
        assert status_res.isError is False
        status_data = json.loads(status_res.content[0].text)
        assert status_data["branch"] == "main"
        assert status_data["is_clean"] is True

        # Test dispatch git_create_branch
        create_res = await dispatch_tool_call(
            ctx, "git_create_branch", {"branch_name": "feature/mcp", "checkout": True}
        )
        assert create_res.isError is False

        # Verify branch was checked out
        status_after = await dispatch_tool_call(ctx, "git_status", {})
        status_data_after = json.loads(status_after.content[0].text)
        assert status_data_after["branch"] == "feature/mcp"

        # Test dispatch git_create_branch missing required arg
        missing_arg_res = await dispatch_tool_call(ctx, "git_create_branch", {})
        assert missing_arg_res.isError is True
        assert "Missing required argument 'branch_name'" in missing_arg_res.content[0].text

        # Test dispatch git_diff_summary
        (repo / "README.md").write_text("# AI-OS updated\n")
        diff_res = await dispatch_tool_call(ctx, "git_diff_summary", {})
        assert diff_res.isError is False
        diff_data = json.loads(diff_res.content[0].text)
        assert diff_data["total_files_changed"] == 1

        # Test path traversal prevention for git tools
        traversal_res = await dispatch_tool_call(ctx, "git_status", {"repo_path": "../outside"})
        assert traversal_res.isError is True
        assert "outside worktree root" in traversal_res.content[0].text

    asyncio.run(_runner())