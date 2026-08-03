# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Repository status

**Phase 1 (Polyglot Analyzer & Knowledge Graph) is implemented** — `ai_os/analyzer/`, `ai_os/knowledge/`, `ai_os/registry.py`, `ai_os/cli.py` exist on disk with a passing pytest suite under `tests/`. Everything else described below (`core/`, `mcp/`, `sandbox/`, `ui/`) is still just the *planned* architecture from `docs/`, which remains the single source of truth for anything not yet built. Do not assume a module/command/test exists beyond what's listed here — check the filesystem before relying on paths from the docs.

Build/lint/test:
- `.venv/bin/pip install -e ".[dev]"` — isolated venv, never touch system Python (shared host).
- `.venv/bin/pytest -q` — 34 tests covering the analyzer/knowledge/registry/CLI layers.
- No `mypy`/`flake8`/`npm test`/`tsc` configured yet — don't treat those as working commands until added.

### CLI (Phase 1, stable — see `ai_os/cli.py`)
```
ai-os project add <name> <path> [--force]
ai-os project remove <name>
ai-os project list
ai-os scan <name-or-path> [--out graph.json] [--max-hops N] [--languages ...] [--exclude DIR] [--skeleton FQN] [--json]
```
`ai-os scan --out graph.json` writes the full Knowledge Graph (node-link JSON, `networkx.json_graph`, pretty-printed) to disk. This is a **first-class, standalone feature** — it does not depend on the (not-yet-built) Orchestrator Core and is meant to be run directly by a human, not just consumed internally later. Keep it working as `KnowledgeEngine.to_json()`/`from_json()` evolve; don't fold it behind orchestrator-only plumbing.

## What AI-OS is

**AI-OS** is a planned **AI Software Engineering Orchestrator** — not a coding assistant or IDE plugin. It acts like an operating system kernel that schedules and supervises AI coding agents (Claude, GPT, Gemini, DeepSeek, local Ollama/vLLM models) as interchangeable "execution cores," while a fully deterministic Python core handles everything that doesn't require AI judgement.

Core philosophy (drives every design decision in the docs):
1. **Compiler First** — never spend AI tokens on anything an algorithm/compiler can do 100% deterministically (AST parsing, dependency/call graphs, type/lint checks).
2. **Knowledge Before Generation** — agents never see the whole codebase; they get a compressed `Context Cache` containing only the symbols/interfaces/types relevant to their task, built from a Knowledge Graph.
3. **Model-Agnostic & Cost-Aware** — all LLM communication goes through standardized MCP adapters; a Dynamic Scheduler picks the cheapest/most reliable model per task based on risk classification.

## Planned system architecture

Four layers plus an observability surface (see `docs/01_ARCHITECTURE_OVERVIEW.md` for the full mermaid diagram):

- **Orchestrator Core** (Python 3.12+, `asyncio`) — `DAG Planner` → `Dynamic Scheduler` → `Lock Manager` → `Agent Task Runner`. 100% deterministic; owns state machines, DAG dependency resolution, file locking, and MCP server/client plumbing. Framed in the docs as "Ring 0 kernel" vs. LLMs as "Ring 3 user-space processes" (doc 07).
- **Deterministic analysis layer** — `Polyglot Analyzer` (Tree-sitter based, 0 AI tokens) extracts AST/symbols/call graphs from the repo and feeds the `Knowledge Engine`.
- **Knowledge & context layer** — Knowledge Graph (NetworkX/Neo4j) + event-driven `Context Cache`; k-hop subgraph extraction + skeleton/stub extraction produce the compressed context sent to agents (80-90% token savings, per doc 08).
- **Execution & Validation Sandbox** — each task runs in an isolated **Git Worktree** on the host, then gets validated inside a **hardened, ephemeral Docker/Podman container** (`--net none`, read-only mount, resource limits, non-root). Failing validation triggers a Prompt Feedback Loop; repeated failure escalates to Human-in-the-Loop (HITL).
- **Glass Box UI** — React + WebSockets (and/or a Rich/Textual CLI dashboard) for real-time observability: DAG state, active locks, live agent logs/diffs, cost/token tracking, and the 3-stage HITL controls (plan review, runtime preemption, failure recovery with a Monaco editor).

### End-to-end task flow
User request → DAG Planner decomposes into `TaskNode`s with declared `read_set`/`write_set` → Polyglot Analyzer keeps the Knowledge Graph current → Context Cache builds the minimal prompt context → Lock Manager checks/acquires file locks → Dynamic Scheduler assigns the cheapest adequate model over MCP → agent executes inside its own Git worktree → Docker container runs compiler/lint/tests → on success: rebase-check against `main`, re-validate if `main` moved, then fast-forward merge and worktree cleanup; on failure: feedback loop / HITL.

### Deterministic vs. heuristic responsibility split (doc 01 §3)
Deterministic (Python core, 0 tokens): AST/symbol extraction, dependency/call graphs, file locking, compilation/test execution, model/cost selection rules.
Heuristic (LLM): task decomposition into a DAG, actual code writing/refactoring, merge-conflict resolution content.

## Where to look for detail

Each doc in `docs/` is authoritative for its subsystem — read the specific one before implementing that area rather than relying on this summary:

| Doc | Subsystem |
| --- | --- |
| `01_ARCHITECTURE_OVERVIEW.md` | Full system diagram, end-to-end flow, responsibility matrix |
| `02_ORCHESTRATOR_CORE.md` | DAG Planner (`TaskNode` schema), Dynamic Scheduler (risk→model matrix), async `LockManager` implementation |
| `03_POLYGLOT_ANALYZER.md` | Tree-sitter language support, symbol/call-graph extraction, incremental re-parse on file change |
| `04_KNOWLEDGE_CONTEXT_ENGINE.md` | Knowledge Graph node/edge types, skeleton/stub context compression, event-driven cache invalidation |
| `05_EXECUTION_VALIDATION_SANDBOX.md` | Git worktree isolation + ephemeral Docker validation + feedback/HITL state machine |
| `06_GLASS_BOX_UI.md` | Observability dashboard concept, WebSocket event shape, HITL control panel options |
| `07_MCP_ADAPTER_ROUTER.md` | Kernel-vs-cores framing, exact MCP JSON-RPC tool schemas (`propose_file_patch`, `fetch_symbol_definition`, `trigger_sandbox_validation`), Python blueprint |
| `08_KNOWLEDGE_GRAPH_AND_SUBGRAPH_EXTRACTION.md` | Full graph schema, k-hop subgraph algorithm, skeleton extractor, reference `KnowledgeEngine` implementation |
| `09_GIT_WORKTREE_STAGING_ENGINE.md` | Worktree lifecycle state machine, async merge queue, rebase/re-validate rule, conflict-resolution-as-a-task pattern, `GitStagingEngine` blueprint |
| `10_EPHEMERAL_CONTAINER_SANDBOX_SPEC.md` | Container hardening flags, per-language sandbox profile matrix, ANSI log cleanup, `EphemeralSandboxRunner` blueprint |
| `11_ORCHESTRATOR_TECH_STACK_AND_DEPL.md` | Language/library choices, SQLite storage design, Docker Compose layout, REST/WebSocket/MCP endpoint list, `main.py` blueprint |
| `12_GLASS_BOX_UI_AND_HITL_SPEC.md` | Full 3-stage HITL workflow (plan review / runtime preemption / failure recovery), UI component blueprint |
| `13_DB_SCHEMA_AND_MODELS.md` | ER diagram and SQLAlchemy 2.0 async models (`epics`, `tasks`, `lock_audits`, `token_costs`, `graph_nodes`, `graph_edges`) |
| `14_PROJECT_DIRECTORY_STRUCTURE.md` | The intended repo layout (`ai_os/`, `ui/`, `docker/`) and `pyproject.toml` dependency list — treat as the target structure, not current state |
| `15_PROVIDER_AUTHENTICATION_AND_ROUTING.md` | Dual auth model per provider (developer API key vs. native web/OAuth session transport), `.env` shape, adapter blueprints |
| `16_MVP_DEVELOPMENT_ROADMAP.md` | 4-phase build order: (1) Analyzer & Knowledge Graph, (2) Core/Locking/Git, (3) MCP Router & Sandbox, (4) Glass Box UI & HITL — with acceptance criteria per phase |
| `17_YOUTUBE_SERIES_AND_OPENSOURCE_PLAN.md` | Content/marketing plan for a companion YouTube devlog series (not architecture) |
| `18_YOUTUBE_PRODUCTION_AND_EDITING_GUIDE.md` | Video production/editing playbook for the same series (not architecture) |

## Intended directory layout

Per `docs/14_PROJECT_DIRECTORY_STRUCTURE.md`, the eventual code layout is:
- `ai_os/core/` — planner, scheduler, lock_manager, staging (Git worktree engine), `db/` (SQLAlchemy models) — **not yet created**
- `ai_os/analyzer/` — `tree_sitter_engine.py`, `call_graph_builder.py`, `languages.py`, `queries/*.scm` — **implemented (Phase 1)**
- `ai_os/knowledge/` — `graph_engine.py` (NetworkX), `skeleton_extractor.py` — **implemented (Phase 1)**; `event_bus.py` — **not yet created** (no file-watcher daemon yet, scans are one-shot CLI invocations)
- `ai_os/mcp/` — `mcp_server.py`, `protocol_router.py`, `adapters/` (per-provider: anthropic, openai, gemini, local) — **not yet created**
- `ai_os/sandbox/` — `container_runner.py`, `log_parser.py` — **not yet created**
- `ui/` — React + Vite + Tailwind + React Flow + Monaco frontend — **not yet created**

When starting implementation, follow this structure unless there's a concrete reason to deviate, since the docs (agents, tests, and any future contributor) assume it.

## Language note

The docs and README are written in Hungarian; code identifiers, comments, and commit messages in this project should follow whatever convention is established once code exists (check for a stated preference before defaulting to either language).
