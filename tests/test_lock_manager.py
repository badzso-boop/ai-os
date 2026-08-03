"""Real-concurrency tests for `ai_os.core.lock_manager.LockManager`.

No mocking of `asyncio.Condition` internals: every test below drives real
coroutines racing via `asyncio.gather`, and timing assertions are made on
`time.monotonic()` intervals recorded by the coroutines themselves. This
mirrors the project's "real behavior over mocks" testing philosophy
established in Phase 1 (real Tree-sitter parses, real Click CliRunner).

`asyncio_mode = "auto"` is configured in `pyproject.toml`, so plain
`async def test_...()` functions work without `@pytest.mark.asyncio` —
verified by running this file directly with `.venv/bin/pytest`.
"""
from __future__ import annotations

import asyncio
import time

import pytest

from ai_os.core.lock_manager import LockManager


def _intervals_overlap(a: tuple[float, float], b: tuple[float, float]) -> bool:
    a_start, a_end = a
    b_start, b_end = b
    return a_start < b_end and b_start < a_end


async def _record_locked_interval(
    lock_manager: LockManager,
    task_id: str,
    read_set: set[str],
    write_set: set[str],
    events: list[tuple[str, str, float]],
    hold_seconds: float = 0.05,
) -> tuple[float, float]:
    """Acquire locks, sleep, release; return the (start, end) interval held."""
    async with lock_manager.locks(task_id, read_set, write_set):
        start = time.monotonic()
        events.append((task_id, "start", start))
        await asyncio.sleep(hold_seconds)
        end = time.monotonic()
        events.append((task_id, "end", end))
    return start, end


async def test_disjoint_write_sets_do_not_serialize():
    """Two tasks writing disjoint files must run concurrently, not be globally serialized."""
    lm = LockManager()
    events: list[tuple[str, str, float]] = []

    results = await asyncio.wait_for(
        asyncio.gather(
            _record_locked_interval(lm, "task-a", set(), {"a.py"}, events),
            _record_locked_interval(lm, "task-b", set(), {"b.py"}, events),
        ),
        timeout=2,
    )

    assert _intervals_overlap(results[0], results[1])


async def test_same_write_set_serializes():
    """Two tasks writing the same file must never hold their locks concurrently."""
    lm = LockManager()
    events: list[tuple[str, str, float]] = []

    results = await asyncio.wait_for(
        asyncio.gather(
            _record_locked_interval(lm, "task-a", set(), {"shared.py"}, events),
            _record_locked_interval(lm, "task-b", set(), {"shared.py"}, events),
        ),
        timeout=2,
    )

    assert not _intervals_overlap(results[0], results[1])


async def test_writer_and_reader_are_mutually_exclusive():
    """A writer and a reader of the same file must never overlap, in either order."""
    lm = LockManager()
    events: list[tuple[str, str, float]] = []

    results = await asyncio.wait_for(
        asyncio.gather(
            _record_locked_interval(lm, "writer", set(), {"f.py"}, events),
            _record_locked_interval(lm, "reader", {"f.py"}, set(), events),
        ),
        timeout=2,
    )

    assert not _intervals_overlap(results[0], results[1])


async def test_concurrent_readers_do_not_conflict():
    """Multiple tasks holding a shared read lock on the same file may overlap freely."""
    lm = LockManager()
    events: list[tuple[str, str, float]] = []

    results = await asyncio.wait_for(
        asyncio.gather(
            _record_locked_interval(lm, "reader-a", {"f.py"}, set(), events),
            _record_locked_interval(lm, "reader-b", {"f.py"}, set(), events),
            _record_locked_interval(lm, "reader-c", {"f.py"}, set(), events),
        ),
        timeout=2,
    )

    assert _intervals_overlap(results[0], results[1])
    assert _intervals_overlap(results[1], results[2])


async def test_reentrant_write_lock_does_not_deadlock():
    """A task_id re-acquiring a write lock it already holds must not conflict with itself.

    This is the concrete bug fix vs. doc 02's naive count-based blueprint,
    which tracked `Set[str]` writers with no ownership: a second
    `acquire_locks` call from the *same* task_id for a file it already
    write-locks would have seen `f in self._write_locks` as true and
    blocked forever, since only its own release could ever clear it.
    """
    lm = LockManager()

    await asyncio.wait_for(
        lm.acquire_locks("task-a", set(), {"f.py"}), timeout=1
    )
    # Re-entrant call: task-a already owns the write lock on f.py.
    await asyncio.wait_for(
        lm.acquire_locks("task-a", set(), {"f.py"}), timeout=1
    )

    assert lm._writer["f.py"] == "task-a"

    await lm.release_locks("task-a", set(), {"f.py"})


async def test_release_locks_is_ownership_scoped():
    """release_locks must be a no-op for locks the caller doesn't own.

    Task A holds a write lock on f.py. Task B (which never acquired it)
    calls release_locks for f.py — this must not release A's lock. We
    verify by having a third task attempt a conflicting acquire, which
    must still block (proven via timeout) since A still holds the lock.
    """
    lm = LockManager()

    await asyncio.wait_for(lm.acquire_locks("task-a", set(), {"f.py"}), timeout=1)

    # task-b never held this lock; releasing it must be a silent no-op.
    await lm.release_locks("task-b", set(), {"f.py"})

    assert lm._writer.get("f.py") == "task-a"

    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(lm.acquire_locks("task-c", set(), {"f.py"}), timeout=0.2)

    # Cleanup: release the still-held lock so the event loop has nothing pending.
    await lm.release_locks("task-a", set(), {"f.py"})


async def test_locks_context_manager_releases_on_exception():
    """`locks()` must release its locks even when the guarded block raises."""
    lm = LockManager()

    class _Boom(Exception):
        pass

    with pytest.raises(_Boom):
        async with lm.locks("task-a", set(), {"f.py"}):
            raise _Boom("simulated task failure")

    # If release didn't happen, this would hang; wait_for turns that into a
    # loud test failure instead of blocking the whole suite.
    await asyncio.wait_for(lm.acquire_locks("task-b", set(), {"f.py"}), timeout=1)
    await lm.release_locks("task-b", set(), {"f.py"})


@pytest.mark.xfail(
    reason=(
        "Known MVP limitation, by design: LockManager has no writer-priority/FIFO "
        "fairness (doc 02 §3 accepts asyncio.Condition's notify_all with no ordering "
        "guarantee). Under a sustained stream of overlapping readers, a waiting "
        "writer can be starved indefinitely. This test documents that limitation "
        "rather than fixing it — a real fix would need a ticket/queue-based waiter "
        "policy, out of scope for this module."
    ),
    strict=True,
)
async def test_writer_can_be_starved_by_overlapping_readers():
    lm = LockManager()
    stop = asyncio.Event()
    writer_acquired = asyncio.Event()

    async def relentless_reader(reader_id: str) -> None:
        # Keep re-acquiring a fresh overlapping read lock so there's never a
        # moment with zero active readers for the writer to slip into.
        while not stop.is_set():
            async with lm.locks(reader_id, {"f.py"}, set()):
                await asyncio.sleep(0.01)

    async def writer() -> None:
        await lm.acquire_locks("writer", set(), {"f.py"})
        writer_acquired.set()
        await lm.release_locks("writer", set(), {"f.py"})

    readers = [asyncio.create_task(relentless_reader(f"reader-{i}")) for i in range(4)]
    writer_task = asyncio.create_task(writer())

    try:
        # A fair/FIFO implementation would let the writer in promptly. The
        # naive notify_all-based implementation lets readers keep cutting
        # in, so this should time out, i.e. the writer is starved.
        await asyncio.wait_for(writer_acquired.wait(), timeout=1)
    finally:
        stop.set()
        for r in readers:
            r.cancel()
        writer_task.cancel()
        await asyncio.gather(*readers, writer_task, return_exceptions=True)
