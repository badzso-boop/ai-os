"""`EventSink` implementations for `ai_os.core.job.EpicJob` (doc 23 §2).

`ConsoleEventSink` is today's only implementation: it reproduces, verbatim,
the Rich-console rendering `ai_os.cli._make_event_printer` used to do directly
inside `epic run`'s `on_event`/`on_status_change` callbacks — so wiring the
CLI through `EpicJob` doesn't change a single printed character. A
`PersistedEventLogSink` (writing every event to the `job_events` DB table) is
an explicit follow-up (doc 23 §2/§3, "PR C") and is NOT built here.
"""
from __future__ import annotations

from rich.console import Console
from rich.panel import Panel

from ai_os.core.job import EventSink, JobEvent


def render_task_event(ev: dict, console: Console, verbose: bool = False) -> None:
    """Render one raw `EpicRunner`/`TaskRunner` `on_event` dict to `console`.
    This is `ai_os.cli._make_event_printer`'s inner `printer(ev)` body, moved
    here unchanged so both the CLI's `epic resume` path (still on the raw
    `on_event` callback) and `ConsoleEventSink` (via `EpicJob`) share exactly
    the same rendering — no drift between the two callers.
    """
    t = ev.get("type")
    tid = ev.get("task_id", "?")
    if t == "task_execution":
        status = ev.get("status", "")
        message = ev.get("message", "")
        cost_val = ev.get("cost")
        elapsed_val = ev.get("elapsed")
        cost_str = f"${cost_val:.4f}" if isinstance(cost_val, (int, float)) else str(cost_val or "")
        elapsed_str = f"{elapsed_val:.2f}s" if isinstance(elapsed_val, (int, float)) else str(elapsed_val or "")

        lines = [
            f"Status: {status}",
            f"Message: {message}",
            f"Elapsed: {elapsed_str}",
            f"Cost: {cost_str}",
        ]
        for k, v in ev.items():
            if k not in {"type", "task_id", "status", "message", "cost", "elapsed"}:
                lines.append(f"{k}: {v}")

        console.print(Panel("\n".join(lines), title=f"Task [{tid}] - {t.upper()}", expand=False))
    elif t == "attempt":
        files = ", ".join(ev.get("target_files") or []) or "(inferred)"
        console.print(
            f"[cyan]▶ {tid}[/cyan] attempt {ev['attempt']}/{ev['max_attempts']} — "
            f"{ev.get('title', '')}  [dim]→ {files}[/dim]"
        )
    elif t == "agent_turn":
        usd = ev.get("usd") or 0.0
        cost = f" · ${usd:.4f}" if usd else ""
        console.print(
            f"  [dim]{tid} · agent: {ev.get('provider')}→{ev.get('model')} · "
            f"{ev.get('input_tokens', 0)}in/{ev.get('output_tokens', 0)}out tok{cost}[/dim]"
        )
    elif t == "validation":
        if ev.get("success"):
            console.print(f"  [green]✓ {tid} sandbox passed[/green] (exit {ev.get('exit_code')})")
        else:
            console.print(
                f"  [red]✗ {tid} sandbox FAILED[/red] (exit {ev.get('exit_code')}) — "
                f"{ev.get('summary', '')}"
            )
            out = (ev.get("output") or "").strip()
            if out:
                tail = out if verbose else "\n".join(out.splitlines()[-12:])
                console.print(Panel(
                    tail, title=f"{tid} sandbox output{'' if verbose else ' (tail)'}",
                    border_style="red", expand=False,
                ))
    elif t == "merge_conflict":
        console.print(f"  [yellow]⚠ {tid} merge conflict[/yellow] — {ev.get('output', '')}")
    elif t == "agent_error":
        console.print(f"  [red]⚠ {tid} agent error[/red] — {ev.get('error', '')}")
    elif t == "retry":
        console.print(f"  [yellow]↻ {tid} retrying (attempt {ev.get('next_attempt')})[/yellow]")
    elif t == "triage_analysis":
        console.print(f"  [cyan]🩺 {tid} triage agent analyzing failure (triage attempt {ev.get('triage_attempt')}/{ev.get('max_triage_retries')})...[/cyan]")
    elif t == "triage_recommendation":
        console.print(f"  [cyan]💡 {tid} triage recommendation:[/cyan]")
        rec = (ev.get("recommendation") or "").strip()
        if rec:
            console.print(Panel(rec, title=f"{tid} triage fix recommendation", border_style="cyan", expand=False))
    elif t == "merged":
        if ev.get("triage_healed"):
            console.print(f"  [bold green]✓ {tid} merged (self-healed via triage)[/bold green]")
        else:
            console.print(f"  [green]✓ {tid} merged[/green]")
    elif t == "test_quality":
        if ev.get("missing_tests"):
            console.print(f"  [yellow]⚠ {tid} no test added for a code change[/yellow]")
        if ev.get("sensitive_files"):
            console.print(
                f"  [yellow]🔐 {tid} touches CI/sensitive config[/yellow] — "
                f"{', '.join(ev['sensitive_files'])} [dim](not self-certifying — review the diff)[/dim]"
            )
    elif t == "test_critique":
        verdict = ev.get("verdict", "UNKNOWN")
        color = {"STRONG": "green", "WEAK": "yellow", "MISSING": "red"}.get(verdict, "yellow")
        console.print(f"  [{color}]🔎 {tid} test critic: {verdict}[/{color}]")
        if verdict not in {"STRONG"} and verbose:
            console.print(Panel(
                (ev.get("critique") or "").strip(), title=f"{tid} test critique",
                border_style=color, expand=False,
            ))


class ConsoleEventSink(EventSink):
    """Reproduces today's exact `epic run` CLI output. `EpicJob` fans two of
    its own synthetic event shapes at this sink beyond the raw `on_event`
    dicts:

    - `kind="task_status"` with `payload["type"] == "status_change"` — the old
      `on_status_change(task_id, status)` callback, rendered exactly as
      before (`RUNNING` suppressed, everything else printed dim).
    - `kind="task_status"` with any other payload — the raw `on_event` dict,
      rendered via `render_task_event` (unchanged from `_make_event_printer`).

    `kind="awaiting_approval"`/`"completed"`/`"failed"` are intentionally
    silent here: the CLI already prints its own "Aborted"/"Epic finished"
    messages around `EpicJob`, so echoing those here would duplicate output
    and risk drifting from the exact existing strings.
    """

    def __init__(self, console: Console, verbose: bool = False) -> None:
        self._console = console
        self._verbose = verbose

    def emit(self, event: JobEvent) -> None:
        if event.kind != "task_status":
            return
        payload = event.payload
        if payload.get("type") == "status_change":
            status = payload.get("status")
            if status != "RUNNING":
                self._console.print(f"[dim]{payload.get('task_id')}: {status}[/dim]")
            return
        render_task_event(payload, self._console, self._verbose)
