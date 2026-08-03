"""Async, ownership-tracked read/write file-lock manager (doc 02 §3).

Deliberate deviation from the illustrative blueprint in
`docs/02_ORCHESTRATOR_CORE.md` §3.1: that snippet only tracks *counts*
(`Dict[str, int]` readers / `Set[str]` writers), never *ownership* (which
`task_id` holds a given lock). Two concrete bugs follow from that and are
fixed here:

1. Without ownership, a task_id that already holds a write lock on a file
   and calls `acquire_locks` again for that same file (e.g. a retry, or a
   task that legitimately re-enters its own critical section) would see its
   own lock as a conflict and block forever against itself.
2. `release_locks` had no way to verify the caller actually owns the lock
   it's naming, so any task_id could release a lock it never acquired.

This module tracks ownership explicitly: `_readers[filepath]` is the *set*
of task_ids currently holding a shared read lock, and `_writer[filepath]` is
the single task_id (if any) holding the exclusive write lock. All conflict
checks compare against `task_id`, not just presence/absence of a lock.

Known, accepted MVP limitation (not a bug to fix here): `asyncio.Condition`
wakes waiters via `notify_all()` with no FIFO ordering guarantee, so a
writer waiting behind a steady stream of overlapping readers can be
starved indefinitely. See `tests/test_lock_manager.py` for an explicit,
documented `xfail` demonstrating this. Doc 02's `while True: ... await
wait()` pattern itself *is* correct for mutual exclusion (the condition's
underlying lock is released while parked and reacquired before the
predicate is re-checked) — only the ownership tracking was wrong.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import AsyncIterator, Dict, Set


class LockManager:
    """Granular, ownership-aware file lock manager.

    Multiple tasks may hold a shared read lock on the same file
    concurrently. A write lock is exclusive: it excludes every other
    writer and every other reader of that file. A task_id never conflicts
    with locks it already owns itself (re-entrant acquisition is safe).
    """

    def __init__(self) -> None:
        self._readers: Dict[str, Set[str]] = {}
        self._writer: Dict[str, str] = {}
        self._cond = asyncio.Condition()

    def _has_conflict(self, task_id: str, read_set: Set[str], write_set: Set[str]) -> bool:
        for f in write_set:
            writer = self._writer.get(f)
            if writer is not None and writer != task_id:
                return True
            readers = self._readers.get(f)
            if readers and (readers - {task_id}):
                return True
        for f in read_set:
            writer = self._writer.get(f)
            if writer is not None and writer != task_id:
                return True
        return False

    async def acquire_locks(self, task_id: str, read_set: Set[str], write_set: Set[str]) -> None:
        """Block until every lock in `read_set`/`write_set` is granted to `task_id`.

        A task_id that already owns a requested lock (write-on-write
        re-entrancy, or reading a file it already holds the write lock
        on / already reads) never conflicts with itself.
        """
        async with self._cond:
            while self._has_conflict(task_id, read_set, write_set):
                await self._cond.wait()

            for f in read_set:
                self._readers.setdefault(f, set()).add(task_id)
            for f in write_set:
                self._writer[f] = task_id

    async def release_locks(self, task_id: str, read_set: Set[str], write_set: Set[str]) -> None:
        """Release only the locks in `read_set`/`write_set` actually owned by `task_id`.

        Releasing a lock `task_id` does not own is a no-op, not an error —
        callers may release a superset of what they hold after a partial
        acquisition failure.
        """
        async with self._cond:
            for f in read_set:
                readers = self._readers.get(f)
                if readers is not None and task_id in readers:
                    readers.discard(task_id)
                    if not readers:
                        del self._readers[f]
            for f in write_set:
                if self._writer.get(f) == task_id:
                    del self._writer[f]
            self._cond.notify_all()

    @asynccontextmanager
    async def locks(
        self, task_id: str, read_set: Set[str], write_set: Set[str]
    ) -> AsyncIterator[None]:
        """Primary public API: acquire on enter, guaranteed release on exit.

        Prefer this over the raw `acquire_locks`/`release_locks` pair
        everywhere except in tests that need to inspect intermediate lock
        state, since it releases even when the guarded block raises.
        """
        await self.acquire_locks(task_id, read_set, write_set)
        try:
            yield
        finally:
            await self.release_locks(task_id, read_set, write_set)
