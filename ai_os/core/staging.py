"""Async Git worktree isolation and rebase/re-validate/merge staging engine (doc 09).

Deliberate deviations from the illustrative blueprint in
`docs/09_GIT_WORKTREE_STAGING_ENGINE.md` §5 — that snippet has real gaps that
would hard-fail or misbehave against real git, fixed here:

1. **`create_worktree` is destroy-and-recreate, not create-only.** Real
   `git worktree add -b <branch> <path> <base>` fails outright if `<path>`
   already exists, and separately fails if `<branch>` already exists. The
   blueprint's plain `add -b` would therefore hard-fail forever on any
   retried/crashed `task_id` (crash recovery is exactly the scenario this
   method needs to survive). There is no partial progress in a worktree
   worth preserving, so the policy here is: prune stale worktree
   registrations, force-remove any existing worktree directory, force-delete
   any existing branch, then `git worktree add -B <branch> ...` (capital
   `-B` = create-or-reset) against a guaranteed-clean slate. All of the
   teardown steps are best-effort (`check=False`) since on a first-ever
   creation there is nothing to prune/remove/delete and that is expected,
   not an error.

2. **`stage_and_merge_task`'s commit step tolerates "nothing to commit."**
   `git commit` exits non-zero when the worktree has no staged changes
   (e.g. a retried task, or a task whose validator only re-runs checks
   without editing files). The blueprint's bare `subprocess.run(..., check=True)`
   would raise on that no-op case; here we check `git status --porcelain`
   first and only commit when there is something to commit, so a
   no-op retry can still proceed to rebase/re-validate/merge.

3. **Validator-callback failures are distinguished from validator-says-no.**
   `validator_callback` returning `False` is an expected, retry-eligible
   business outcome (tests failed) and yields `stage_and_merge_task() ->
   False`. If the callback itself *raises* (e.g. the sandbox runner crashed,
   Docker is unreachable), that is an infra fault, not a task-review
   verdict, and is re-raised wrapped in `ValidationCallbackError` so callers
   don't silently treat "the validator broke" the same as "the code failed
   its tests."

4. **Cleanup only happens on the two explicit terminal paths.** A rebase
   conflict or a failed re-validation leaves the worktree and branch alive
   (`stage_and_merge_task` returns `False` without cleaning up) so the task
   can be retried or inspected/HITL-escalated. `cleanup_worktree` only runs
   automatically at the end of a *successful* merge. `abandon_task` is the
   separate, explicit "permanently give up on this task" path (max retries
   exhausted, HITL-aborted, etc.) and simply calls `cleanup_worktree`.

5. **All git invocations use `asyncio.create_subprocess_exec`**, never
   blocking `subprocess.run` — this whole core is asyncio-based (see doc 02),
   and a blocking subprocess call anywhere would stall the event loop for
   every other concurrently-running task.

Note on step 4 of the merge (`git checkout main` + `git merge --ff-only`
directly in `repo_root`): this mutates the checked-out branch of
`repo_root` itself, which is fine for tests against a disposable throwaway
repo (all Phase 2 needs) but would be unsafe against a real developer's
live working copy — it would yank their checkout out from under them.
A better approach for production use would be
`git fetch <wt_path> <branch>:main` from `repo_root`, which fast-forwards
the `main` ref without touching whatever branch `repo_root` currently has
checked out. Left as a TODO, not implemented now, per the spec for this
task.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Awaitable, Callable, List, Tuple


class GitCommandError(RuntimeError):
    """A checked git subprocess invocation exited non-zero."""

    def __init__(self, args: List[str], returncode: int, stderr: str) -> None:
        self.args_list = args
        self.returncode = returncode
        self.stderr = stderr
        super().__init__(
            f"git {' '.join(args)} failed (exit {returncode}): {stderr.strip()}"
        )


class ValidationCallbackError(RuntimeError):
    """`validator_callback` raised instead of returning a pass/fail verdict.

    This signals an infrastructure fault (the sandbox/validator itself
    broke), as distinct from the validator running successfully and
    reporting that the task's code failed its checks.
    """

    def __init__(self, task_id: str) -> None:
        self.task_id = task_id
        super().__init__(
            f"validator_callback raised while validating task {task_id!r}"
        )


class GitStagingEngine:
    """Manages per-task Git worktree isolation and the merge-queue pipeline.

    One `GitStagingEngine` is bound to a single main repository
    (`repo_root`). Each task gets its own worktree under
    `.ai-os/worktrees/<task_id>` on branch `ai-os/<task_id>`, created off of
    `base_branch` (normally `main`). `stage_and_merge_task` commits whatever
    is in that worktree, rebases onto the base branch to pick up any prior
    task's merge, re-validates on the rebased tree, and — only on success —
    fast-forward-merges into the base branch and tears the worktree down.
    """

    def __init__(self, repo_root: Path) -> None:
        self.repo_root = Path(repo_root)
        self.worktrees_dir = self.repo_root / ".ai-os" / "worktrees"
        # Serializes merges into base_branch: only one task's commit/rebase/
        # merge sequence touches the shared base branch at a time.
        self._merge_lock = asyncio.Lock()
        # Serializes worktree create/destroy bookkeeping (prune/remove/add)
        # so concurrent create_worktree calls for different task_ids don't
        # race on `git worktree prune` or on the shared worktree registry.
        self._worktree_admin_lock = asyncio.Lock()

    async def _run_git(
        self, args: List[str], cwd: Path, check: bool = True
    ) -> Tuple[int, str, str]:
        proc = await asyncio.create_subprocess_exec(
            "git",
            *args,
            cwd=str(cwd),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        assert proc.returncode is not None
        if check and proc.returncode != 0:
            raise GitCommandError(args, proc.returncode, stderr.decode())
        return proc.returncode, stdout.decode(), stderr.decode()

    def _branch_name(self, task_id: str) -> str:
        return f"ai-os/{task_id}"

    def _worktree_path(self, task_id: str) -> Path:
        return self.worktrees_dir / task_id

    async def create_worktree(self, task_id: str, base_branch: str = "main") -> Path:
        """Create (or destroy-and-recreate) the worktree for `task_id`.

        Idempotent / crash-recovery-safe: calling this twice for the same
        `task_id` (e.g. after a crashed prior attempt) tears down any
        leftover worktree/branch first, so it always succeeds rather than
        failing on "path already exists" / "branch already exists".
        """
        branch = self._branch_name(task_id)
        wt_path = self._worktree_path(task_id)
        async with self._worktree_admin_lock:
            # Best-effort teardown of any stale state from a prior attempt.
            await self._run_git(["worktree", "prune"], self.repo_root, check=False)
            if wt_path.exists():
                await self._run_git(
                    ["worktree", "remove", "--force", str(wt_path)],
                    self.repo_root,
                    check=False,
                )
            await self._run_git(
                ["branch", "-D", branch], self.repo_root, check=False
            )
            # -B (capital): create-or-reset the branch, so a stale ref left
            # behind by the best-effort delete above can't block creation.
            await self._run_git(
                ["worktree", "add", "-B", branch, str(wt_path), base_branch],
                self.repo_root,
                check=True,
            )
        return wt_path

    async def stage_and_merge_task(
        self,
        task_id: str,
        commit_message: str,
        validator_callback: Callable[[Path], Awaitable[bool]],
    ) -> bool:
        """Commit, rebase onto main, re-validate, and fast-forward merge.

        Returns `True` only when the task's changes are now merged into
        `main` and the worktree has been cleaned up. Returns `False` for
        expected, retry-eligible business-outcome failures (rebase
        conflict, validator reports failure) — in both of those cases the
        worktree is left alive for inspection/retry/HITL. Raises
        `ValidationCallbackError` if `validator_callback` itself raises
        (an infra fault, not a task verdict) — nothing is merged in that
        case either.
        """
        wt_path = self._worktree_path(task_id)
        branch = self._branch_name(task_id)

        async with self._merge_lock:
            # 1. Commit whatever is in the worktree, tolerating "nothing to
            #    commit" (a no-op retry should still be able to proceed).
            await self._run_git(["add", "."], wt_path)
            _, status_out, _ = await self._run_git(
                ["status", "--porcelain"], wt_path, check=False
            )
            if status_out.strip():
                await self._run_git(["commit", "-m", commit_message], wt_path)

            # 2. Rebase onto main to pick up any prior task's merge.
            rc, _, _ = await self._run_git(
                ["rebase", "main"], wt_path, check=False
            )
            if rc != 0:
                await self._run_git(
                    ["rebase", "--abort"], wt_path, check=False
                )
                return False

            # 3. Re-validate on the rebased tree. Distinguish "validator
            #    says no" (expected -> False) from "validator itself
            #    broke" (infra fault -> raise).
            try:
                is_valid = await validator_callback(wt_path)
            except Exception as exc:
                raise ValidationCallbackError(task_id) from exc
            if not is_valid:
                return False

            # 4. Fast-forward-only merge into main. See module docstring
            #    for why this checks out main in repo_root directly (fine
            #    for a disposable test repo; TODO for production: `git
            #    fetch <wt_path> <branch>:main` instead, to avoid touching
            #    repo_root's checked-out branch).
            await self._run_git(["checkout", "main"], self.repo_root)
            await self._run_git(["merge", "--ff-only", branch], self.repo_root)

            # Cleanup only on this success path — a rebase/validation
            # failure above returns before reaching here, leaving the
            # worktree alive for retry/HITL inspection.
            await self.cleanup_worktree(task_id)
            return True

    async def cleanup_worktree(self, task_id: str) -> None:
        """Force-remove the worktree directory and delete its branch."""
        branch = self._branch_name(task_id)
        wt_path = self._worktree_path(task_id)
        await self._run_git(
            ["worktree", "remove", "--force", str(wt_path)],
            self.repo_root,
            check=False,
        )
        await self._run_git(["branch", "-D", branch], self.repo_root, check=False)

    async def abandon_task(self, task_id: str) -> None:
        """Explicit terminal-cleanup path for a task being permanently given
        up on (max retries exhausted, HITL-aborted, etc.) — the only other
        place besides a successful merge that should tear down the
        worktree.
        """
        await self.cleanup_worktree(task_id)
