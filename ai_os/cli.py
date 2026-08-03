"""AI-OS CLI: project registry, one-shot Polyglot Analyzer scans (Phase 1),
manual MCP provider adapter testing (Phase 3a), and running one task
end-to-end through the sandboxed agent loop (Phase 3b)."""
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
from ai_os.core.lock_manager import LockManager
from ai_os.core.models import TaskNode
from ai_os.core.staging import GitStagingEngine
from ai_os.core.task_runner import TaskRunner, build_claude_cli_agent_turn_executor
from ai_os.knowledge.graph_engine import KnowledgeEngine
from ai_os.mcp.adapters.base_adapter import LLMTaskRequest
from ai_os.mcp.config import load_configured_adapters
from ai_os.sandbox.container_runner import EphemeralSandboxRunner

console = Console()


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


if __name__ == "__main__":
    main()
