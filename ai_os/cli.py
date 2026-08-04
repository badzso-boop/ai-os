"""AI-OS CLI: project registry, one-shot Polyglot Analyzer scans (Phase 1),
manual MCP provider adapter testing (Phase 3a), running one task end-to-end
through the sandboxed agent loop (Phase 3b), and decomposing a high-level
request into a multi-task DAG distributed across models (Phase 4a)."""
from __future__ import annotations

import asyncio
import tempfile
import time
from pathlib import Path

import click
from rich.console import Console
from rich.table import Table

from ai_os import registry
from ai_os.analyzer.call_graph_builder import CallGraphBuilder
from ai_os.analyzer.languages import LANGUAGES
from ai_os.core.epic_planner import EpicPlanError, decompose
from ai_os.core.epic_runner import EpicRunner
from ai_os.core.lock_manager import LockManager
from ai_os.core.models import TaskNode
from ai_os.core.persistence import Persistence, default_db_url
from ai_os.core.scheduler import DynamicScheduler
from ai_os.core.scheduling_policy import SchedulingPolicy
from ai_os.core.staging import GitStagingEngine
from ai_os.core.task_runner import TaskRunner, build_claude_cli_agent_turn_executor
from ai_os.knowledge.graph_engine import KnowledgeEngine
from ai_os.knowledge.watcher import ProjectWatcher
from ai_os.mcp.adapters.base_adapter import LLMTaskRequest
from ai_os.mcp.config import load_configured_adapters
from ai_os.mcp.protocol_router import ProtocolRouter, risk_provider_order_from_env
from ai_os.sandbox.container_runner import EphemeralSandboxRunner

console = Console()

_LANGUAGE_CHOICES = sorted(["python", "javascript", "typescript", "java"])


@click.group()
def main() -> None:
    """AI-OS — deterministic Polyglot Analyzer & Knowledge Graph (Phase 1)."""


@main.group()
def project() -> None:
    """Manage the registry of scannable project roots (~/.ai-os/projects.json)."""


@project.command("add")
@click.argument("name")
@click.argument("path")
@click.option("--force", is_flag=True, help="Overwrite an existing registration with this name.")
def project_add(name: str, path: str, force: bool) -> None:
    try:
        entry = registry.add(name, path, force=force)
    except (registry.InvalidProjectNameError, registry.ProjectPathError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc
    console.print(f"[green]Registered[/green] {entry.name} -> {entry.path}")


@project.command("remove")
@click.argument("name")
def project_remove(name: str) -> None:
    try:
        registry.remove(name)
    except registry.ProjectNotFoundError as exc:
        raise click.ClickException(f"No such project: {exc}") from exc
    console.print(f"[green]Removed[/green] {name}")


@project.command("list")
def project_list() -> None:
    entries = registry.list_projects()
    if not entries:
        console.print("No projects registered. Use [bold]ai-os project add <name> <path>[/bold].")
        return
    table = Table(title="Registered projects")
    table.add_column("Name")
    table.add_column("Path")
    table.add_column("Exists")
    table.add_column("Added at")
    for entry in entries:
        table.add_row(entry.name, entry.path, "yes" if entry.exists else "[red]no[/red]", entry.added_at)
    console.print(table)


@main.command("scan")
@click.argument("name_or_path")
@click.option("--out", "out_path", type=click.Path(dir_okay=False), help="Write the full graph as JSON.")
@click.option("--max-hops", default=2, show_default=True, help="k-hop radius used for --skeleton debug lookups.")
@click.option("--languages", default=None, help=f"Comma-separated subset of: {', '.join(LANGUAGES)}.")
@click.option("--exclude", "extra_excluded", multiple=True, help="Extra directory names to skip (repeatable).")
@click.option("--skeleton", "skeleton_fqn", default=None, help="Print the skeleton stub for one FQN and exit.")
@click.option("--json", "as_json", is_flag=True, help="Emit the summary as JSON instead of a table.")
def scan(
    name_or_path: str,
    out_path: str | None,
    max_hops: int,
    languages: str | None,
    extra_excluded: tuple[str, ...],
    skeleton_fqn: str | None,
    as_json: bool,
) -> None:
    try:
        root = registry.resolve(name_or_path)
    except registry.ProjectPathError as exc:
        raise click.ClickException(str(exc)) from exc

    language_filter = None
    if languages:
        language_filter = {lang.strip() for lang in languages.split(",") if lang.strip()}
        unknown = language_filter - set(LANGUAGES)
        if unknown:
            raise click.ClickException(f"Unknown language(s): {', '.join(sorted(unknown))}")

    started = time.monotonic()
    builder = CallGraphBuilder()
    result = builder.scan(root, languages=language_filter, extra_excluded_dirs=extra_excluded)
    elapsed = time.monotonic() - started

    engine = KnowledgeEngine()
    engine.build_from_scan(result)

    if skeleton_fqn is not None:
        node = engine.graph.nodes.get(skeleton_fqn)
        if node is None or not node.get("stub"):
            raise click.ClickException(f"No skeleton stub found for {skeleton_fqn!r}.")
        console.print(node["stub"])
        return

    if out_path is not None:
        engine.to_json(Path(out_path))

    summary = _build_summary(name_or_path, root, result, engine, elapsed, out_path)

    if as_json:
        console.print_json(data=summary)
    else:
        _print_summary_table(summary)


@main.command("watch")
@click.argument("name_or_path")
@click.option("--out", "out_path", type=click.Path(dir_okay=False), help="Re-write the full graph as JSON on every change.")
@click.option("--interval", default=1.0, show_default=True, help="Seconds between polls.")
@click.option("--languages", default=None, help=f"Comma-separated subset of: {', '.join(LANGUAGES)}.")
@click.option("--exclude", "extra_excluded", multiple=True, help="Extra directory names to skip (repeatable).")
def watch(
    name_or_path: str,
    out_path: str | None,
    interval: float,
    languages: str | None,
    extra_excluded: tuple[str, ...],
) -> None:
    """Watch NAME_OR_PATH and keep its Knowledge Graph fresh: on any add/modify/
    delete of a source file, re-scan the project (the same full scan `ai-os scan`
    runs) and, with --out, re-write the graph JSON. Runs until Ctrl-C.

    Zero-LLM, zero-network — this is the deterministic analyzer kept live.
    """
    try:
        root = registry.resolve(name_or_path)
    except registry.ProjectPathError as exc:
        raise click.ClickException(str(exc)) from exc

    language_filter = None
    if languages:
        language_filter = {lang.strip() for lang in languages.split(",") if lang.strip()}
        unknown = language_filter - set(LANGUAGES)
        if unknown:
            raise click.ClickException(f"Unknown language(s): {', '.join(sorted(unknown))}")

    watcher = ProjectWatcher(root, languages=language_filter, extra_excluded_dirs=extra_excluded)

    def _write_out(engine) -> None:
        if out_path is not None:
            engine.to_json(Path(out_path))

    started = time.monotonic()
    engine = watcher.start()
    _write_out(engine)
    stats = engine.stats()
    console.print(
        f"[bold]Watching[/bold] {root} (every {interval}s). Initial scan in "
        f"{time.monotonic() - started:.2f}s: {stats.get('nodes', '?')} nodes, "
        f"{stats.get('edges', '?')} edges."
        + (f" Graph -> {out_path}." if out_path else "")
    )
    console.print("[dim]Press Ctrl-C to stop.[/dim]")

    def _on_change(engine, event) -> None:
        _write_out(engine)
        s = engine.stats()
        changed = ", ".join(
            filter(None, [
                f"+{len(event.added)}" if event.added else "",
                f"~{len(event.modified)}" if event.modified else "",
                f"-{len(event.removed)}" if event.removed else "",
            ])
        )
        stamp = time.strftime("%H:%M:%S")
        console.print(
            f"[dim]{stamp}[/dim] re-scanned ({changed} files) -> "
            f"{s.get('nodes', '?')} nodes, {s.get('edges', '?')} edges"
            + (f", graph updated" if out_path else "")
        )

    try:
        watcher.run(interval=interval, on_change=_on_change)
    except KeyboardInterrupt:
        console.print("\nStopped watching.")


def _build_summary(name_or_path, root, result, engine, elapsed, out_path) -> dict:
    symbol_kinds: dict[str, int] = {}
    for file_result in result.files:
        for symbol in file_result.symbols:
            symbol_kinds[symbol.kind] = symbol_kinds.get(symbol.kind, 0) + 1

    unresolved_imports = sum(1 for e in result.import_edges if not e.resolved)
    external_imports = sum(1 for e in result.import_edges if not e.resolved and e.external)
    genuinely_unresolved_imports = unresolved_imports - external_imports
    ambiguous_calls = sum(1 for e in result.call_edges if e.ambiguous)

    return {
        "project": name_or_path,
        "root": str(root),
        "elapsed_seconds": round(elapsed, 3),
        "files_by_language": dict(sorted(result.file_count_by_language.items())),
        "files_total": sum(result.file_count_by_language.values()),
        "files_other": result.other_file_count,
        "symbols": symbol_kinds,
        "import_edges_total": len(result.import_edges),
        "import_edges_unresolved": unresolved_imports,
        "import_edges_external": external_imports,
        "import_edges_genuinely_unresolved": genuinely_unresolved_imports,
        "call_edges_total": len(result.call_edges),
        "call_edges_ambiguous": ambiguous_calls,
        "extends_edges_total": len(result.extends_edges),
        "graph": engine.stats(),
        "graph_written_to": out_path,
    }


def _print_summary_table(summary: dict) -> None:
    console.print("[bold]AI-OS Polyglot Analyzer[/bold] — scan report")
    console.print(f"Project: {summary['project']}  ({summary['root']})")
    console.print(f"Scanned in {summary['elapsed_seconds']}s\n")

    files_table = Table(title="Files by language")
    files_table.add_column("Language")
    files_table.add_column("Count", justify="right")
    for lang, count in summary["files_by_language"].items():
        files_table.add_row(lang, str(count))
    files_table.add_row("other", str(summary["files_other"]))
    files_table.add_row("[bold]total[/bold]", f"[bold]{summary['files_total'] + summary['files_other']}[/bold]")
    console.print(files_table)

    symbols_table = Table(title="Symbols extracted")
    symbols_table.add_column("Kind")
    symbols_table.add_column("Count", justify="right")
    for kind, count in summary["symbols"].items():
        symbols_table.add_row(kind, str(count))
    console.print(symbols_table)

    edges_table = Table(title="Graph edges")
    edges_table.add_column("Kind")
    edges_table.add_column("Count", justify="right")
    for kind, count in summary["graph"]["edges_by_kind"].items():
        edges_table.add_row(kind, str(count))
    console.print(edges_table)
    console.print(
        f"IMPORTS unresolved: {summary['import_edges_unresolved']}/{summary['import_edges_total']}"
        f" (external deps: {summary['import_edges_external']},"
        f" genuinely unresolved: {summary['import_edges_genuinely_unresolved']})   "
        f"CALLS ambiguous: {summary['call_edges_ambiguous']}/{summary['call_edges_total']}"
    )
    console.print(
        f"\nKnowledge graph: {summary['graph']['nodes']} nodes / {summary['graph']['edges']} edges"
    )
    if summary["graph_written_to"]:
        console.print(f"Written to {summary['graph_written_to']}")


@main.group()
def llm() -> None:
    """Manually exercise a configured MCP provider adapter (Phase 3a).

    This makes REAL calls using whatever credentials are configured via
    `.env`/the environment (see `.env.example`) — it consumes real usage or
    quota. It is a hands-on verification tool, deliberately not part of the
    automated pytest suite (which never makes real network/subprocess calls
    to a provider).
    """


@llm.command("test")
@click.argument("provider")
@click.option("--prompt", required=True, help="User prompt / context payload to send.")
@click.option("--system", default="", help="Optional system prompt.")
@click.option("--model", default=None, help="Override the provider's default model.")
def llm_test(provider: str, prompt: str, system: str, model: str | None) -> None:
    """Send a real request to PROVIDER (anthropic|gemini|openrouter) and print the response."""
    adapters = load_configured_adapters()
    if provider not in adapters:
        raise click.ClickException(
            f"Provider {provider!r} is not configured. Configured: {sorted(adapters) or 'none'}. "
            "Copy .env.example to .env and fill in credentials for this provider."
        )
    request = LLMTaskRequest(
        task_id="cli-llm-test", system_prompt=system, context_payload=prompt, model=model
    )
    response = asyncio.run(adapters[provider].execute_task(request))

    console.print(f"[bold]Provider:[/bold] {response.provider}  [bold]Model:[/bold] {response.model_name}")
    console.print(response.generated_text)
    console.print(
        f"\n[dim]tokens in={response.usage.input_tokens} out={response.usage.output_tokens} "
        f"est. cost=${response.usage.estimated_usd_cost:.4f}[/dim]"
    )


@llm.command("list")
def llm_list() -> None:
    """List which providers are currently configured (credentials present)."""
    adapters = load_configured_adapters()
    if not adapters:
        console.print("No providers configured. Copy .env.example to .env and fill in credentials.")
        return
    table = Table(title="Configured LLM providers")
    table.add_column("Provider")
    for name in sorted(adapters):
        table.add_row(name)
    console.print(table)


@main.group()
def task() -> None:
    """Run one task end-to-end through the sandboxed agent loop (Phase 3b)."""


@task.command("run")
@click.argument("name_or_path")
@click.option("--task-id", required=True, help="Unique task id, e.g. TASK-101.")
@click.option("--title", required=True)
@click.option("--description", required=True, help="What the agent should do.")
@click.option(
    "--target-files",
    required=True,
    help="Comma-separated files this task reads context around AND is expected to write.",
)
@click.option("--language", required=True, type=click.Choice(sorted(["python", "javascript", "typescript", "java"])))
@click.option("--risk-level", default="MEDIUM", type=click.Choice(["LOW", "MEDIUM", "HIGH", "CRITICAL"]))
@click.option("--max-retries", default=2, show_default=True)
@click.option("--model", default="claude-sonnet-4-5", show_default=True)
def task_run(
    name_or_path: str,
    task_id: str,
    title: str,
    description: str,
    target_files: str,
    language: str,
    risk_level: str,
    max_retries: int,
    model: str,
) -> None:
    """Run TASK-ID against NAME_OR_PATH's real repo, through the real Git
    worktree/lock/sandbox pipeline, using a REAL `claude` CLI agent turn
    (via --mcp-config) that can call propose_file_patch/fetch_symbol_definition/
    trigger_sandbox_validation. This makes real, non-trivial usage/quota calls —
    it is the manual end-to-end verification step, not part of the test suite.
    """
    try:
        root = registry.resolve(name_or_path)
    except registry.ProjectPathError as exc:
        raise click.ClickException(str(exc)) from exc

    files = [f.strip() for f in target_files.split(",") if f.strip()]
    task_node = TaskNode(
        id=task_id,
        title=title,
        description=description,
        risk_level=risk_level,
        target_files=files,
        write_set=set(files),
        max_retries=max_retries,
    )

    console.print(f"Scanning {root} to build the Context Cache...")
    scan_result = CallGraphBuilder().scan(root)
    engine = KnowledgeEngine()
    engine.build_from_scan(scan_result)

    with tempfile.TemporaryDirectory(prefix="ai-os-task-graph-") as tmp_dir:
        graph_json_path = Path(tmp_dir) / "graph.json"
        engine.to_json(graph_json_path)

        agent_turn_executor = build_claude_cli_agent_turn_executor(
            repo_root=root,
            graph_json_path=graph_json_path,
            sandbox_language=language,
            model=model,
        )
        runner = TaskRunner(
            lock_manager=LockManager(),
            staging=GitStagingEngine(root),
            knowledge_engine=engine,
            agent_turn_executor=agent_turn_executor,
            sandbox_runner=EphemeralSandboxRunner(),
            on_status_change=lambda tid, status: console.print(f"[dim]{tid}: {status}[/dim]"),
        )
        result = asyncio.run(runner.run_task(task_node, language=language))

    color = "green" if result.status == "COMPLETED" else "red"
    console.print(f"\n[bold {color}]{result.status}[/bold {color}] after {result.attempts} attempt(s).")
    if result.final_output:
        console.print("\nLast validation output:")
        console.print(result.final_output)


@main.group()
def epic() -> None:
    """Decompose a high-level request into a multi-task DAG and run it,
    distributing tasks across models by risk level (Phase 4a)."""


def _print_plan_table(tasks, assignments) -> None:
    table = Table(title="Proposed DAG plan")
    table.add_column("ID")
    table.add_column("Risk")
    table.add_column("Provider→Model")
    table.add_column("Depends on")
    table.add_column("Writes")
    table.add_column("Title")
    for t in tasks:
        a = assignments[t.id]
        model = a.model or "(provider default)"
        table.add_row(
            t.id, t.risk_level, f"{a.provider} → {model}",
            ", ".join(t.dependencies) or "-", ", ".join(sorted(t.write_set)) or "-", t.title,
        )
    console.print(table)


@epic.command("run")
@click.argument("name_or_path")
@click.option("--prompt", required=True, help="High-level request to decompose, e.g. 'add JWT auth'.")
@click.option("--language", required=True, type=click.Choice(_LANGUAGE_CHOICES))
@click.option("--yes", is_flag=True, help="Skip the plan-review approval gate and run immediately.")
def epic_run(name_or_path: str, prompt: str, language: str, yes: bool) -> None:
    """Decompose PROMPT against NAME_OR_PATH into a task DAG, show the plan for
    approval (HITL Stage 1), then execute it — routing each task to a model by
    its risk level via the providers configured in .env. Makes REAL LLM calls
    (planning + each task) and consumes real usage/quota.
    """
    try:
        root = registry.resolve(name_or_path)
    except registry.ProjectPathError as exc:
        raise click.ClickException(str(exc)) from exc

    adapters = load_configured_adapters()
    if not adapters:
        raise click.ClickException(
            "No LLM providers configured. Copy .env.example to .env and add credentials."
        )
    router = ProtocolRouter(adapters, risk_provider_order=risk_provider_order_from_env())
    scheduler = DynamicScheduler(router)

    console.print(f"Scanning {root} to ground the planner...")
    scan_result = CallGraphBuilder().scan(root)
    engine = KnowledgeEngine()
    engine.build_from_scan(scan_result)

    plan_assignment = scheduler.planning_assignment()
    console.print(
        f"Decomposing the request with [bold]{plan_assignment.provider}[/bold] "
        f"({plan_assignment.model or 'provider default'})... this is a real LLM call."
    )
    try:
        tasks = asyncio.run(
            decompose(prompt, engine, adapters[plan_assignment.provider], model=plan_assignment.model)
        )
    except EpicPlanError as exc:
        raise click.ClickException(f"Planning failed: {exc}") from exc

    # Plan table + HITL Stage 1 gate use a persistence-free runner (pure
    # scheduler routing) so the DB (and its event loop) is only touched inside
    # the single async run below — opening the aiosqlite engine in one loop and
    # using it in another would bind it to the wrong loop.
    prelim = EpicRunner(
        repo_root=root, scheduler=scheduler, adapters=adapters, language=language,
    )
    assignments = prelim.plan_assignments(tasks)
    _print_plan_table(tasks, assignments)

    # HITL Stage 1: Plan Review gate (doc 12 §2.1). The React UI version is
    # Phase 4b; the approval gate itself works fine in a terminal.
    if not yes and not click.confirm("\nApprove this plan and execute the DAG?", default=False):
        console.print("Aborted — no tasks were run.")
        return

    async def _execute():
        # Accounting (Stage 3): persist epic + per-task rows + token/lock audit
        # so `ai-os cost` / `ai-os epic history` can read back real spend.
        persistence, engine = await Persistence.open(default_db_url())
        runner = EpicRunner(
            repo_root=root, scheduler=scheduler, adapters=adapters, language=language,
            sandbox_runner=EphemeralSandboxRunner(),
            on_status_change=lambda tid, status: console.print(f"[dim]{tid}: {status}[/dim]"),
            persistence=persistence,
            # Stage 4: rate-limit backoff + provider fallback always on; the
            # optional cost cap comes from AI_OS_EPIC_BUDGET_USD in the env.
            scheduling_policy=SchedulingPolicy.from_env(),
        )
        try:
            return await runner.run_epic(tasks, epic_title=prompt[:120], raw_prompt=prompt)
        finally:
            await engine.dispose()

    result = asyncio.run(_execute())

    console.print("\n[bold]Epic finished.[/bold]")
    console.print(f"  [green]Completed[/green]: {', '.join(result.completed) or '-'}")
    console.print(f"  [red]Blocked[/red]:   {', '.join(result.blocked) or '-'}")
    console.print(f"  [yellow]Skipped[/yellow]:   {', '.join(result.skipped) or '-'}")
    if result.epic_id:
        console.print(f"\n[dim]Accounting saved. See:[/dim] ai-os cost --epic {result.epic_id}")


@epic.command("history")
def epic_history() -> None:
    """List past epics with their status, task counts, and total spend
    (reads the accounting DB written by `ai-os epic run`)."""

    async def _load():
        persistence, engine = await Persistence.open(default_db_url())
        try:
            return await persistence.epic_summaries()
        finally:
            await engine.dispose()

    summaries = asyncio.run(_load())
    if not summaries:
        console.print("No epics recorded yet. Run `ai-os epic run ...` first.")
        return

    table = Table(title="Epic history")
    table.add_column("Epic ID")
    table.add_column("Title")
    table.add_column("Status")
    table.add_column("Tasks (done/total)")
    table.add_column("Tokens (in/out)")
    table.add_column("USD", justify="right")
    for s in summaries:
        table.add_row(
            s.id[:12], (s.title or "")[:40], s.status,
            f"{s.completed_tasks}/{s.total_tasks}",
            f"{s.input_tokens}/{s.output_tokens}", f"${s.total_usd:.4f}",
        )
    console.print(table)


@epic.command("resume")
@click.argument("name_or_path")
@click.option("--epic", "epic_id", required=True, help="The epic id to resume (see `ai-os epic history`).")
@click.option("--language", required=True, type=click.Choice(_LANGUAGE_CHOICES))
def epic_resume(name_or_path: str, epic_id: str, language: str) -> None:
    """Resume a crashed/interrupted epic: re-run only the tasks that weren't
    already COMPLETED (their merged work is kept), respecting the DAG. The task
    statuses come from the accounting DB; you supply the project + language
    again (they aren't stored). Makes REAL LLM calls for the remaining tasks.
    """
    try:
        root = registry.resolve(name_or_path)
    except registry.ProjectPathError as exc:
        raise click.ClickException(str(exc)) from exc

    adapters = load_configured_adapters()
    if not adapters:
        raise click.ClickException(
            "No LLM providers configured. Copy .env.example to .env and add credentials."
        )
    router = ProtocolRouter(adapters, risk_provider_order=risk_provider_order_from_env())
    scheduler = DynamicScheduler(router)

    async def _resume():
        persistence, engine = await Persistence.open(default_db_url())
        runner = EpicRunner(
            repo_root=root, scheduler=scheduler, adapters=adapters, language=language,
            sandbox_runner=EphemeralSandboxRunner(),
            on_status_change=lambda tid, status: console.print(f"[dim]{tid}: {status}[/dim]"),
            persistence=persistence,
            scheduling_policy=SchedulingPolicy.from_env(),
        )
        try:
            return await runner.resume_epic(epic_id)
        finally:
            await engine.dispose()

    try:
        result = asyncio.run(_resume())
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc

    console.print("\n[bold]Epic resume finished.[/bold]")
    console.print(f"  [green]Completed[/green]: {', '.join(result.completed) or '-'}")
    console.print(f"  [red]Blocked[/red]:   {', '.join(result.blocked) or '-'}")
    console.print(f"  [yellow]Skipped[/yellow]:   {', '.join(result.skipped) or '-'}")


@main.command("cost")
@click.option("--epic", "epic_id", default=None, help="Scope the breakdown to one epic id (default: all epics).")
def cost(epic_id: str | None) -> None:
    """Show token/USD spend grouped by provider+model (all epics, or one via
    --epic), from the accounting DB."""

    async def _load():
        persistence, engine = await Persistence.open(default_db_url())
        try:
            rows = await persistence.provider_breakdown(epic_id=epic_id)
            total = await persistence.epic_total_usd(epic_id) if epic_id else None
            return rows, total
        finally:
            await engine.dispose()

    rows, epic_total = asyncio.run(_load())
    if not rows:
        scope = f"epic {epic_id}" if epic_id else "any epic"
        console.print(f"No spend recorded for {scope} yet.")
        return

    title = f"Spend by provider — epic {epic_id[:12]}" if epic_id else "Spend by provider (all epics)"
    table = Table(title=title)
    table.add_column("Provider")
    table.add_column("Model")
    table.add_column("Calls", justify="right")
    table.add_column("Tokens (in/out)")
    table.add_column("USD", justify="right")
    grand = 0.0
    for r in rows:
        grand += r.total_usd
        table.add_row(
            r.provider, r.model_name, str(r.calls),
            f"{r.input_tokens}/{r.output_tokens}", f"${r.total_usd:.4f}",
        )
    console.print(table)
    console.print(f"[bold]Total:[/bold] ${grand:.4f}")


if __name__ == "__main__":
    main()
