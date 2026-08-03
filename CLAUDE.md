# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Repository status

**Phase 1 — Polyglot Analyzer & Knowledge Graph — is implemented and stable.** Everything under `ai_os/analyzer/`, `ai_os/knowledge/`, `ai_os/registry.py`, `ai_os/cli.py` is real, working code.

**Phase 2 — Orchestrator Core & Git Engine — is also implemented and stable.** Everything under `ai_os/core/` (`models.py`, `db/`, `lock_manager.py`, `staging.py`, `planner.py`) is real, working code. Phase 3–4 (`mcp/`, `sandbox/`, `ui/`) are **not built yet** — they remain the *planned* architecture described in `docs/`, which stays the single source of truth for anything not covered below. Check the filesystem before assuming a module/command exists.

### Build / test

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/pytest -q          # 79 tests (78 passed + 1 documented xfail), ~4s
```
No `mypy`/`flake8`/`npm test`/`tsc` configured yet.

## What AI-OS is (full vision, drives Phase 2+ design)

**AI-OS** is a planned **AI Software Engineering Orchestrator** — not a coding assistant or IDE plugin. It acts like an operating system kernel that schedules and supervises AI coding agents (Claude, GPT, Gemini, DeepSeek, local Ollama/vLLM models) as interchangeable "execution cores," while a fully deterministic Python core handles everything that doesn't require AI judgement.

Core philosophy (drives every design decision, including Phase 1's):
1. **Compiler First** — never spend AI tokens on anything an algorithm/compiler can do 100% deterministically (AST parsing, dependency/call graphs, type/lint checks). Phase 1 is a direct application of this: it's a complete, zero-LLM analysis layer.
2. **Knowledge Before Generation** — agents never see the whole codebase; they get a compressed `Context Cache` containing only the symbols/interfaces/types relevant to their task, built from a Knowledge Graph.
3. **Model-Agnostic & Cost-Aware** — all LLM communication goes through standardized MCP adapters; a Dynamic Scheduler picks the cheapest/most reliable model per task based on risk classification.

Full planned architecture (Orchestrator Core → Dynamic Scheduler → Lock Manager, Git Worktree + ephemeral Docker validation, Glass Box UI with 3-stage HITL) is described in `docs/01`–`docs/18`. Phases 1 and 2 below are the foundation everything else (MCP adapters, sandbox, UI) builds on.

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

---

## Phase 2 — how it actually works

Built by 4 parallel agents against a plan validated against real library behavior (SQLAlchemy 2.0 async pooling, real `git worktree`/`rebase` semantics, networkx 3.6.1) — the illustrative blueprint code in docs 02/09/13 had several real correctness gaps (self-deadlock risk, in-memory SQLite pooling pitfalls, no crash-recovery for worktree creation), all fixed in the implementation, not reproduced from the docs verbatim.

### Module map (`ai_os/core/`)

- **`models.py`** — pydantic `TaskNode` (id, title, description, risk_level, target_files, read_set, write_set, dependencies, status, max_retries, retry_count) and a minimal `EpicNode`. **Deliberate deviation from doc 14's directory listing** (which shows only `core/db/models.py`): this is the in-memory planning contract shared by `planner.py` and (by convention, not an import) `lock_manager.py`/`staging.py`, kept separate from the SQLAlchemy persistence models in `db/models.py`. Validators reject `read_set`/`write_set` overlap and self-dependency at construction time. **Path convention binding this to `lock_manager.py`/`staging.py`**: every path in `target_files`/`read_set`/`write_set` is POSIX-style, relative to repo root, no leading `./`.
- **`db/database.py` + `db/models.py`** — async SQLAlchemy 2.0 (`aiosqlite`). `make_engine(url)` applies `StaticPool`+`check_same_thread=False` only for `:memory:` URLs (otherwise each pooled connection would open a distinct, empty in-memory DB); a `connect` event listener (targeting `engine.sync_engine`, not the `AsyncEngine` wrapper) sets `PRAGMA journal_mode=WAL` + `PRAGMA foreign_keys=ON` (SQLite has FK enforcement off per-connection by default; WAL is a no-op on `:memory:`, so tests assert it only against a real `tmp_path` file). `init_db(engine)` is an explicit async `Base.metadata.create_all` call (schema creation needs a live connection, so it can't run at import time). Models: `EpicModel`, `TaskModel`, `LockAuditModel`, `TokenCostModel`, `GraphNodeModel`, `GraphEdgeModel` per doc 13's ER diagram (JSON columns via `default=list` callables). **No Alembic** — deliberate MVP scope cut, no prior schema to migrate from. **Nothing writes to `LockAuditModel`/`TokenCostModel` yet** — schema only, wiring is Phase 3+ orchestrator-glue work.
- **`lock_manager.py`** — `LockManager`: ownership-tracked async read/write file locks (`_readers: dict[str, set[task_id]]`, `_writer: dict[str, task_id]`, single `asyncio.Condition`). Fixes two real bugs in doc 02's naive count-only blueprint: (1) a task re-acquiring a write lock it already holds no longer self-deadlocks; (2) `release_locks` only releases locks the caller actually owns. Public API is the `locks(task_id, read_set, write_set)` async context manager (guarantees release-on-exception) — prefer it over the raw `acquire_locks`/`release_locks` primitives. **Known, accepted limitation**: no FIFO/writer-priority fairness — a writer can be starved by a steady stream of overlapping readers; documented via an explicit `xfail` test, not silently ignored.
- **`staging.py`** — `GitStagingEngine(repo_root)`: async (`asyncio.create_subprocess_exec` throughout) Git worktree lifecycle per doc 09. `create_worktree(task_id, base_branch="main")` is **idempotent via destroy-and-recreate** (`worktree prune` → best-effort `remove --force`/`branch -D` → `worktree add -B`) — crash-recovery policy is "start fresh," not "resume." `stage_and_merge_task(task_id, commit_message, validator_callback)` guards "nothing to commit," rebases onto `base_branch`, aborts+returns `False` on conflict (worktree left alive for retry/HITL), wraps validator exceptions in `ValidationCallbackError` (infra fault) vs. a plain `False` return (business-outcome failure: tests failed), fast-forward-merges on success, and **only then** calls `cleanup_worktree`. `abandon_task(task_id)` is the separate, explicit terminal-cleanup path (max retries exhausted / HITL-aborted). **Known limitation, flagged not fixed**: the merge step does `git checkout main` + `merge --ff-only` directly in `repo_root` — fine against a disposable test repo, but would yank a real developer's checkout out from under them; a production fix would use `git fetch <worktree> <branch>:main` instead (TODO in the module docstring).
- **`planner.py`** — deterministic-only (no LLM decomposition — that's Phase 3+): `build_graph(tasks)` builds a `networkx.DiGraph` from `TaskNode.dependencies`, raising `UnknownDependencyError` on a dangling reference rather than silently creating a phantom node; `validate_acyclic(graph)` raises `CyclicDependencyError` carrying the actual cycle (`nx.find_cycle`); `topological_batches(graph)` returns `list[list[str]]` generations via `nx.topological_generations`, **explicitly re-sorted within each generation by node-insertion order** (empirically, raw generation order follows edge-insertion order, not task-list order, which would make output non-reproducible across equivalent inputs otherwise). **Batches are not conflict-aware by design**: two tasks with no dependency edge can land in the same generation even with overlapping `write_set`s — `LockManager` alone serializes that at runtime; the planner only reasons about causal ordering.

### Integration test (`tests/test_orchestrator_integration.py`)

Ties `LockManager` + `GitStagingEngine` together against a real disposable git repo to concretely demonstrate doc 16's two Phase 2 acceptance criteria (no mocks): **Test A** — two tasks with disjoint `write_set`s and no dependency edge run concurrently via `asyncio.gather`; whichever merges second genuinely exercises `git rebase main`. **Test B** — two tasks with the *same* `write_set` and no dependency edge (deliberately, to prove the Lock Manager — not the DAG — provides the guarantee) are fully serialized by `LockManager.locks()`; their critical sections are asserted to never overlap in wall-clock time, and because of that real serialization, the second task's git operations never race the first's, so no conflict is even possible.

---

## Intended directory layout

Per `docs/14_PROJECT_DIRECTORY_STRUCTURE.md`:
- `ai_os/analyzer/`, `ai_os/knowledge/`, `ai_os/registry.py`, `ai_os/cli.py` — **implemented (Phase 1)**, described above.
- `ai_os/core/` — `models.py`, `db/` (`database.py`, `models.py`), `lock_manager.py`, `staging.py`, `planner.py` — **implemented (Phase 2)**, described above. `scheduler.py` (Dynamic Scheduler / LLM risk→model routing) — **not yet created** (needs Phase 3's MCP adapters).
- `ai_os/mcp/` — `mcp_server.py`, `protocol_router.py`, `adapters/` (per-provider: anthropic, openai, gemini, local) — **not yet created**.
- `ai_os/sandbox/` — `container_runner.py`, `log_parser.py` — **not yet created**.
- `ui/` — React + Vite + Tailwind + React Flow + Monaco frontend — **not yet created**.

When starting Phase 3+ implementation, follow this structure unless there's a concrete reason to deviate, since the docs (and Phase 1/2's own test suites) assume it.

## Where to look for detail (docs/, still authoritative for anything not implemented)

| Doc | Subsystem |
| --- | --- |
| `01_ARCHITECTURE_OVERVIEW.md` | Full system diagram, end-to-end flow, responsibility matrix |
| `02_ORCHESTRATOR_CORE.md` | DAG Planner (`TaskNode` schema), Dynamic Scheduler (risk→model matrix), async `LockManager` — **TaskNode/planner/lock_manager implemented in Phase 2** (see above); Dynamic Scheduler (risk→model routing) not built |
| `03_POLYGLOT_ANALYZER.md` | Tree-sitter language support, symbol/call-graph extraction, incremental re-parse on file change (Phase 1 implements the non-incremental parts; incremental re-parse is not built) |
| `04_KNOWLEDGE_CONTEXT_ENGINE.md` | Knowledge Graph node/edge types, skeleton/stub context compression, event-driven cache invalidation (Phase 1 implements the graph + stubs; event-driven invalidation is not built) |
| `05_EXECUTION_VALIDATION_SANDBOX.md` | Git worktree isolation + ephemeral Docker validation + feedback/HITL state machine — worktree isolation implemented in Phase 2 (`staging.py`); Docker sandbox/HITL not built |
| `06_GLASS_BOX_UI.md` | Observability dashboard concept, WebSocket event shape, HITL control panel options — not built |
| `07_MCP_ADAPTER_ROUTER.md` | Kernel-vs-cores framing, exact MCP JSON-RPC tool schemas, Python blueprint — not built |
| `08_KNOWLEDGE_GRAPH_AND_SUBGRAPH_EXTRACTION.md` | Full graph schema, k-hop subgraph algorithm, skeleton extractor — **implemented in Phase 1**, see `graph_engine.py`/`skeleton_extractor.py` above |
| `09_GIT_WORKTREE_STAGING_ENGINE.md` | Worktree lifecycle, async merge queue, rebase/re-validate rule — **implemented in Phase 2**, see `staging.py` above |
| `10_EPHEMERAL_CONTAINER_SANDBOX_SPEC.md` | Container hardening flags, per-language sandbox profile matrix — not built |
| `11_ORCHESTRATOR_TECH_STACK_AND_DEPL.md` | Language/library choices, SQLite storage design, Docker Compose layout, endpoint list — DB/storage design implemented in Phase 2 (`db/`); Docker Compose/REST/WebSocket endpoints not built |
| `12_GLASS_BOX_UI_AND_HITL_SPEC.md` | Full 3-stage HITL workflow, UI component blueprint — not built |
| `13_DB_SCHEMA_AND_MODELS.md` | ER diagram and SQLAlchemy 2.0 async models — **implemented in Phase 2**, see `db/models.py` above (Alembic migrations deliberately skipped) |
| `14_PROJECT_DIRECTORY_STRUCTURE.md` | Intended repo layout — see "Intended directory layout" above for current-vs-planned |
| `15_PROVIDER_AUTHENTICATION_AND_ROUTING.md` | Dual auth model per provider, `.env` shape, adapter blueprints — not built |
| `16_MVP_DEVELOPMENT_ROADMAP.md` | 4-phase build order: (1) Analyzer & Knowledge Graph **[done]**, (2) Core/Locking/Git **[done]**, (3) MCP Router & Sandbox, (4) Glass Box UI & HITL |
| `17_YOUTUBE_SERIES_AND_OPENSOURCE_PLAN.md` | Content/marketing plan for a companion YouTube devlog series (not architecture) |
| `18_YOUTUBE_PRODUCTION_AND_EDITING_GUIDE.md` | Video production/editing playbook for the same series (not architecture) |

## Language note

The docs and README are written in Hungarian. Code identifiers, comments, and commit messages in this project are in **English** (the convention established by Phase 1's implementation).
