"""A per-project run lock so two concurrent `ai-os epic run`/`resume` invocations
don't clobber each other's shared git working tree (the staging engine does
`git checkout` in the repo root — two runs at once would corrupt it).

Uses `fcntl.flock` on a lock file under `<AI_OS_HOME>/locks/`, keyed by a hash of
the repo path. flock is released automatically when the process exits (even on a
crash / SIGKILL), so there's no stale-lock problem to clean up.
"""
from __future__ import annotations

import fcntl
import hashlib
import os
from contextlib import contextmanager
from pathlib import Path


class RunLockError(RuntimeError):
    """Another epic run already holds the lock for this project."""


def _lock_path(repo_root: Path) -> Path:
    home = os.environ.get("AI_OS_HOME")
    base = (Path(home) if home else Path.home() / ".ai-os") / "locks"
    base.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(str(Path(repo_root).resolve()).encode()).hexdigest()[:16]
    return base / f"{digest}.lock"


def acquire_epic_run_lock(repo_root: Path) -> int:
    """Acquire the lock and return the held file descriptor. The lock is held
    until the fd is closed OR the process exits (flock auto-releases on both),
    so a one-shot CLI command can just acquire and let process exit release it —
    no explicit release needed. Raises `RunLockError` if already held.
    """
    path = _lock_path(repo_root)
    fd = os.open(str(path), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        os.close(fd)
        raise RunLockError(
            f"Another `ai-os epic` run is already in progress for {Path(repo_root).resolve()} "
            "(they share the repo's git working tree). Wait for it to finish, or run on a "
            "separate clone."
        ) from exc
    os.ftruncate(fd, 0)
    os.write(fd, f"pid={os.getpid()}\n".encode())
    return fd


@contextmanager
def epic_run_lock(repo_root: Path):
    """Hold an exclusive lock for `repo_root` for the duration of the block.
    Raises `RunLockError` immediately if another run holds it (non-blocking)."""
    path = _lock_path(repo_root)
    fd = os.open(str(path), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise RunLockError(
                f"Another `ai-os epic` run is already in progress for {Path(repo_root).resolve()} "
                "(they share the repo's git working tree). Wait for it to finish, or run on a "
                "separate clone."
            ) from exc
        os.ftruncate(fd, 0)
        os.write(fd, f"pid={os.getpid()}\n".encode())
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)
