"""ConsoleEventSink reproduces the exact CLI output the raw on_event/
on_status_change callbacks used to print directly - a regression test that the
EpicJob rewire (issue #36) doesn't change `epic run`'s user-visible output."""
from __future__ import annotations

from rich.console import Console

from ai_os.core.job import JobEvent
from ai_os.core.job_sinks import ConsoleEventSink, render_task_event


def _record(sink_verbose: bool = False) -> tuple[ConsoleEventSink, Console]:
    console = Console(record=True, width=120)
    return ConsoleEventSink(console, verbose=sink_verbose), console


def test_status_change_running_is_suppressed():
    sink, console = _record()
    sink.emit(JobEvent(job_id="j", kind="task_status", payload={
        "type": "status_change", "task_id": "T-1", "status": "RUNNING",
    }))
    assert console.export_text().strip() == ""


def test_status_change_non_running_prints_dim_line():
    sink, console = _record()
    sink.emit(JobEvent(job_id="j", kind="task_status", payload={
        "type": "status_change", "task_id": "T-1", "status": "COMPLETED",
    }))
    assert "T-1: COMPLETED" in console.export_text()


def test_raw_on_event_dict_is_rendered_via_render_task_event():
    sink, console = _record()
    sink.emit(JobEvent(job_id="j", kind="task_status", payload={
        "type": "attempt", "task_id": "T-2", "attempt": 1, "max_attempts": 2,
        "title": "Add auth", "target_files": ["a.py"],
    }))
    out = console.export_text()
    assert "T-2" in out
    assert "attempt 1/2" in out
    assert "Add auth" in out


def test_non_task_status_kinds_are_silent():
    sink, console = _record()
    sink.emit(JobEvent(job_id="j", kind="completed", payload={"completed": ["T-1"]}))
    sink.emit(JobEvent(job_id="j", kind="failed", payload={"error": "boom"}))
    sink.emit(JobEvent(job_id="j", kind="awaiting_approval", payload={"approved": True}))
    assert console.export_text().strip() == ""


def test_render_task_event_matches_direct_console_call_for_validation_success():
    console_a = Console(record=True, width=120)
    console_b = Console(record=True, width=120)
    ev = {"type": "validation", "task_id": "T-3", "success": True, "exit_code": 0}

    render_task_event(ev, console_a)
    console_b.print(f"  [green]✓ T-3 sandbox passed[/green] (exit 0)")

    assert console_a.export_text() == console_b.export_text()
