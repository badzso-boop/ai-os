# Project conventions for AI-OS agents

These rules apply to every task AI-OS runs against THIS repository (the AI-OS
codebase itself — dogfooding). They are injected into both the planner and every
task's prompt. Follow them exactly.

## Language & style
- **All code identifiers, comments, docstrings, and commit messages are in
  English.** (The prose docs under `docs/` and the README are Hungarian — but you
  are writing code, so: English.)
- Start every module with `from __future__ import annotations`.
- Write a **module-level docstring** that explains *how the module actually
  works* and calls out any deliberate deviation from a doc — this repo's
  docstrings are explanatory, not one-liners (see `ai_os/core/staging.py`,
  `ai_os/sandbox/container_runner.py` as the reference tone).
- Match the surrounding code: type hints everywhere, `dataclass` for plain
  records, `pydantic` only where the repo already uses it (`core/models.py`,
  `mcp/adapters/base_adapter.py`). Prefer small, pure, directly-testable
  functions over classes with hidden state.
- Keep comments at the density of the neighbouring modules — explain the *why*
  and the non-obvious, not the obvious.

## Architecture principles (non-negotiable — they define this project)
1. **Compiler First.** Never spend an LLM token on anything an algorithm can do
   deterministically. The work in this epic is a *deterministic* layer — it must
   contain **zero** LLM calls and zero network access. Parsing, graph-building,
   and heuristics are pure functions over data.
2. **Knowledge Before Generation.** Build on the existing Phase-1 analyzer
   (`ai_os/analyzer/` — Tree-sitter HTML/CSS/JS parsing) and the
   `KnowledgeEngine` graph (`ai_os/knowledge/graph_engine.py`,
   `networkx.DiGraph`). Reuse its patterns (node/edge dataclasses, a
   `build_*_context_cache` that renders a compressed text block) — do not invent
   a parallel mechanism.
3. **Reuse, don't duplicate.** No new heavy third-party dependency — everything
   needed (`tree-sitter*`, `networkx`, `pydantic`) is already in `pyproject.toml`.
   If you think you need a new dependency, you almost certainly don't; stop and
   reconsider.

## Testing philosophy (this repo's hard rule)
- **The automated test suite NEVER makes a real LLM call, a real network call, or
  starts a real Docker container.** Deterministic modules get plain, fast unit
  tests over hand-built inputs. Where you'd need an LLM/adapter, inject a fake.
  Real `git`/`tmp_path`/`pathlib` usage in tests is fine and encouraged.
- **Tests are mandatory for every change** (Phase 6 enforces this) and must be
  *meaningful* — call the code under test with real inputs and assert on real
  outputs. No tautological tests (no `assert True`, no asserting a mock returns
  what you told it to).
- Put the new tests for this epic under **`tests/ui/`** (a `test_*.py` file per
  module). The sandbox validates with `python -m pytest tests/ui` — so your tests
  must pass there, needing no Docker/network. Existing tests elsewhere stay green.

## Sandbox / runtime notes
- The validation sandbox runs **Python 3.12** (the repo targets 3.13 locally, but
  keep your code 3.12-compatible — avoid 3.13-only stdlib/syntax).
- Import the package as `ai_os.<...>`; the repo is a flat-layout package (no
  `src/`), importable from the repo root.

## Scope discipline for this epic
- This epic builds the **deterministic core only** of the UI-debug toolchain (see
  `docs/19_UI_DEBUG_TOOLCHAIN.md`). **Do NOT** add the headless Playwright probe,
  any browser automation, the LLM triage/routing, or the CLI command in this epic
  — those are explicitly deferred to a later, human-driven step. Staying inside
  the deterministic boundary is what makes this epic sandbox-validatable.
- New code lives under `ai_os/ui/`. Do not modify `ai_os/analyzer/` or
  `ai_os/knowledge/` beyond importing from them (read-only reuse).

## Security / sensitive files
- Do not touch `.github/workflows/*`, `.ai-os/*`, Dockerfiles, or any
  secrets/CI/build config — these are flagged for human review and must not be
  changed as part of a feature task.

---

## ⛔ Critical rules learned from production failures (MUST follow — non-negotiable)

### 1. NEVER create a directory named after a third-party PyPI package
**Why:** A local directory (e.g., `tests/mcp/`, `src/requests/`) with an
`__init__.py` becomes a Python package that **shadows** the real installed package.
Every `from mcp.xxx import ...` anywhere in the codebase will silently import
from YOUR directory instead — causing `ModuleNotFoundError` for the real submodule.

✅ **DO:** `tests/test_git_tools.py`, `tests/test_mcp_server.py`
❌ **DON'T:** `tests/mcp/__init__.py`, `tests/requests/`, `tests/json/`

**Rule:** If you want to group related tests in a subdirectory, name it after the
FEATURE, not the library. E.g., `tests/git/test_tools.py`, `tests/mcp_server/test_git.py`.
And check: does a PyPI package with that directory name exist? If yes, rename it.

### 2. NEVER rename or change the signature of existing public APIs
When you are asked to *extend* or *add to* an existing module, you must not:
- Rename dataclass fields (e.g., `ToolContext.worktree_path` → `worktree_root`)
- Change argument names/order of public functions
- Change attribute names on return types (e.g., `CallToolResult.is_error` → `isError`)

**Why:** Dozens of existing tests depend on the current API. Renaming breaks them all.

**Rule:** Before touching any existing class/function:
1. Run `grep -r "ClassName\|function_name" tests/` to see how it's used.
2. Only EXTEND with new optional fields/parameters — never remove or rename.
3. If you disagree with a naming choice, leave a comment and do NOT change it.

### 3. ALWAYS verify existing tests still pass after your changes
Before writing new test files, confirm the already-passing tests still pass with your new code:
```bash
python -m pytest tests/ --ignore=tests/your_new_file.py -q
```
If existing tests fail, STOP and fix the regression before continuing.

### 4. When extending an existing module, copy it and diff — do not rewrite from scratch
If you need to add new functionality to `ai_os/mcp/mcp_server.py` or similar,
use the **existing file as your starting point**. Add your new code BELOW the existing
code with minimal structural changes. A wholesale rewrite of 400-line modules is
almost always wrong and will break the existing test suite.

### 5. Do not add duplicate test shim files
If `tests/test_git_tools.py` already exists, do NOT create `tests/ui/test_git_tools.py`
or `tests/mcp/test_git_tools.py` as a re-exporting shim. The sandbox runs ALL
test files under `tests/`, and duplicate names cause import collection errors.
Your task's new tests should be in a single, well-named file.

