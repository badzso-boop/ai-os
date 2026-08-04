"""Repo-side project conventions injected into every AI-OS agent prompt.

A project drops an optional `.ai-os/conventions.md` at its repo root (committed,
so it lands in every git worktree). AI-OS reads it and injects it into BOTH the
epic planner's decomposition prompt (so the DAG respects the conventions — e.g.
"add an i18n translation task") AND every task's agent turn (so each task follows
them). This is the provider-agnostic place for project-level guidance — unlike a
`CLAUDE.md`, which only the Anthropic `claude` CLI auto-loads, this reaches
Gemini/OpenRouter tasks too.

Example `.ai-os/conventions.md`:

    # Project conventions for AI-OS agents

    - All user-facing strings MUST go through the i18n system (`t('key')`),
      never hardcoded. Default language is Hungarian; add English translations.
    - UI primitives are Base UI (`@base-ui/react`), NOT shadcn/Radix — compose a
      Button-as-link with `render={<Link/>}` + `nativeButton={false}`.
    - Prefer the existing service/repository layer over raw SQL.
"""
from __future__ import annotations

from pathlib import Path

CONVENTIONS_RELPATH = Path(".ai-os") / "conventions.md"

# Cap injected size so a runaway conventions file can't blow the context budget.
_MAX_CHARS = 8000


def load_project_conventions(repo_root: Path) -> str:
    """Return the project's `.ai-os/conventions.md` contents (stripped, size
    capped), or `""` if the file is absent/empty. Never raises — a bad read just
    means no conventions are injected."""
    try:
        path = Path(repo_root) / CONVENTIONS_RELPATH
        if not path.is_file():
            return ""
        text = path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""
    if len(text) > _MAX_CHARS:
        text = text[:_MAX_CHARS] + "\n… (project conventions truncated)"
    return text


def conventions_block(conventions: str) -> str:
    """Render conventions as a labelled prompt section, or "" if there are
    none — so callers can unconditionally `+= conventions_block(...)`."""
    if not conventions.strip():
        return ""
    return (
        "\n## Project conventions (MUST follow)\n"
        "The project defined these rules for all changes — follow them exactly:\n"
        f"{conventions.strip()}\n"
    )
