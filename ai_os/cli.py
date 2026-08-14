"""AI-OS CLI: project registry, one-shot Polyglot Analyzer scans (Phase 1),
manual MCP provider adapter testing (Phase 3a), running one task end-to-end
through the sandboxed agent loop (Phase 3b), and decomposing a high-level
request into a multi-task DAG distributed across models (Phase 4a)."""
from __future__ import annotations

import asyncio
import subprocess
import tempfile
import time
from pathlib import Path

import click
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from ai_os import registry
from ai_os.analyzer.call_graph_builder import CallGraphBuilder
from ai_os.analyzer.languages import LANGUAGES
from ai_os.core.epic_planner import EpicPlanError, decompose
from ai_os.core.epic_runner import EpicRunner
from ai_os.core.lock_manager import LockManager
from ai_os.core.conventions import load_project_conventions
from ai_os.core.cost_estimator import estimate_epic
from ai_os.core.models import TaskNode
from ai_os.core.scaffold import DB_CAPABLE, PRESETS, write_scaffold
from ai_os.core.startup.brief import parse_startup_brief
from ai_os.core.startup.generator import generate_startup
from ai_os.core.persistence import Persistence, default_db_url
from ai_os.core.run_lock import RunLockError, acquire_epic_run_lock
from ai_os.core.sensitive_files import sensitive_paths
from ai_os.core.scheduler import DynamicScheduler
from ai_os.core.scheduling_policy import SchedulingPolicy
from ai_os.core.staging import GitStagingEngine
from ai_os.core.task_runner import (
    TaskRunner,
    build_claude_cli_agent_turn_executor,
    build_output_summarizer,
    build_test_critic,
)
from ai_os.knowledge.graph_engine import KnowledgeEngine
from ai_os.knowledge.watcher import ProjectWatcher
from ai_os.mcp.adapters.base_adapter import LLMTaskRequest
from ai_os.mcp.config import load_configured_adapters
from ai_os.mcp.protocol_router import ProtocolRouter, risk_provider_order_from_env
from ai_os.sandbox.container_runner import EphemeralSandboxRunner
from ai_os.core.wizard import run_wizard
from ai_os.core.onboarding import scan_and_generate_configs

console = Console()

_LANGUAGE_CHOICES = sorted(["python", "javascript", "typescript", "java"])


@click.group()
def main() -> None:
    """AI-OS — deterministic Polyglot Analyzer & Knowledge Graph (Phase 1)."""


@main.command("wizard")
def wizard() -> None:
    """Run the interactive post-install setup and diagnostic wizard."""
    run_wizard()


@main.command("clean")
@click.argument("name_or_path", required=False)
@click.option("--branches", is_flag=True, help="Also delete leftover ai-os/* branches (DISCARDS blocked/unmerged work) — needs a project path.")
@click.option("--dry-run", is_flag=True, help="Show what would be removed, remove nothing.")
@click.option("--yes", is_flag=True, help="Skip the confirmation prompt.")
def clean(name_or_path: str | None, branches: bool, dry_run: bool, yes: bool) -> None:
    """Reclaim disk/state AI-OS left behind — especially after a crash.

    Removes AI-OS's own Docker artifacts (per-task dependency/copy images, leaked
    sandbox containers, DB sidecar networks) — only objects matching AI-OS's
    naming, never anything else. With a project path it also prunes stale git
    worktrees; add --branches to delete leftover ai-os/* branches too. Run this
    when no `ai-os epic` is active (it doesn't take the run lock).
    """
    from ai_os.core import cleaner

    art = cleaner.list_docker_artifacts()
    repo = None
    stale_branches: list[str] = []
    if name_or_path:
        try:
            repo = registry.resolve(name_or_path)
        except registry.ProjectPathError as exc:
            raise click.ClickException(str(exc)) from exc
        stale_branches = cleaner.list_ai_os_branches(repo)

    console.print("[bold]AI-OS cleanup[/bold]")
    console.print(f"  Docker images:     {len(art.images)}")
    for x in art.images:
        console.print(f"    [dim]{x}[/dim]")
    console.print(f"  Docker containers: {len(art.containers)}")
    for x in art.containers:
        console.print(f"    [dim]{x}[/dim]")
    console.print(f"  Docker networks:   {len(art.networks)}")
    for x in art.networks:
        console.print(f"    [dim]{x}[/dim]")
    if repo is not None:
        console.print(f"  Git worktrees:     prune stale registrations in {repo}")
        if branches:
            console.print(f"  [yellow]ai-os/* branches:  {len(stale_branches)} (will be DELETED — discards blocked/unmerged work)[/yellow]")
            for b in stale_branches:
                console.print(f"    [yellow]{b}[/yellow]")

    nothing = art.is_empty() and repo is None
    if nothing:
        console.print("\nNothing to clean. ✨")
        return
    if dry_run:
        console.print("\n[dim]--dry-run: nothing removed.[/dim]")
        return
    if not yes and not click.confirm("\nRemove the above?", default=False):
        console.print("Aborted — nothing removed.")
        return

    removed = cleaner.remove_docker_artifacts(art)
    if repo is not None:
        cleaner.prune_worktrees(repo)
        removed.append(f"pruned stale worktrees in {repo}")
        if branches and stale_branches:
            deleted = cleaner.delete_branches(repo, stale_branches)
            removed += [f"branch {b}" for b in deleted]

    console.print(f"\n[green]Removed {len(removed)} item(s).[/green]")
    for item in removed:
        console.print(f"  [dim]{item}[/dim]")


@main.command("init")
@click.argument("path", type=click.Path(file_okay=False))
@click.option("--stack", required=True, type=click.Choice(PRESETS), help="Project template to scaffold.")
@click.option("--with-db", "with_db", is_flag=True, help="Add a Postgres sidecar + migration/seed + a DB-backed test (fastapi/fastapi-react/spring/next-prisma).")
@click.option("--name", default=None, help="Registry name to register the project under (default: the dir name).")
def init(path: str, stack: str, with_db: bool, name: str | None) -> None:
    """Scaffold a NEW project from zero at PATH (a working baseline + tests +
    `.ai-os/sandbox.json`), make it a git repo with an initial `main` commit, and
    register it — so `ai-os epic run` can build on it immediately.

    Deterministic: templates are written directly (no network / host toolchain).
    For the `fastapi-react` monorepo, run epics per language (`--language python`
    for the backend, `--language typescript` for the frontend). `--with-db` wires
    a throwaway Postgres sidecar (on a `--internal` network) with a migration +
    reference-data seed + a DB-backed test.
    """
    root = Path(path).resolve()
    if with_db and stack not in DB_CAPABLE:
        raise click.ClickException(
            f"--with-db is not supported for --stack {stack}. Supported: {', '.join(DB_CAPABLE)}"
        )
    try:
        written = write_scaffold(root, stack, with_db=with_db)
    except FileExistsError as exc:
        raise click.ClickException(str(exc)) from exc

    # Make it a git repo with a `main` commit (AI-OS branches worktrees off main).
    def _git(*args: str) -> None:
        result = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True)
        if result.returncode != 0:
            raise click.ClickException(f"git {' '.join(args)} failed: {result.stderr.strip()}")

    if not (root / ".git").exists():
        _git("init", "-b", "main")
    _git("add", ".")
    # Use the machine's configured identity, but fall back to a local one when
    # none is set, so the initial commit never fails on a fresh box.
    status = subprocess.run(["git", "status", "--porcelain"], cwd=root, capture_output=True, text=True)
    if status.stdout.strip():
        identity: list[str] = []
        who = subprocess.run(["git", "config", "user.email"], cwd=root, capture_output=True, text=True)
        if not who.stdout.strip():
            identity = ["-c", "user.email=ai-os@localhost", "-c", "user.name=AI-OS"]
        commit = subprocess.run(
            ["git", *identity, "commit", "-m", f"Scaffold {stack} project (ai-os init)"],
            cwd=root, capture_output=True, text=True,
        )
        if commit.returncode != 0:
            raise click.ClickException(f"git commit failed: {commit.stderr.strip()}")

    reg_name = name or root.name
    registry.add(reg_name, str(root), force=True)

    console.print(f"[green]Scaffolded[/green] a [bold]{stack}[/bold] project at {root}")
    for rel in written:
        console.print(f"  [dim]created[/dim] {rel}")
    console.print(f"\nRegistered as [bold]{reg_name}[/bold]. Next:")
    if stack == "fastapi-react":
        console.print(f"  ai-os epic run {reg_name} --prompt \"add a /users CRUD API with tests\" --language python")
        console.print(f"  ai-os epic run {reg_name} --prompt \"add a users list page\" --language typescript")
    else:
        lang = {
            "fastapi": "python",
            "react": "typescript",
            "spring": "java",
            "next-prisma": "typescript",
            "startup": "javascript",
        }[stack]
        console.print(f"  ai-os epic run {reg_name} --prompt \"...\" --language {lang}")


@main.group()
def project() -> None:
    """Manage the registry of scannable project roots (~/.ai-os/projects.json)."""


@project.command("add")
@click.argument("name")
@click.argument("path")
@click.option("--force", is_flag=True, help="Overwrite an existing registration with this name.")
@click.option("--deep-scan", "deep_scan", is_flag=True, default=None, help="Perform deep scan of codebase stubs and AST.")
def project_add(name: str, path: str, force: bool, deep_scan: bool | None) -> None:
    try:
        entry = registry.add(name, path, force=force)
    except (registry.InvalidProjectNameError, registry.ProjectPathError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc

    if deep_scan is None:
        try:
            deep_scan = click.confirm("Perform deep scan of codebase?", default=False)
        except click.Abort:
            deep_scan = False

    scan_and_generate_configs(entry.path, use_deep_scan=deep_scan)
    console.print(f"[green]Registered[/green] {entry.name} -> {entry.path}")
    click.echo(f"Project '{entry.name}' added at {Path(entry.path).resolve()}.")


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


def _print_plan_table(tasks, assignments, estimate=None) -> None:
    est_by_id = {e.task_id: e for e in estimate.per_task} if estimate is not None else {}
    table = Table(title="Proposed DAG plan")
    table.add_column("ID")
    table.add_column("Risk")
    table.add_column("Provider→Model")
    table.add_column("Depends on")
    table.add_column("Writes")
    table.add_column("Title")
    if estimate is not None:
        table.add_column("~tok (in/out)")
        table.add_column("~$", justify="right")
    for t in tasks:
        a = assignments[t.id]
        model = a.model or "(provider default)"
        row = [
            t.id, t.risk_level, f"{a.provider} → {model}",
            ", ".join(t.dependencies) or "-", ", ".join(sorted(t.write_set)) or "-", t.title,
        ]
        if estimate is not None:
            e = est_by_id.get(t.id)
            if e is None:
                row += ["-", "-"]
            else:
                row += [
                    f"{e.input_tokens // 1000}K/{e.output_tokens // 1000}K",
                    "sub" if e.usd is None else f"${e.usd:.3f}",
                ]
        table.add_row(*row)
    console.print(table)

    if estimate is not None:
        total_k = (estimate.total_input_tokens + estimate.total_output_tokens) / 1000
        console.print(
            f"[bold]Estimated usage[/bold] [dim](rough — real usage varies ±2-3× with retries/tool loops)[/dim]: "
            f"~{estimate.total_input_tokens // 1000}K in + {estimate.total_output_tokens // 1000}K out "
            f"(~{total_k:.0f}K total)"
        )
        notes = []
        if estimate.total_usd > 0:
            notes.append(f"~${estimate.total_usd:.2f} on metered providers")
        if estimate.has_subscription:
            notes.append("Anthropic session tasks = subscription (included usage, no $)")
        if estimate.has_unpriced:
            notes.append("some models unpriced")
        if notes:
            console.print("  " + "  ·  ".join(notes))


def _make_event_printer(verbose: bool = False, console: Console | None = None):
    """Return an `on_event` callback that renders the structured run events
    (attempt, agent turn, sandbox validation, retry, merge) so the run is
    debuggable instead of a black box. `verbose` dumps the full sandbox output
    on failure; otherwise just the tail."""

    def printer(ev: dict) -> None:
        con = console if console is not None else globals()["console"]
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

            con.print(Panel("\n".join(lines), title=f"Task [{tid}] - {t.upper()}", expand=False))
        elif t == "attempt":
            files = ", ".join(ev.get("target_files") or []) or "(inferred)"
            con.print(
                f"[cyan]▶ {tid}[/cyan] attempt {ev['attempt']}/{ev['max_attempts']} — "
                f"{ev.get('title', '')}  [dim]→ {files}[/dim]"
            )
        elif t == "agent_turn":
            usd = ev.get("usd") or 0.0
            cost = f" · ${usd:.4f}" if usd else ""
            con.print(
                f"  [dim]{tid} · agent: {ev.get('provider')}→{ev.get('model')} · "
                f"{ev.get('input_tokens', 0)}in/{ev.get('output_tokens', 0)}out tok{cost}[/dim]"
            )
        elif t == "validation":
            if ev.get("success"):
                con.print(f"  [green]✓ {tid} sandbox passed[/green] (exit {ev.get('exit_code')})")
            else:
                con.print(
                    f"  [red]✗ {tid} sandbox FAILED[/red] (exit {ev.get('exit_code')}) — "
                    f"{ev.get('summary', '')}"
                )
                out = (ev.get("output") or "").strip()
                if out:
                    tail = out if verbose else "\n".join(out.splitlines()[-12:])
                    con.print(Panel(
                        tail, title=f"{tid} sandbox output{'' if verbose else ' (tail)'}",
                        border_style="red", expand=False,
                    ))
        elif t == "merge_conflict":
            con.print(f"  [yellow]⚠ {tid} merge conflict[/yellow] — {ev.get('output', '')}")
        elif t == "agent_error":
            con.print(f"  [red]⚠ {tid} agent error[/red] — {ev.get('error', '')}")
        elif t == "retry":
            con.print(f"  [yellow]↻ {tid} retrying (attempt {ev.get('next_attempt')})[/yellow]")
        elif t == "triage_analysis":
            con.print(f"  [cyan]🩺 {tid} triage agent analyzing failure (triage attempt {ev.get('triage_attempt')}/{ev.get('max_triage_retries')})...[/cyan]")
        elif t == "triage_recommendation":
            con.print(f"  [cyan]💡 {tid} triage recommendation:[/cyan]")
            rec = (ev.get("recommendation") or "").strip()
            if rec:
                con.print(Panel(rec, title=f"{tid} triage fix recommendation", border_style="cyan", expand=False))
        elif t == "merged":
            if ev.get("triage_healed"):
                con.print(f"  [bold green]✓ {tid} merged (self-healed via triage)[/bold green]")
            else:
                con.print(f"  [green]✓ {tid} merged[/green]")
        elif t == "test_quality":
            if ev.get("missing_tests"):
                con.print(f"  [yellow]⚠ {tid} no test added for a code change[/yellow]")
            if ev.get("sensitive_files"):
                con.print(
                    f"  [yellow]🔐 {tid} touches CI/sensitive config[/yellow] — "
                    f"{', '.join(ev['sensitive_files'])} [dim](not self-certifying — review the diff)[/dim]"
                )
        elif t == "test_critique":
            verdict = ev.get("verdict", "UNKNOWN")
            color = {"STRONG": "green", "WEAK": "yellow", "MISSING": "red"}.get(verdict, "yellow")
            con.print(f"  [{color}]🔎 {tid} test critic: {verdict}[/{color}]")
            if verdict not in {"STRONG"} and verbose:
                con.print(Panel(
                    (ev.get("critique") or "").strip(), title=f"{tid} test critique",
                    border_style=color, expand=False,
                ))

    return printer


printer = _make_event_printer(verbose=False)


@epic.command("run")
@click.argument("name_or_path")
@click.option("--prompt", required=True, help="High-level request to decompose, e.g. 'add JWT auth'.")
@click.option("--language", required=True, type=click.Choice(_LANGUAGE_CHOICES))
@click.option("--yes", is_flag=True, help="Skip the plan-review approval gate and run immediately.")
@click.option("--merge-to-main", is_flag=True, help="Merge straight to main instead of opening a pull request (the default).")
@click.option("--pr-base", default="main", show_default=True, help="Base branch the PR targets (and the integration branch forks from).")
@click.option("--verbose", "-v", is_flag=True, help="Show full sandbox output on failures (default: just the tail).")
def epic_run(name_or_path: str, prompt: str, language: str, yes: bool, merge_to_main: bool, pr_base: str, verbose: bool) -> None:
    """Decompose PROMPT against NAME_OR_PATH into a task DAG, show the plan for
    approval (HITL Stage 1), then execute it — routing each task to a model by
    its risk level via the providers configured in .env. Makes REAL LLM calls
    (planning + each task) and consumes real usage/quota.

    By default the epic's work is gathered on a per-epic integration branch and
    a single pull request is opened (via `gh`); pass --merge-to-main to merge
    directly to main instead. If PR mode can't reach a git remote or `gh`, it
    falls back to a local merge so nothing is lost.
    """
    try:
        root = registry.resolve(name_or_path)
    except registry.ProjectPathError as exc:
        raise click.ClickException(str(exc)) from exc

    # Cross-run lock: two epic runs on the same repo share its git working tree
    # and would clobber each other's `git checkout`. Held until this process exits.
    try:
        acquire_epic_run_lock(root)
    except RunLockError as exc:
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

    conventions = load_project_conventions(root)
    if conventions:
        console.print("[dim]Loaded .ai-os/conventions.md — injecting into the plan + every task.[/dim]")

    plan_assignment = scheduler.planning_assignment()
    console.print(
        f"Decomposing the request with [bold]{plan_assignment.provider}[/bold] "
        f"({plan_assignment.model or 'provider default'})... this is a real LLM call."
    )
    try:
        tasks = asyncio.run(
            decompose(prompt, engine, adapters[plan_assignment.provider],
                      model=plan_assignment.model, conventions=conventions)
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
    try:
        estimate = estimate_epic(tasks, engine, scheduler, adapters)
    except Exception:
        estimate = None  # estimation is best-effort; never block the run on it
    _print_plan_table(tasks, assignments, estimate)

    # Security guard: flag tasks touching CI/secrets/build/AI-OS-config files —
    # these run with real credentials post-merge, so they must go through a
    # reviewable PR, never a blind --merge-to-main.
    flagged = sensitive_paths({p for t in tasks for p in t.write_set})
    if flagged:
        console.print(
            "\n[bold yellow]⚠ Security-sensitive files in this plan[/bold yellow] "
            "(run with real credentials in CI / change how AI-OS validates) — review carefully:"
        )
        for p in flagged:
            console.print(f"  [yellow]•[/yellow] {p}")
        if merge_to_main:
            raise click.ClickException(
                "Refusing --merge-to-main: this plan touches security-sensitive files. "
                "Run without --merge-to-main so the change goes through a PR a human reviews."
            )
        console.print("[dim]These will go through the PR (default) for human review before merge.[/dim]")

    # HITL Stage 1: Plan Review gate (doc 12 §2.1). The React UI version is
    # Phase 4b; the approval gate itself works fine in a terminal.
    if not yes and not click.confirm("\nApprove this plan and execute the DAG?", default=False):
        console.print("Aborted — no tasks were run.")
        return

    # Cheap-model summarizer for large validation failure logs: route it to the
    # LOW-risk (cheapest configured) provider+model, so the expensive task model
    # gets a tight diagnosis on retry instead of the whole log.
    try:
        low = scheduler.assign("LOW")
        summarizer = build_output_summarizer(adapters[low.provider], low.model)
        # Cheap-model test critic (feature 1c): one extra cheap call per COMPLETED
        # task, judging test quality on its diff — surfaced in the PR body.
        test_critic = build_test_critic(adapters[low.provider], low.model)
        # 2-Tier Triage Agent: cheap LOW-model triage analysis to recommend fixes
        # for failed/blocked tasks to the primary agent for self-healing.
        triage_agent = build_triage_agent(adapters[low.provider], low.model)
    except Exception:
        summarizer = None  # no LOW provider configured -> skip summarization
        test_critic = None
        triage_agent = None

    async def _execute():
        # Accounting (Stage 3): persist epic + per-task rows + token/lock audit
        # so `ai-os cost` / `ai-os epic history` can read back real spend.
        persistence, engine = await Persistence.open(default_db_url())
        runner = EpicRunner(
            repo_root=root, scheduler=scheduler, adapters=adapters, language=language,
            sandbox_runner=EphemeralSandboxRunner(),
            summarizer=summarizer,
            test_critic=test_critic,
            triage_agent=triage_agent,
            # Terminal states only — the attempt/validation events (on_event)
            # cover the in-flight detail, so RUNNING would just be noise.
            on_status_change=lambda tid, status: (
                console.print(f"[dim]{tid}: {status}[/dim]") if status != "RUNNING" else None
            ),
            persistence=persistence,
            # Stage 4: rate-limit backoff + provider fallback always on; the
            # optional cost cap comes from AI_OS_EPIC_BUDGET_USD in the env.
            scheduling_policy=SchedulingPolicy.from_env(),
            create_pr=not merge_to_main,
            pr_base_branch=pr_base,
            on_event=_make_event_printer(verbose),
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
    if result.pull_request_url:
        console.print(f"\n  [bold green]Pull request opened:[/bold green] {result.pull_request_url}")
    elif result.integration_branch and result.merged_to_main:
        console.print(
            f"\n  [yellow]No git remote / gh — merged to {pr_base} locally.[/yellow] "
            f"Work is on branch [bold]{result.integration_branch}[/bold] (and {pr_base})."
        )
    elif result.integration_branch and result.completed:
        console.print(f"\n  Work gathered on branch [bold]{result.integration_branch}[/bold].")
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
@click.option("--merge-to-main", is_flag=True, help="Merge straight to main instead of opening a pull request (the default).")
@click.option("--pr-base", default="main", show_default=True, help="Base branch the PR targets.")
@click.option("--verbose", "-v", is_flag=True, help="Show full sandbox output on failures (default: just the tail).")
def epic_resume(name_or_path: str, epic_id: str, language: str, merge_to_main: bool, pr_base: str, verbose: bool) -> None:
    """Resume a crashed/interrupted epic: re-run only the tasks that weren't
    already COMPLETED (their merged work is kept), respecting the DAG. The task
    statuses come from the accounting DB; you supply the project + language
    again (they aren't stored). Makes REAL LLM calls for the remaining tasks.
    """
    try:
        root = registry.resolve(name_or_path)
    except registry.ProjectPathError as exc:
        raise click.ClickException(str(exc)) from exc

    try:
        acquire_epic_run_lock(root)  # same cross-run lock as `epic run`
    except RunLockError as exc:
        raise click.ClickException(str(exc)) from exc

    adapters = load_configured_adapters()
    if not adapters:
        raise click.ClickException(
            "No LLM providers configured. Copy .env.example to .env and add credentials."
        )
    router = ProtocolRouter(adapters, risk_provider_order=risk_provider_order_from_env())
    scheduler = DynamicScheduler(router)

    try:
        low = scheduler.assign("LOW")
        summarizer = build_output_summarizer(adapters[low.provider], low.model)
        test_critic = build_test_critic(adapters[low.provider], low.model)
    except Exception:
        summarizer = test_critic = None

    async def _resume():
        persistence, engine = await Persistence.open(default_db_url())
        runner = EpicRunner(
            repo_root=root, scheduler=scheduler, adapters=adapters, language=language,
            sandbox_runner=EphemeralSandboxRunner(),
            summarizer=summarizer,
            test_critic=test_critic,
            on_status_change=lambda tid, status: (
                console.print(f"[dim]{tid}: {status}[/dim]") if status != "RUNNING" else None
            ),
            persistence=persistence,
            scheduling_policy=SchedulingPolicy.from_env(),
            create_pr=not merge_to_main,
            pr_base_branch=pr_base,
            on_event=_make_event_printer(verbose),
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
    if result.pull_request_url:
        console.print(f"\n  [bold green]Pull request opened:[/bold green] {result.pull_request_url}")
    elif result.integration_branch and result.merged_to_main:
        console.print(f"\n  [yellow]No git remote / gh — merged to {pr_base} locally.[/yellow]")


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


@main.command("startup")
@click.option("--prompt", "-p", default=None, help="Raw text prompt or description for the startup brief.")
@click.option("--brief", "-b", default=None, help="Path to brief markdown file or brief text content.")
@click.option("--out", "-o", default=None, help="Output directory path for generated startup scaffold.")
@click.option("--no-deploy", is_flag=True, default=False, help="Skip deployment step.")
def startup(
    prompt: str | None,
    brief: str | None,
    out: str | None,
    no_deploy: bool,
) -> None:
    """Generate a startup scaffold from a design brief or text prompt."""
    raw_input = brief if brief is not None else (prompt if prompt is not None else "Default Startup App")
    design_brief = parse_startup_brief(raw_input)

    out_dir = Path(out) if out else Path("out")
    generated_path = generate_startup(out_dir, design_brief)

    console.print(f"Startup scaffold generated successfully at: {generated_path}")
    console.print(f"Title: {design_brief.title}")
    console.print(f"Value Proposition: {design_brief.value_proposition}")
    console.print(f"Target Audience: {design_brief.target_audience}")
    console.print(f"Pages: {', '.join(p['title'] if isinstance(p, dict) else str(p) for p in design_brief.pages)}")
    console.print(f"Core Flow: {', '.join(s['action'] if isinstance(s, dict) else str(s) for s in design_brief.core_flow)}")
    console.print(f"Brand / Tone: {design_brief.brand}")
    if no_deploy:
        console.print("Deployment: Skipped (--no-deploy)")
    else:
        console.print("Deployment: Ready")


if __name__ == "__main__":
    main()
