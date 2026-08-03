"""AI-OS Phase 1 CLI: project registry + one-shot Polyglot Analyzer scans."""
from __future__ import annotations

import time
from pathlib import Path

import click
from rich.console import Console
from rich.table import Table

from ai_os import registry
from ai_os.analyzer.call_graph_builder import CallGraphBuilder
from ai_os.analyzer.languages import LANGUAGES
from ai_os.knowledge.graph_engine import KnowledgeEngine

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
        f"IMPORTS unresolved: {summary['import_edges_unresolved']}/{summary['import_edges_total']}   "
        f"CALLS ambiguous: {summary['call_edges_ambiguous']}/{summary['call_edges_total']}"
    )
    console.print(
        f"\nKnowledge graph: {summary['graph']['nodes']} nodes / {summary['graph']['edges']} edges"
    )
    if summary["graph_written_to"]:
        console.print(f"Written to {summary['graph_written_to']}")


if __name__ == "__main__":
    main()
