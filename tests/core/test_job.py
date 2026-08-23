"""Tests for `ai_os.core.job.EpicJob` (doc 23 §2, issue #36): fans events out
to N sinks, `cancel()` actually stops the underlying async run, and
`approve_plan()` resolves the HITL gate without touching stdin when given an
explicit decision. Uses a fake runner (duck-types `EpicRunner`'s
`run_epic`/`on_event`/`on_status_change` surface) — no real LLM/git/Docker,
matching this repo's testing philosophy.
"""
from __future__ import annotations

import asyncio

import pytest

from ai_os.core.job import EpicJob, EventSink, JobEvent, JobState


class _FakeEpicRunResult:
    def __init__(self):
        self.completed = ["T-1"]
        self.blocked = []
        self.skipped = []
        self.epic_id = "epic-123"
        self.pull_request_url = None


class _FakeRunner:
    """Duck-types the slice of `EpicRunner` that `EpicJob` touches."""

    def __init__(self, *, delay: float = 0.0, raise_exc: Exception | None = None):
        self.on_event = None
        self.on_status_change = None
        self._delay = delay
        self._raise_exc = raise_exc
        self.run_epic_calls = 0
        self.resume_epic_calls = 0

    async def run_epic(self, tasks, epic_title="epic", raw_prompt=""):
        self.run_epic_calls += 1
        if self._delay:
            await asyncio.sleep(self._delay)
        if self._raise_exc is not None:
            raise self._raise_exc
        # Exercise both hooks, exactly like the real EpicRunner does mid-run.
        if self.on_event is not None:
            self.on_event({"type": "attempt", "task_id": "T-1", "attempt": 1, "max_attempts": 1})
        if self.on_status_change is not None:
            self.on_status_change("T-1", "RUNNING")
            self.on_status_change("T-1", "COMPLETED")
        return _FakeEpicRunResult()

    async def resume_epic(self, epic_id):
        self.resume_epic_calls += 1
        return _FakeEpicRunResult()


class _RecordingSink:
    """A fake `EventSink` that just captures every event it sees, in order."""

    def __init__(self):
        self.events: list[JobEvent] = []

    def emit(self, event: JobEvent) -> None:
        self.events.append(event)


def _job(**kwargs) -> tuple[EpicJob, _FakeRunner]:
    runner = _FakeRunner(**kwargs)
    job = EpicJob(job_id="job-1", tasks=["fake-task-node"], epic_title="t", raw_prompt="p")
    job.bind_runner(runner)
    return job, runner


# -- fan-out to multiple sinks -----------------------------------------------


@pytest.mark.asyncio
async def test_fans_events_out_to_multiple_sinks_in_order():
    job, runner = _job()
    sink_a = _RecordingSink()
    sink_b = _RecordingSink()
    job.register_sink(sink_a)
    job.register_sink(sink_b)

    result = await job.start()

    assert result.completed == ["T-1"]
    assert runner.run_epic_calls == 1

    # Both sinks saw the same events, in the same order.
    assert [e.kind for e in sink_a.events] == [e.kind for e in sink_b.events]
    kinds = [e.kind for e in sink_a.events]
    assert "task_status" in kinds
    assert kinds[-1] == "completed"

    # The synthetic status_change events and the raw on_event dict both arrive
    # as "task_status" JobEvents, carrying their original payload through.
    payloads = [e.payload for e in sink_a.events if e.kind == "task_status"]
    assert any(p.get("type") == "attempt" for p in payloads)
    assert any(p.get("type") == "status_change" and p.get("status") == "COMPLETED" for p in payloads)

    completed_event = sink_a.events[-1]
    assert completed_event.job_id == "job-1"
    assert completed_event.payload["completed"] == ["T-1"]
    assert completed_event.payload["epic_id"] == "epic-123"


@pytest.mark.asyncio
async def test_a_sink_registered_after_start_only_sees_later_events():
    job, runner = _job(delay=0.05)
    early_sink = _RecordingSink()
    job.register_sink(early_sink)
    task = job.start()

    late_sink = _RecordingSink()
    job.register_sink(late_sink)
    await task

    # The early sink saw everything; the late sink missed the mid-run events
    # (registered after they'd already fired) but still got "completed".
    assert any(e.kind == "task_status" for e in early_sink.events)
    assert any(e.kind == "completed" for e in late_sink.events)


# -- cancel() actually stops the run -----------------------------------------


@pytest.mark.asyncio
async def test_cancel_stops_the_underlying_run():
    job, runner = _job(delay=10.0)  # long enough it would never finish in a test
    task = job.start()

    await asyncio.sleep(0)  # let the task actually start running
    cancelled = job.cancel()
    assert cancelled is True

    with pytest.raises(asyncio.CancelledError):
        await task

    assert job.status().state == JobState.CANCELLED


def test_cancel_before_start_is_a_noop():
    job, runner = _job()
    assert job.cancel() is False


@pytest.mark.asyncio
async def test_cancel_after_completion_is_a_noop():
    job, runner = _job()
    await job.start()
    assert job.cancel() is False


# -- status() reflects failure too -------------------------------------------


@pytest.mark.asyncio
async def test_status_reflects_a_failed_run():
    job, runner = _job(raise_exc=RuntimeError("boom"))
    sink = _RecordingSink()
    job.register_sink(sink)

    with pytest.raises(RuntimeError):
        await job.start()

    status = job.status()
    assert status.state == JobState.FAILED
    assert "boom" in (status.error or "")
    assert any(e.kind == "failed" for e in sink.events)


# -- approve_plan() ------------------------------------------------------


def test_approve_plan_with_explicit_decision_never_touches_stdin():
    job, runner = _job()
    sink = _RecordingSink()
    job.register_sink(sink)

    assert job.approve_plan(True) is True
    assert job.status().state == JobState.PENDING  # approval alone doesn't start the job
    assert sink.events[-1].kind == "awaiting_approval"
    assert sink.events[-1].payload == {"approved": True}


def test_approve_plan_is_idempotent():
    job, runner = _job()
    assert job.approve_plan(False) is False
    # A second call doesn't re-resolve or flip the decision, even with a
    # different argument.
    assert job.approve_plan(True) is False


@pytest.mark.asyncio
async def test_start_raises_if_plan_was_declined():
    job, runner = _job()
    job.approve_plan(False)
    with pytest.raises(RuntimeError):
        job.start()


def test_start_raises_without_a_bound_runner():
    job = EpicJob(job_id="job-2", tasks=[])
    with pytest.raises(RuntimeError):
        job.start()


# -- resume path --------------------------------------------------------


@pytest.mark.asyncio
async def test_start_calls_resume_epic_when_resume_epic_id_is_set():
    runner = _FakeRunner()
    job = EpicJob(job_id="job-3", tasks=[], resume_epic_id="epic-999")
    job.bind_runner(runner)

    await job.start()

    assert runner.resume_epic_calls == 1
    assert runner.run_epic_calls == 0
