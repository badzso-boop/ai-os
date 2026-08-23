"""Transport-agnostic Job/Event model over one `EpicRunner` execution (doc 23
§2). Today's only caller is the CLI (`ai-os epic run`), but the whole point of
this module is that a future caller wouldn't be: `EpicJob` doesn't print
anything, read stdin, or otherwise assume a terminal — it fans structured
`JobEvent`s out to any number of registered `EventSink`s (a Rich console today,
a persisted event log / WebSocket broadcaster later — see doc 23 §2's
`PersistedEventLogSink`, explicitly out of scope for this PR) and exposes
`start()`/`cancel()`/`approve_plan()`/`status()` as the only surface a caller
needs, whether that caller is a CLI command blocking on stdin or a future HTTP
handler.

Deliberately NOT changed by this module: `EpicRunner`/`TaskRunner` internals.
`EpicJob` reuses their existing `on_event`/`on_status_change` callback hooks
(Phase 4a/6) — it just fans them out to N sinks instead of the CLI printing
directly, and wraps the whole `run_epic`/`resume_epic` coroutine in a
cancellable `asyncio.Task`.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any, Literal, Optional, Protocol

if TYPE_CHECKING:
    from ai_os.core.epic_runner import EpicRunner, EpicRunResult


JobEventKind = Literal[
    "task_status", "plan_ready", "awaiting_approval", "log_line", "completed", "failed"
]


@dataclass(frozen=True)
class JobEvent:
    """One observable thing that happened during a job's run. `payload` is a
    plain dict (not a typed schema) on purpose — it's a direct pass-through of
    whatever `EpicRunner`/`TaskRunner` already emit via `on_event`, so this
    layer doesn't need to know the shape of every event kind those runners
    produce; a sink decides what to do with it."""

    job_id: str
    kind: JobEventKind
    payload: dict[str, Any]


class EventSink(Protocol):
    """Anything that wants to observe a job's events. `ConsoleEventSink`
    (`job_sinks.py`) is the only implementation today; a `PersistedEventLogSink`
    and a WebSocket sink are follow-ups (doc 23 §2/§3, PR C)."""

    def emit(self, event: JobEvent) -> None: ...


class JobState(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class JobStatus:
    job_id: str
    state: JobState
    result: Optional["EpicRunResult"] = None
    error: Optional[str] = None


class EpicJob:
    """Long-running handle over one `EpicRunner` execution (either a fresh
    `run_epic` or a `resume_epic`). Owns its own `asyncio.Task`, can be
    cancelled, and fans events out to every registered `EventSink`.

    The `EpicRunner` itself is bound via `bind_runner()` rather than the
    constructor because building one requires an already-open `Persistence`
    (async), while a job's id/tasks/approval state are known — and useful —
    before that. This mirrors the CLI's existing two-phase shape: decompose +
    plan-review happen outside any `asyncio.run`, then a single `asyncio.run`
    opens the DB, builds the runner, and executes.
    """

    def __init__(
        self,
        job_id: str,
        tasks: list,
        epic_title: str = "epic",
        raw_prompt: str = "",
        resume_epic_id: Optional[str] = None,
    ) -> None:
        self.job_id = job_id
        self._tasks = tasks
        self._epic_title = epic_title
        self._raw_prompt = raw_prompt
        self._resume_epic_id = resume_epic_id

        self._sinks: list[EventSink] = []
        self._runner: Optional["EpicRunner"] = None
        self._task: Optional["asyncio.Task[EpicRunResult]"] = None
        self._state = JobState.PENDING
        self._result: Optional["EpicRunResult"] = None
        self._error: Optional[str] = None
        # None = not yet resolved; True/False = the HITL decision, once made.
        self._approval: Optional[bool] = None

    # -- sinks -----------------------------------------------------------

    def register_sink(self, sink: EventSink) -> None:
        """Add a sink. Order doesn't matter — every registered sink sees every
        event emitted after it's registered."""
        self._sinks.append(sink)

    def emit(self, event: JobEvent) -> None:
        for sink in self._sinks:
            sink.emit(event)

    # -- wiring ------------------------------------------------------------

    def bind_runner(self, runner: "EpicRunner") -> None:
        """Attach the `EpicRunner` this job will execute, and take over its
        `on_event`/`on_status_change` hooks so their output is fanned to this
        job's sinks instead of going straight to a console. Must be called
        before `start()`."""
        self._runner = runner
        runner.on_event = self._on_runner_event
        runner.on_status_change = self._on_runner_status_change

    def _on_runner_event(self, ev: dict) -> None:
        self.emit(JobEvent(job_id=self.job_id, kind="task_status", payload=ev))

    def _on_runner_status_change(self, task_id: str, status: str) -> None:
        self.emit(
            JobEvent(
                job_id=self.job_id,
                kind="task_status",
                payload={"type": "status_change", "task_id": task_id, "status": status},
            )
        )

    # -- HITL plan-review gate ---------------------------------------------

    def approve_plan(self, decision: Optional[bool] = None) -> bool:
        """Resolve the plan-review HITL gate (doc 12 §2.1) exactly once.

        Called with no argument (today's only caller, the CLI), it blocks
        synchronously on stdin via `click.confirm` — byte-identical UX to the
        `click.confirm(...)` it replaces. A future non-interactive caller (an
        API endpoint backing `POST /jobs/{id}/approve`) would instead pass
        `decision` directly, never touching a terminal — same job object, two
        callers, per doc 23 §2.

        Idempotent: once a decision is recorded, later calls just return it
        without re-prompting.
        """
        if self._approval is not None:
            return self._approval
        if decision is None:
            import click  # local import: keep this module importable without

            decision = click.confirm("\nApprove this plan and execute the DAG?", default=False)
        self._approval = decision
        self.emit(
            JobEvent(job_id=self.job_id, kind="awaiting_approval", payload={"approved": decision})
        )
        return decision

    # -- lifecycle -----------------------------------------------------------

    def start(self) -> "asyncio.Task[EpicRunResult]":
        """Start the underlying `EpicRunner` run as a cancellable asyncio
        task. Must be called from within a running event loop (the CLI does
        this inside its single `asyncio.run(_execute())`). Raises if the plan
        was explicitly declined (`approve_plan()` returned False) or if the
        runner hasn't been bound yet."""
        if self._runner is None:
            raise RuntimeError("EpicJob.start() called before bind_runner()")
        if self._approval is False:
            raise RuntimeError("EpicJob.start() called but the plan was not approved")
        if self._task is not None:
            raise RuntimeError("EpicJob.start() already called")

        self._state = JobState.RUNNING
        self._task = asyncio.ensure_future(self._run())
        return self._task

    async def _run(self) -> "EpicRunResult":
        assert self._runner is not None
        try:
            if self._resume_epic_id:
                result = await self._runner.resume_epic(self._resume_epic_id)
            else:
                result = await self._runner.run_epic(
                    self._tasks, epic_title=self._epic_title, raw_prompt=self._raw_prompt
                )
        except asyncio.CancelledError:
            self._state = JobState.CANCELLED
            self.emit(JobEvent(job_id=self.job_id, kind="failed", payload={"reason": "cancelled"}))
            raise
        except Exception as exc:  # noqa: BLE001 - surfaced via JobStatus.error, not swallowed
            self._state = JobState.FAILED
            self._error = str(exc)
            self.emit(JobEvent(job_id=self.job_id, kind="failed", payload={"error": str(exc)}))
            raise

        self._result = result
        self._state = JobState.COMPLETED
        self.emit(
            JobEvent(
                job_id=self.job_id,
                kind="completed",
                payload={
                    "completed": result.completed,
                    "blocked": result.blocked,
                    "skipped": result.skipped,
                    "epic_id": result.epic_id,
                    "pull_request_url": result.pull_request_url,
                },
            )
        )
        return result

    def cancel(self) -> bool:
        """Cancel the underlying run if it's in flight. Returns True if a
        cancellation was actually requested (task existed and wasn't already
        done), False otherwise (never started, or already finished)."""
        if self._task is None or self._task.done():
            return False
        return self._task.cancel()

    async def wait(self) -> "EpicRunResult":
        """Await the job's result. Raises whatever `_run()` raised (including
        `asyncio.CancelledError` after a `cancel()`)."""
        if self._task is None:
            raise RuntimeError("EpicJob.wait() called before start()")
        return await self._task

    def status(self) -> JobStatus:
        return JobStatus(
            job_id=self.job_id, state=self._state, result=self._result, error=self._error
        )
