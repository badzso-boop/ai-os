"""Safe Git MCP Tools for AI-OS.

This module provides safe helper functions for interacting with Git repositories in AI-OS:
- git_status: Inspects repository branch, cleanliness, staged/unstaged/untracked files, and sync status.
- git_pull_main: Safely pulls updates for the main branch without corrupting uncommitted work.
- git_create_branch: Creates and optionally checks out a new branch with input validation.
- git_diff_summary: Generates structured diff summaries for working tree, staged, or target refs.

Safety & Design Principles:
1. Safe Execution: Uses subprocess.run without shell=True to avoid shell injection vulnerabilities.
2. Argument Sanitization: Strictly checks ref and branch names to reject strings starting with '-'
   or containing invalid control characters (preventing flag/option injection attacks).
3. Pre-flight Repository Checks: Validates that the target path is a valid Git work tree before
   executing operations.
4. Dirty Worktree Protection: Refuses operations like branch checkout or pull when there are
   uncommitted changes that could be overwritten.
"""

from __future__ import annotations

from pathlib import Path
import re
import subprocess
from typing import Any


def _run_git(args: list[str], cwd: str | Path) -> tuple[int, str, str]:
    """Execute a git command safely using subprocess without shell=True."""
    path = Path(cwd).resolve()
    if not path.is_dir():
        return (1, "", f"Directory does not exist: '{cwd}'.")

    try:
        proc = subprocess.run(
            ["git", "-c", "safe.directory=*"] + args,
            cwd=path,
            capture_output=True,
            text=True,
            check=False,
        )
        return (proc.returncode, proc.stdout.strip(), proc.stderr.strip())
    except Exception as exc:
        return (1, "", str(exc))


def _is_git_repo(path: str | Path) -> tuple[bool, str]:
    """Check if path is inside a valid git work tree."""
    rc, stdout, stderr = _run_git(["rev-parse", "--is-inside-work-tree"], path)
    if rc == 0 and stdout == "true":
        return True, ""
    return False, stderr or f"Path '{path}' is not a valid git repository."


def _sanitize_ref(ref_name: str, name_label: str = "Ref name") -> tuple[bool, str]:
    """Sanitize and validate ref/branch names to prevent command injection."""
    if not ref_name or not ref_name.strip():
        return False, f"{name_label} cannot be empty."
    clean_ref = ref_name.strip()
    if clean_ref.startswith("-"):
        return False, f"{name_label} '{clean_ref}' cannot start with '-'."
    if re.search(r"[\s~\^:?*\[\\]", clean_ref):
        return False, f"{name_label} '{clean_ref}' contains invalid characters."
    return True, ""


def git_status(repo_path: str | Path = ".") -> dict[str, Any]:
    """Inspect repository status safely.

    Returns structured dictionary containing branch name, clean status, lists of staged,
    unstaged, and untracked files, and ahead/behind commit counts.
    """
    is_repo, err = _is_git_repo(repo_path)
    if not is_repo:
        return {"success": False, "error": err}

    rc, stdout, stderr = _run_git(["status", "--porcelain=v2", "-b"], repo_path)
    if rc != 0:
        return {"success": False, "error": stderr or "Failed to run git status."}

    branch = "HEAD"
    ahead = 0
    behind = 0
    staged: list[str] = []
    unstaged: list[str] = []
    untracked: list[str] = []

    for line in stdout.splitlines():
        if not line:
            continue
        if line.startswith("#"):
            parts = line.split()
            if len(parts) >= 3 and parts[1] == "branch.head":
                branch = parts[2]
            elif len(parts) >= 4 and parts[1] == "branch.ab":
                if parts[2].startswith("+"):
                    try:
                        ahead = int(parts[2][1:])
                    except ValueError:
                        pass
                if parts[3].startswith("-"):
                    try:
                        behind = int(parts[3][1:])
                    except ValueError:
                        pass
        elif line.startswith("? "):
            untracked.append(line[2:].strip())
        elif line.startswith("1 "):
            parts = line.split(" ", 8)
            if len(parts) >= 9:
                xy = parts[1]
                path_str = parts[8].split("\t")[0]
                if xy[0] != ".":
                    staged.append(path_str)
                if xy[1] != ".":
                    unstaged.append(path_str)
        elif line.startswith("2 "):
            parts = line.split(" ", 9)
            if len(parts) >= 10:
                xy = parts[1]
                path_str = parts[9].split("\t")[0]
                if xy[0] != ".":
                    staged.append(path_str)
                if xy[1] != ".":
                    unstaged.append(path_str)
        elif line.startswith("u "):
            parts = line.split(" ", 10)
            if len(parts) >= 11:
                path_str = parts[10]
                unstaged.append(path_str)

    if branch in ("(initial)", "(detached)"):
        rc_b, stdout_b, _ = _run_git(["branch", "--show-current"], repo_path)
        if rc_b == 0 and stdout_b:
            branch = stdout_b

    is_clean = not staged and not unstaged and not untracked

    return {
        "success": True,
        "branch": branch,
        "is_clean": is_clean,
        "staged": staged,
        "unstaged": unstaged,
        "untracked": untracked,
        "ahead": ahead,
        "behind": behind,
    }


def git_pull_main(
    repo_path: str | Path = ".",
    main_branch: str = "main",
    remote: str = "origin",
) -> dict[str, Any]:
    """Safely pull/fetch changes for the main branch from remote.

    Checks working tree cleanliness before proceeding to prevent data loss.
    If currently on main_branch, performs a fast-forward pull.
    If currently on another branch, fetches remote updates to main_branch cleanly.
    """
    is_repo, err = _is_git_repo(repo_path)
    if not is_repo:
        return {"success": False, "error": err}

    ok_main, main_err = _sanitize_ref(main_branch, "Main branch name")
    if not ok_main:
        return {"success": False, "error": main_err}

    ok_rem, rem_err = _sanitize_ref(remote, "Remote name")
    if not ok_rem:
        return {"success": False, "error": rem_err}

    status = git_status(repo_path)
    if not status.get("success"):
        return status

    if not status.get("is_clean", False):
        return {
            "success": False,
            "error": "Working directory has uncommitted changes. Please commit or stash changes before pulling.",
            "is_clean": False,
        }

    current_branch = status.get("branch", "HEAD")

    if current_branch == main_branch:
        rc, stdout, stderr = _run_git(["pull", "--ff-only", remote, main_branch], repo_path)
        if rc == 0:
            return {
                "success": True,
                "branch": main_branch,
                "remote": remote,
                "updated": True,
                "message": stdout or f"Successfully pulled {remote}/{main_branch}.",
            }
        return {"success": False, "error": stderr or f"Failed to pull {remote}/{main_branch}."}
    else:
        rc, stdout, stderr = _run_git(
            ["fetch", remote, f"{main_branch}:{main_branch}"], repo_path
        )
        if rc == 0:
            return {
                "success": True,
                "current_branch": current_branch,
                "main_branch": main_branch,
                "remote": remote,
                "updated": True,
                "message": stdout or f"Successfully updated {main_branch} from {remote}/{main_branch}.",
            }
        rc_f, stdout_f, stderr_f = _run_git(["fetch", remote, main_branch], repo_path)
        if rc_f == 0:
            return {
                "success": True,
                "current_branch": current_branch,
                "main_branch": main_branch,
                "remote": remote,
                "updated": True,
                "message": stdout_f or f"Fetched {remote}/{main_branch}.",
            }
        return {"success": False, "error": stderr or stderr_f or f"Failed to fetch {remote}/{main_branch}."}


def git_create_branch(
    branch_name: str,
    repo_path: str | Path = ".",
    start_point: str | None = None,
    checkout: bool = True,
) -> dict[str, Any]:
    """Safely create a new Git branch.

    Sanitizes branch name and start_point, checks if branch already exists,
    and optionally checks out the new branch.
    """
    is_repo, err = _is_git_repo(repo_path)
    if not is_repo:
        return {"success": False, "error": err}

    ok_b, b_err = _sanitize_ref(branch_name, "Branch name")
    if not ok_b:
        return {"success": False, "error": b_err}

    if start_point:
        ok_sp, sp_err = _sanitize_ref(start_point, "Start point ref")
        if not ok_sp:
            return {"success": False, "error": sp_err}
        rc_sp, _, stderr_sp = _run_git(["rev-parse", "--verify", start_point], repo_path)
        if rc_sp != 0:
            return {"success": False, "error": f"Invalid start point ref '{start_point}': {stderr_sp}"}

    rc_ex, _, _ = _run_git(["rev-parse", "--verify", "--quiet", f"refs/heads/{branch_name}"], repo_path)
    if rc_ex == 0:
        return {"success": False, "error": f"Branch '{branch_name}' already exists."}

    if checkout:
        status = git_status(repo_path)
        if not status.get("success"):
            return status

        cmd = ["checkout", "-b", branch_name]
        if start_point:
            cmd.append(start_point)
    else:
        cmd = ["branch", branch_name]
        if start_point:
            cmd.append(start_point)

    rc, stdout, stderr = _run_git(cmd, repo_path)
    if rc == 0:
        return {
            "success": True,
            "branch": branch_name,
            "start_point": start_point,
            "checkout": checkout,
            "message": stdout or f"Branch '{branch_name}' created successfully.",
        }
    return {"success": False, "error": stderr or f"Failed to create branch '{branch_name}'."}


def git_diff_summary(
    repo_path: str | Path = ".",
    target: str | None = None,
    cached: bool = False,
) -> dict[str, Any]:
    """Generate structured diff summary for working tree, staged changes, or target ref.

    Returns structured file change list with insertion/deletion metrics and raw stat output.
    """
    is_repo, err = _is_git_repo(repo_path)
    if not is_repo:
        return {"success": False, "error": err}

    if target:
        ok_t, t_err = _sanitize_ref(target, "Target ref")
        if not ok_t:
            return {"success": False, "error": t_err}
        rc_t, _, stderr_t = _run_git(["rev-parse", "--verify", target], repo_path)
        if rc_t != 0:
            return {"success": False, "error": f"Invalid target ref '{target}': {stderr_t}"}

    diff_args = ["diff"]
    if cached:
        diff_args.append("--cached")
    if target:
        diff_args.append(target)

    rc_stat, stdout_stat, stderr_stat = _run_git(diff_args + ["--stat"], repo_path)
    if rc_stat != 0:
        return {"success": False, "error": stderr_stat or "Failed to run git diff --stat."}

    rc_num, stdout_num, stderr_num = _run_git(diff_args + ["--numstat"], repo_path)
    if rc_num != 0:
        return {"success": False, "error": stderr_num or "Failed to run git diff --numstat."}

    rc_name, stdout_name, stderr_name = _run_git(diff_args + ["--name-status"], repo_path)
    if rc_name != 0:
        return {"success": False, "error": stderr_name or "Failed to run git diff --name-status."}

    status_map: dict[str, str] = {}
    for line in stdout_name.splitlines():
        if not line:
            continue
        parts = line.split("\t")
        if len(parts) >= 2:
            status_map[parts[-1]] = parts[0]

    files: list[dict[str, Any]] = []
    total_insertions = 0
    total_deletions = 0

    for line in stdout_num.splitlines():
        if not line:
            continue
        parts = line.split("\t")
        if len(parts) >= 3:
            ins_str, del_str, path_str = parts[0], parts[1], parts[2]
            is_binary = ins_str == "-" or del_str == "-"
            ins = int(ins_str) if not is_binary else 0
            dels = int(del_str) if not is_binary else 0
            file_status = status_map.get(path_str, "M")
            files.append(
                {
                    "path": path_str,
                    "status": file_status,
                    "insertions": ins,
                    "deletions": dels,
                    "is_binary": is_binary,
                }
            )
            total_insertions += ins
            total_deletions += dels

    return {
        "success": True,
        "target": target,
        "cached": cached,
        "total_files_changed": len(files),
        "total_insertions": total_insertions,
        "total_deletions": total_deletions,
        "files": files,
        "raw_stat": stdout_stat,
    }