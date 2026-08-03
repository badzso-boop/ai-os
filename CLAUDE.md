# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Repository status

**Phase 1 — Polyglot Analyzer & Knowledge Graph — is implemented and stable.** Everything under `ai_os/analyzer/`, `ai_os/knowledge/`, `ai_os/registry.py`, `ai_os/cli.py` is real, working code with a passing pytest suite. Phase 2–4 (`core/`, `mcp/`, `sandbox/`, `ui/`) are **not built yet** — they remain the *planned* architecture described in `docs/`, which stays the single source of truth for anything not covered below. Check the filesystem before assuming a module/command exists.

### Build / test

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/pytest -q          # 37 tests, ~1s
```
No `mypy`/`flake8`/`npm test`/`tsc` configured yet.

## What AI-OS is (full vision, drives Phase 2+ design)

**AI-OS** is a planned **AI Software Engineering Orchestrator** — not a coding assistant or IDE plugin. It acts like an operating system kernel that schedules and supervises AI coding agents (Claude, GPT, Gemini, DeepSeek, local Ollama/vLLM models) as interchangeable "execution cores," while a fully deterministic Python core handles everything that doesn't require AI judgement.

Core philosophy (drives every design decision, including Phase 1's):
1. **Compiler First** — never spend AI tokens on anything an algorithm/compiler can do 100% deterministically (AST parsing, dependency/call graphs, type/lint checks). Phase 1 is a direct application of this: it's a complete, zero-LLM analysis layer.
2. **Knowledge Before Generation** — agents never see the whole codebase; they get a compressed `Context Cache` containing only the symbols/interfaces/types relevant to their task, built from a Knowledge Graph.
3. **Model-Agnostic & Cost-Aware** — all LLM communication goes through standardized MCP adapters; a Dynamic Scheduler picks the cheapest/most reliable model per task based on risk classification.

Full planned architecture (Orchestrator Core → Dynamic Scheduler → Lock Manager, Git Worktree + ephemeral Docker validation, Glass Box UI with 3-stage HITL) is described in `docs/01`–`docs/18`. Phase 1 below is the foundation everything else builds on.

---

## Phase 1 — how it actually works

### End-to-end flow
```
ai-os scan <project>
  └─ CallGraphBuilder.scan(root)
       ├─ walk filesystem, skip DEFAULT_EXCLUDED_DIRS (node_modules, .git, target, build, venv, ...)
       ├─ per file: detect_language() → TreeSitterEngine.parse_file() → extract_symbols()
       ├─ build a global name→FQN index (for CALLS) and a type-name→FQN index (for EXTENDS)
       ├─ build a Java package→file index (for Java import resolution)
       ├─ load dependency manifests once (package.json / requirements*.txt / pyproject.toml)
       ├─ per file: extract IMPORTS, CALLS, EXTENDS edges (all deterministic, name/path-based)
       └─ → ProjectScanResult
  └─ KnowledgeEngine.build_from_scan(result)
       ├─ FileNode per file, ClassNode/FunctionNode/TypeNode per symbol, CONTAINS edges
       ├─ IMPORTS / CALLS / EXTENDS edges (from the scan result)
       └─ a skeleton stub (signature-only, body stripped) attached to every symbol node
  └─ CLI prints a Rich summary table and optionally writes the full graph as JSON (--out)
```

### Module map (`ai_os/`)

- **`registry.py`** — external, updatable project registry at `~/.ai-os/projects.json` (override via `AI_OS_HOME` env var, used by tests to avoid touching the real file). Atomic writes (tempfile + `os.replace`). `add/remove/list_projects/resolve`; `resolve()` treats a registered name as priority, otherwise falls back to a literal filesystem path — so `ai-os scan` works both on registered projects and ad-hoc paths.
- **`analyzer/languages.py`** — `LanguageProfile` per supported language (extensions, Tree-sitter grammar factory, `.scm` query file, comment template) + `detect_language(path)`. Supported: **Python, Java, JavaScript, TypeScript, HTML, CSS, SQL**. HTML/CSS have no symbol query (no functions/classes to extract) but still get import edges.
- **`analyzer/queries/*.scm`** — per-language Tree-sitter queries capturing `@class.def/@class.name`, `@function.def/@function.name`, `@call.site/@call.name` (SQL uses `@type.def/@type.name` for `CREATE TABLE/VIEW`).
- **`analyzer/tree_sitter_engine.py`** — `TreeSitterEngine.parse_file()` + `.extract_symbols()`. Produces `Symbol` records: kind (`class|interface|function|method|type`), FQN (`<relpath>::<QualifiedName>`), params, return type, docstring, `extends` list of `(base_name, "extends"|"implements")`. Also `extract_call_sites()` for the call graph.
- **`analyzer/call_graph_builder.py`** — the project-wide walk. Builds:
  - **IMPORTS**: per-language resolution (Python module/package resolution against project root; JS/TS relative + `require()` with extension probing; Java via a package→file index; HTML `<script src>`/`<link href>`; CSS `@import`).
  - **CALLS**: purely name-based matching against the global symbol-name index — no type inference, ever (per "Compiler First": zero LLM disambiguation). Exactly one FQN match → normal edge; multiple matches → edge to *all* candidates with `ambiguous=True`; zero matches → dropped. This is a known, accepted source of noise on codebases with many same-named methods (getters, CRUD boilerplate, exception constructors) — see "Known limitations" below.
  - **EXTENDS**: same name-based approach for `extends`/`implements` (Java's `implements` is recorded as `EXTENDS` with `via="implements"`, not a separate edge kind).
  - **Import classification (`external` field on `ImportEdge`)**: every unresolved import is further classified as `external=True` (expected — a third-party dependency or stdlib module) or `external=False` (a genuine resolution gap worth investigating). Sources of truth: `package.json` (`dependencies`/`devDependencies`/`peerDependencies`/`optionalDependencies`, unioned across every non-`node_modules` `package.json` under the scanned root) + a small Node builtin-module list for JS/TS; `requirements*.txt` + `pyproject.toml` (`project.dependencies`, `tool.poetry.dependencies`) + `sys.stdlib_module_names` for Python; `java.*`/`javax.*`/`jakarta.*` prefix *or* simply "not in the already-complete internal package index" for Java (there's no concept of a "genuine gap" for Java imports — the internal index is exhaustive, so a miss is always external by construction). Verified against a real ~350-file mixed Java/TS/Python repo: took unresolved imports from 70% down to 0% "genuinely unresolved" (the rest correctly classified as external deps).
- **`knowledge/graph_engine.py`** — `KnowledgeEngine` wraps a `networkx.DiGraph`. Node types: `FileNode`, `ClassNode` (covers interfaces via a `kind` attr), `FunctionNode` (covers methods via `is_method`), `TypeNode` (SQL tables/views). Edge kinds: `CONTAINS`, `IMPORTS`, `CALLS`, `EXTENDS`. Key methods:
  - `k_hop_subgraph(seeds, max_hops)` — custom **mixed-direction BFS** (not `nx.ego_graph`, which is single-direction only): outgoing for `IMPORTS`/`EXTENDS`/`CONTAINS`, incoming for `CALLS`/`CONTAINS` (so a seed pulls in both what it depends on *and* who calls it).
  - `build_context_cache(seeds, max_hops)` — renders the k-hop subgraph's skeleton stubs into a single "COMPRESSED CONTEXT CACHE" text block — this is the Phase-2+-facing artifact that will feed agent prompts.
  - `stats()`, `invalidate_file()`, `to_json()`/`from_json()` (networkx node-link JSON, pretty-printed).
- **`knowledge/skeleton_extractor.py`** — `extract_skeleton(symbol, source, profile)`: slices out just the signature, replaces the body with a language-appropriate stub marker and a banner comment. HTML/CSS/SQL pass through unchanged (no meaningful body to strip).
- **`cli.py`** — Click CLI, see below.

### CLI surface (stable, general-purpose — not orchestrator-only)

```
ai-os project add <name> <path> [--force]
ai-os project remove <name>
ai-os project list
ai-os scan <name-or-path> [--out graph.json] [--max-hops N] [--languages ...] [--exclude DIR] [--skeleton FQN] [--json]
```

`ai-os scan --out graph.json` writes the **full Knowledge Graph** (pretty-printed node-link JSON) to disk — this is a first-class, standalone feature meant for direct human use, not just future internal orchestrator consumption. Keep it working as `KnowledgeEngine` evolves.

The scan summary reports import resolution as three numbers: total unresolved, of which external (expected), of which genuinely unresolved (worth investigating) — see `_build_summary` in `cli.py`.

### Known limitations (intentional trade-offs, not bugs to silently "fix")

- **CALLS ambiguity is inherent to name-only resolution.** On a real Java codebase this can hit ~45% ambiguous. A discussed but *not implemented* improvement is scoping name candidates to the caller's file's import graph — but a naive version of that would introduce **false negatives**: Java same-package classes need no import statement at all, so scoping by literal imports would silently drop real edges for same-package calls (very common in service/mapper/repository-per-package layouts) and for methods inherited from a supertype not directly imported by name. A correct version would need same-package siblings + the EXTENDS-transitive closure of imports, not just the raw import list — more work than the simple pitch, not yet built.
- **Java "external" classification is a default, not a lookup.** Any unresolved Java import is classified external because the internal package index is exhaustive by construction (not because we verified it against a POM/Gradle dependency list) — this is deliberate, not a gap.
- **No file-watcher / incremental re-parse yet.** Every `scan` is a one-shot full walk (fast enough: ~2s for ~350 files). `ai_os/knowledge/event_bus.py` (doc 04) is not built — that's what would turn this into a live-updating daemon.

## Intended directory layout

Per `docs/14_PROJECT_DIRECTORY_STRUCTURE.md`:
- `ai_os/analyzer/`, `ai_os/knowledge/`, `ai_os/registry.py`, `ai_os/cli.py` — **implemented (Phase 1)**, described above.
- `ai_os/core/` — planner, scheduler, lock_manager, staging (Git worktree engine), `db/` (SQLAlchemy models) — **not yet created**.
- `ai_os/mcp/` — `mcp_server.py`, `protocol_router.py`, `adapters/` (per-provider: anthropic, openai, gemini, local) — **not yet created**.
- `ai_os/sandbox/` — `container_runner.py`, `log_parser.py` — **not yet created**.
- `ui/` — React + Vite + Tailwind + React Flow + Monaco frontend — **not yet created**.

When starting Phase 2+ implementation, follow this structure unless there's a concrete reason to deviate, since the docs (and Phase 1's own test suite) assume it.

## Where to look for detail (docs/, still authoritative for anything not implemented)

| Doc | Subsystem |
| --- | --- |
| `01_ARCHITECTURE_OVERVIEW.md` | Full system diagram, end-to-end flow, responsibility matrix |
| `02_ORCHESTRATOR_CORE.md` | DAG Planner (`TaskNode` schema), Dynamic Scheduler (risk→model matrix), async `LockManager` implementation |
| `03_POLYGLOT_ANALYZER.md` | Tree-sitter language support, symbol/call-graph extraction, incremental re-parse on file change (Phase 1 implements the non-incremental parts; incremental re-parse is not built) |
| `04_KNOWLEDGE_CONTEXT_ENGINE.md` | Knowledge Graph node/edge types, skeleton/stub context compression, event-driven cache invalidation (Phase 1 implements the graph + stubs; event-driven invalidation is not built) |
| `05_EXECUTION_VALIDATION_SANDBOX.md` | Git worktree isolation + ephemeral Docker validation + feedback/HITL state machine — not built |
| `06_GLASS_BOX_UI.md` | Observability dashboard concept, WebSocket event shape, HITL control panel options — not built |
| `07_MCP_ADAPTER_ROUTER.md` | Kernel-vs-cores framing, exact MCP JSON-RPC tool schemas, Python blueprint — not built |
| `08_KNOWLEDGE_GRAPH_AND_SUBGRAPH_EXTRACTION.md` | Full graph schema, k-hop subgraph algorithm, skeleton extractor — **implemented in Phase 1**, see `graph_engine.py`/`skeleton_extractor.py` above |
| `09_GIT_WORKTREE_STAGING_ENGINE.md` | Worktree lifecycle, async merge queue, rebase/re-validate rule — not built |
| `10_EPHEMERAL_CONTAINER_SANDBOX_SPEC.md` | Container hardening flags, per-language sandbox profile matrix — not built |
| `11_ORCHESTRATOR_TECH_STACK_AND_DEPL.md` | Language/library choices, SQLite storage design, Docker Compose layout, endpoint list — not built |
| `12_GLASS_BOX_UI_AND_HITL_SPEC.md` | Full 3-stage HITL workflow, UI component blueprint — not built |
| `13_DB_SCHEMA_AND_MODELS.md` | ER diagram and SQLAlchemy 2.0 async models — not built |
| `14_PROJECT_DIRECTORY_STRUCTURE.md` | Intended repo layout — see "Intended directory layout" above for current-vs-planned |
| `15_PROVIDER_AUTHENTICATION_AND_ROUTING.md` | Dual auth model per provider, `.env` shape, adapter blueprints — not built |
| `16_MVP_DEVELOPMENT_ROADMAP.md` | 4-phase build order: (1) Analyzer & Knowledge Graph **[done]**, (2) Core/Locking/Git, (3) MCP Router & Sandbox, (4) Glass Box UI & HITL |
| `17_YOUTUBE_SERIES_AND_OPENSOURCE_PLAN.md` | Content/marketing plan for a companion YouTube devlog series (not architecture) |
| `18_YOUTUBE_PRODUCTION_AND_EDITING_GUIDE.md` | Video production/editing playbook for the same series (not architecture) |

## Language note

The docs and README are written in Hungarian. Code identifiers, comments, and commit messages in this project are in **English** (the convention established by Phase 1's implementation).
