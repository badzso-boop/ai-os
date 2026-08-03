# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Repository status

**Phase 1 — Polyglot Analyzer & Knowledge Graph — is implemented and stable.** Everything under `ai_os/analyzer/`, `ai_os/knowledge/`, `ai_os/registry.py`, `ai_os/cli.py` is real, working code.

**Phase 2 — Orchestrator Core & Git Engine — is also implemented and stable.** Everything under `ai_os/core/` (`models.py`, `db/`, `lock_manager.py`, `staging.py`, `planner.py`) is real, working code.

**Phase 3a — MCP Provider Adapters & Router — is implemented and stable.** Everything under `ai_os/mcp/adapters/`, `ai_os/mcp/protocol_router.py`, `ai_os/mcp/config.py` is real, working code, verified against real providers.

**Phase 3b — Ephemeral Sandbox & MCP Tool Server — is also implemented and stable.** `ai_os/sandbox/` (Docker-hardened validation runner, doc 10), `ai_os/mcp/mcp_server.py` (the real JSON-RPC/MCP tool server, doc 07 §3/§4), and `ai_os/core/task_runner.py` (the end-to-end orchestration loop with retries + HITL escalation) are all real, working, automated-tested code. Verified live end-to-end by the user: a real Claude model autonomously called the MCP tools, fixed real code, the sandbox validated it, and it merged to main.

**Phase 4a — Epic Decomposition & Multi-Model Distribution — is implemented and stable.** `ai_os/core/epic_planner.py` (LLM decomposes a high-level request into a validated `TaskNode` DAG), `ai_os/core/scheduler.py` (`DynamicScheduler`: risk_level → provider+model), `ai_os/core/epic_runner.py` (runs the DAG batch-by-batch, tasks within a batch concurrent), and the completion-based agent executor in `task_runner.py` are all real, automated-tested code. **`ui/` (Phase 4b — the React Glass Box UI) is NOT built yet.** The plan-review HITL gate exists at the CLI level (`ai-os epic run` prompts for approval before executing), not as a web UI. **Scope boundary (unchanged from 3b)**: autonomous MCP tool-calling only works through the Anthropic CLI-session adapter; Gemini/OpenRouter/Anthropic-API-key tasks use the completion write-back path instead (the model returns full file contents, AI-OS writes them). Check the filesystem before assuming a module/command exists.

### Build / test

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/pytest -q          # 204 tests (203 passed + 1 documented xfail), ~30s (real Docker containers) — never makes a real LLM/network-to-a-provider call
cp .env.example .env         # fill in whichever provider credentials you have
.venv/bin/ai-os llm list     # shows which providers are actually configured
.venv/bin/ai-os llm test anthropic --prompt "say hi"   # REAL call, consumes real usage/quota
.venv/bin/ai-os task run <project> --task-id T-1 --title "..." --description "..." \
    --target-files "src/foo.py" --language python   # REAL single-task agent run, real usage/quota
.venv/bin/ai-os epic run <project> --prompt "add JWT auth" --language python  # REAL multi-task DAG, real usage/quota
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

## Phase 3a — how it actually works

**Deliberate, discussed-with-the-user deviation from `docs/15_PROVIDER_AUTHENTICATION_AND_ROUTING.md`**: that doc's "Native Web Session" auth mode means scraping `claude.ai`/`chatgpt.com`/`gemini.google.com` browser session cookies/tokens to call their private, undocumented internal APIs, bypassing the paid developer API — a ToS-risk, fragile, reverse-engineering approach. **Not built, on purpose.** For Anthropic specifically there's a legitimate official alternative instead: the `claude` CLI, already installed and OAuth-logged-in on this machine, supports non-interactive scripting (`-p`/`--print`, `--output-format json`) that consumes the user's Claude subscription's included usage through Anthropic's own sanctioned interface — see `anthropic_adapter.py` below. The OpenAI/ChatGPT adapter was skipped entirely (not requested, and its only doc-specified auth mode is the same risky scraping approach). Gemini's web-session-cookie mode was also skipped (not requested, same concern) — only its API-key mode is built.

### Module map (`ai_os/mcp/`)

- **`adapters/base_adapter.py`** — shared pydantic contract (mirrors `ai_os/core/models.py`'s role in Phase 2): `TokenUsage`, `LLMTaskRequest` (task_id, system_prompt, context_payload, optional `model` override), `LLMTaskResponse` (task_id, provider, model_name, generated_text, usage), and the `BaseMCPAdapter` ABC (`async execute_task(request) -> LLMTaskResponse`).
- **`adapters/anthropic_adapter.py`** — `AnthropicAdapter`, two modes. **CLI session mode** (`use_cli_session=True`): shells out to `claude -p <prompt> --output-format json --model <model> --permission-mode plan --disallowedTools "Bash Edit Write NotebookEdit Read Glob Grep WebFetch WebSearch"` via `asyncio.create_subprocess_exec` (matching `staging.py`'s subprocess pattern) — the permission lockdown matters: this adapter is a pure "prompt in, text out" call, AI-OS's own Git worktree/patch layer is what should mediate real file changes, not a raw spawned Claude Code session. Runs with `cwd` pinned to a lazily-created, per-instance scratch directory (not the repo) so the CLI doesn't auto-load CLAUDE.md/project context (confirmed empirically: ~15K wasted cache-creation tokens on a trivial prompt otherwise) — the `--bare` flag would also suppress this but forces API-key-only auth, defeating the point of session mode, so it's deliberately not used. Parses the verified real JSON shape: `result`→`generated_text`, `usage.input_tokens`/`usage.output_tokens`, `total_cost_usd`→`estimated_usd_cost`. **API-key mode** (`api_key=...`): standard `httpx` POST to `api.anthropic.com/v1/messages`.
- **`adapters/gemini_adapter.py`** — `GeminiAdapter`, Google AI Studio API-key mode only, via `httpx` against `generativelanguage.googleapis.com`. System prompt goes in a separate top-level `systemInstruction` field (camelCase), not prepended into user text. Default model verified against Google's current docs at implementation time (not doc 15's stale `gemini-1.5-flash`) — fully overridable via constructor/`request.model` either way, so a future rename is a one-line config fix, not a redesign. Handles Gemini's empty-`candidates` safety-filter-block failure mode explicitly (surfaces `promptFeedback.blockReason`) rather than a raw `KeyError`.
- **`adapters/openrouter_adapter.py`** — `OpenRouterAdapter`, new (not in any doc). OpenAI-compatible chat-completions schema via `httpx` against `openrouter.ai/api/v1/chat/completions`. **Deliberately has no hardcoded default model** — unlike the other two adapters, `execute_task` raises `ValueError` if neither `request.model` nor a constructor default is set, since OpenRouter's whole value is caller-driven model choice across many providers. Optional `HTTP-Referer`/`X-Title` attribution headers, sent only when configured.
- **`protocol_router.py`** — `ProtocolRouter`: a thin registry of configured adapters + a static, overridable risk-level→provider preference order (`DEFAULT_RISK_PROVIDER_ORDER`). **Deliberately NOT** doc 02 §2.2's full Dynamic Scheduler (no TPM/RPM tracking, no cost-based backoff) — building real rate-limit-aware scheduling without real usage data to tune it against would be premature. `execute(provider, request)` for explicit choice, `execute_for_risk(risk_level, request)` picks the first configured provider in that risk level's preference list.
- **`config.py`** — `load_configured_adapters()`: reads `.env` (via `python-dotenv`, real env vars always win) + the environment, builds only the adapters with actual evidence of credentials — Anthropic session mode requires the `claude` binary to actually resolve via `shutil.which` (not just an env flag), Gemini/OpenRouter require their API key env var to be non-empty. Silently omits unconfigured providers rather than raising; callers decide what to do about a missing provider.

### CLI surface (`ai-os llm ...`)

```
ai-os llm list                                            # which providers are configured
ai-os llm test <provider> --prompt "..." [--system ...] [--model ...]   # REAL call, real usage/quota
```
`ai-os llm test` is the manual, human-run verification tool (mirrors Phase 2's demo-script precedent) — the automated pytest suite never calls a real provider; adapter tests use `httpx.MockTransport` (HTTP adapters) or a fake stub executable (Anthropic CLI mode) instead.

### Configuration

Copy `.env.example` to `.env` (gitignored) and fill in whichever providers are actually available — see that file for the exact variable names (`ANTHROPIC_MODE`/`ANTHROPIC_API_KEY`/`ANTHROPIC_CLI`, `GEMINI_API_KEY`, `OPENROUTER_API_KEY`/`OPENROUTER_DEFAULT_MODEL`/`OPENROUTER_SITE_URL`/`OPENROUTER_APP_TITLE`).

### Explicitly out of scope for Phase 3a (not silently dropped — see doc 16 §3 for what's still needed)

- The OpenAI/ChatGPT adapter and any local/Ollama adapter — not requested.
- Real TPM/RPM rate-limit tracking / cost-based backoff in `protocol_router.py` (doc 02 §2.2).

---

## Phase 3b — how it actually works

Built by 2 parallel agents (sandbox, MCP server) against interface contracts fixed up front, then the orchestration loop (`task_runner.py`) done directly, same pattern as Phases 1/2/3a. **Explicit constraint honored throughout**: no real `claude` CLI invocation or live LLM call was made by me anywhere in this phase's development — real Docker containers and a real MCP client↔server protocol round-trip provide the automated proof instead; the actual "real LLM fixes real code" run is a deliberately manual, human-run step (see `ai-os task run` below).

### Two verified, deliberate deviations from the docs

- **`ai_os/sandbox/container_runner.py` shells out to the `docker` CLI via `asyncio.create_subprocess_exec`**, not the synchronous `docker` Python SDK doc 10 §4's blueprint uses (wrapped in `loop.run_in_executor`) — matches `staging.py`'s established convention for every other external process (git) in this codebase, and avoids a second heavyweight SDK.
- **`ai_os/mcp/mcp_server.py` uses the real `mcp` PyPI package (`mcp>=2.0,<3`)**, reversing this project's usual "avoid heavy SDKs, use raw httpx" instinct (Phase 3a) — deliberately. Verified hard facts: `mcp` requires `starlette`+`sse-starlette`+`anyio` unconditionally even for pure-stdio use (a real dependency-weight cost), but the MCP wire protocol has real, evolving complexity — 5 protocol revisions exist as of writing, and the newest changed the transport model itself — and a hand-rolled server could silently speak the wrong shape against whatever revision the installed `claude` CLI negotiates, with no live-LLM test available to catch that. Correctness won over leanness here. The installed `mcp` 2.0.0's low-level `Server` API is constructor-based (`on_list_tools=`, `on_call_tool=` kwargs), not the decorator style (`@server.list_tools()`) shown in older tutorials — verify against the actually-installed version before assuming API shape if upgrading.

### Module map

- **`ai_os/sandbox/log_parser.py`** — `strip_ansi_codes()` (doc 10's own regex) + `build_feedback()` returning a generic `{status, exit_code, summary, output}` envelope (output truncated keeping the *tail*, since errors are almost always at the end of a log). **Deliberately not building** doc 10 §3's per-toolchain structured error parsing (`{file, line, column, rule, message}` for `tsc`, etc.) — a distinct, high-maintenance parser per compiler/linter/test-runner for marginal benefit over what an LLM can already read from clean text.
- **`ai_os/sandbox/container_runner.py`** — `EphemeralSandboxRunner.run_validation(worktree_path, language) -> ValidationResult`. Applies doc 10 §1.1's hardening faithfully: `--rm`, `-v <worktree>:/app:ro`, `--network none`, `--memory=2g --cpus=2.0`, `--tmpfs /tmp:rw,noexec,nosuid,size=256m`, `--cap-drop=ALL --user 1000:1000`, unique `--name` for timeout-kill targeting. `SANDBOX_PROFILES` covers python/javascript/typescript/java (doc 10 §2 + JS alongside TS, matching Phase 1's own JS/TS sibling treatment) — Java is a real entry but not exercised by automated tests (heavy image, shared host). Tests use real Docker to prove `--network none` genuinely blocks an outbound connection and the read-only mount genuinely blocks a write — not just that the flags were passed.
  - **Real bug caught and fixed post-hoc, worth knowing about**: since every validation run is `--network none`, a `pip install` *inside* the container can never reach PyPI — vanilla `python:3.12-slim` has no `pytest`, so the naive profile (doc 10's own blueprint has the identical flaw) would always fail with "pytest: command not found", regardless of the project. Fixed by baking `pytest` into a locally-built image ahead of time, with network access, once: `docker/python-sandbox.Dockerfile` → `docker build -t ai-os-sandbox-python:3.12 -f docker/python-sandbox.Dockerfile .` (a one-time setup step — the sandbox profile now points at `ai-os-sandbox-python:3.12`, not raw `python:3.12-slim`). **Known, still-open gap**: a project's own third-party dependencies beyond pytest still can't install inside the network-isolated run for the same reason — a real fix needs a "build a task-specific image with network, then validate network-free" two-phase flow, not built yet. For now, Python tasks validated by `ai-os task run` need tests that only require the standard library + pytest.
- **`ai_os/mcp/mcp_server.py`** — `ServerConfig.from_env()` reads `AI_OS_WORKTREE_PATH` (required)/`AI_OS_GRAPH_JSON_PATH`/`AI_OS_SANDBOX_LANGUAGE` (both optional, degrade only their one dependent tool). `propose_file_patch` writes into the given worktree path with path-traversal rejection (`..`/absolute-path escapes) — it does **not** create/destroy worktrees itself (doc 07 §4's blueprint duplicates that; the real lifecycle is `ai_os.core.staging.GitStagingEngine`, Phase 2). `fetch_symbol_definition` looks up a `KnowledgeEngine` node's `"stub"` by FQN. `trigger_sandbox_validation` calls `EphemeralSandboxRunner`. Tested both in-process (fast, fake sandbox runner) and via one real stdio subprocess round-trip using the `mcp` SDK's own client machinery (`initialize` → `tools/list` → `tools/call`, no LLM involved).
- **`ai_os/core/task_runner.py`** (new, not in doc 14's tree — same flagged-deviation treatment as Phase 2's `core/models.py`) — `TaskRunner.run_task(task, language)`: acquire locks → create worktree → build Context Cache → loop up to `task.max_retries + 1` attempts calling an injectable `AgentTurnExecutor` then validating in the sandbox, feeding the previous attempt's (clean) output back into the next attempt's `AgentTurnContext` (the actual "prompt feedback loop") → merge on success, `abandon_task` + report `BLOCKED` on exhaustion (HITL escalation — just a status value today, nothing renders it, that's Phase 4). `build_claude_cli_agent_turn_executor` is the real production executor: spawns `claude -p ... --mcp-config ... --strict-mcp-config --allowedTools "mcp__ai_os__propose_file_patch mcp__ai_os__fetch_symbol_definition mcp__ai_os__trigger_sandbox_validation"` — unlike Phase 3a's adapter (zero tools, pure completion), this grants exactly the 3 AI-OS MCP tools. **This exact `--allowedTools` MCP-name-prefix convention has not been live-verified against a real `claude` CLI run** (per the no-manual-testing constraint) — it's the first thing to check on your first `ai-os task run`.
- **CLI**: `ai-os task run <project> --task-id ... --title ... --description ... --target-files "a.py,b.py" --language python [--risk-level] [--max-retries] [--model]` — builds a `TaskNode`, scans the project fresh for a Context Cache, and runs it through the real pipeline. **Makes real `claude` CLI calls, consumes real usage** — the deliberate manual verification step for this phase, exactly like `ai-os llm test` was for Phase 3a.

### Explicitly out of scope for Phase 3b (not silently dropped)

- Tool-calling loops for Gemini/OpenRouter/Anthropic-API-key — only the Anthropic CLI-session path has real autonomous tool use (see "Repository status" above).
- The Java sandbox profile isn't exercised by automated tests (heavy image, shared host) — the profile entry is real and correct, just untested here.
- `TaskRunner`'s `on_status_change` hook is a plain callback, not wired to Phase 2's `TaskModel`/DB by default — deliberately thin (avoids assuming a `TaskModel` row already exists, since `epic_id` is a non-null FK); callers wire real persistence themselves.
- Resuming a task after a process crash mid-retry-loop — matches `staging.py`'s existing "start fresh" policy.
- The full Glass Box UI/HITL web flow (Phase 4b) — the plan-review gate exists at the CLI level (Phase 4a); nothing renders the DAG/logs/diffs in a browser yet.

---

## Phase 4a — how it actually works

Closes the gap between "run one hand-specified task" (Phase 3b's `ai-os task run`) and "give it a high-level request and have it split the work across models" (`ai-os epic run`). Built directly (not via parallel agents — the pieces form a tight scheduler → executor → planner → runner chain).

### Module map

- **`ai_os/core/scheduler.py`** — `DynamicScheduler`: maps a task's `risk_level` to a concrete `Assignment(provider, model)`. Reuses `ProtocolRouter.resolve_provider` (Phase 3a) for the provider half and adds the missing model half via `DEFAULT_MODEL_MATRIX` (anthropic: LOW→`haiku`, MEDIUM/HIGH→`sonnet`, CRITICAL→`opus` — CLI aliases, so they never go stale on a model rename; gemini/openrouter default to `None` = the adapter's own default). Overridable per cell via `AI_OS_MODEL_<PROVIDER>_<RISK>` env vars. `planning_assignment()` routes decomposition itself as CRITICAL (strongest configured model). **Still deliberately NOT the full doc 02 §2.2 scheduler** — no TPM/RPM tracking or cost-based backoff (same deferral `protocol_router.py` documents).
- **`ai_os/core/epic_planner.py`** — `decompose(user_prompt, engine, adapter, model)`: builds a compact repo summary from the `KnowledgeEngine` (files + symbol FQNs, to ground task paths in real files), prompts the LLM for a JSON task array, and parses it defensively. `parse_task_plan` strips markdown fences, extracts the outermost `[...]`, `json.loads`, tolerates extra keys, and pydantic-validates each entry into a `TaskNode`; `EpicPlanError` carries the raw text. `decompose` retries once with the parse error fed back to the model (mirroring the sandbox feedback loop), then runs the plan through the deterministic `planner.build_graph`/`validate_acyclic` to reject cycles/dangling deps before anything executes.
- **completion-based executor (`task_runner.py`)** — `build_completion_agent_turn_executor(adapter, model)`: for providers WITHOUT autonomous tool use (Gemini/OpenRouter/Anthropic-API-key), sends task+context+(on retry)previous-validation-output as a plain completion, asks for full new file contents in a sentinel-delimited format (`<<<AI_OS_FILE: path>>> ... <<<AI_OS_END>>>` — deliberately NOT markdown fences, which collide with code containing ```), and AI-OS itself writes the parsed files into the worktree (with the same path-traversal rejection as `mcp_server.py`). The existing `TaskRunner` sandbox-validation + retry loop then works identically. **Known limitation, flagged**: asking for full file contents means very large files can hit the model's output limit / truncate — fine for the focused single-responsibility tasks the DAG produces; a diff-based protocol would lift it, not built.
- **`ai_os/core/epic_runner.py`** — `EpicRunner.run_epic(tasks)`: `planner.topological_batches` gives dependency generations; tasks within a generation run concurrently via `asyncio.gather` **sharing one `LockManager` + one `GitStagingEngine`** (so Phase 2's concurrency guarantees apply directly — the integration test in `test_epic_runner.py` proves a real diamond DAG merges in dependency order against real git worktrees). Each task is routed by the scheduler and gets the right executor kind (anthropic-CLI-session → real MCP tool use; everything else → completion write-back). Between generations the repo is re-scanned so later tasks see earlier merged work. A task whose dependency ended `BLOCKED` (or was itself skipped) is skipped, not run.
- **CLI**: `ai-os epic run <project> --prompt "..." --language python [--yes]` — decomposes, prints the proposed DAG as a Rich table (id, risk, provider→model, deps, write-set), and **pauses for approval (HITL Stage 1, doc 12 §2.1) unless `--yes`** before executing. Makes real LLM calls (planning + every task).

### Testing (no real LLM/Docker anywhere in the automated suite)

`test_scheduler.py` (pure routing), `test_completion_executor.py` (parser + fake-adapter write-back, path-traversal rejection), `test_epic_planner.py` (canned plan JSON incl. malformed-then-valid retry, cycle rejection), `test_epic_runner.py` (**real** `LockManager`+`GitStagingEngine`+disposable git repo, fake completion adapter, fake sandbox — proves a diamond DAG executes in dependency order, per-risk model routing is observable, and a blocked task's dependents are skipped), `test_cli_epic.py` (plan-review gate: declined→no execution, approved/`--yes`→executes).

### Explicitly out of scope for Phase 4a (not silently dropped)

- Tool-calling loops for non-Anthropic-CLI providers — unchanged from Phase 3b; those use the completion write-back path.
- Diff-based patching for large files (completion executor sends whole files).
- The React Glass Box UI, WebSocket streaming, runtime preemption (Stage 2), Monaco manual-edit recovery (Stage 3) — Phase 4b.
- Real TPM/RPM/cost-based scheduling (doc 02 §2.2) — still deferred.

---

## Intended directory layout

Per `docs/14_PROJECT_DIRECTORY_STRUCTURE.md`:
- `ai_os/analyzer/`, `ai_os/knowledge/`, `ai_os/registry.py`, `ai_os/cli.py` — **implemented (Phase 1)**, described above.
- `ai_os/core/` — `models.py`, `db/` (`database.py`, `models.py`), `lock_manager.py`, `staging.py`, `planner.py` (Phase 2) + `scheduler.py` (Dynamic Scheduler / risk→model, Phase 4a) — **implemented**, described above.
- `ai_os/mcp/` — `protocol_router.py`, `config.py`, `adapters/base_adapter.py`/`anthropic_adapter.py`/`gemini_adapter.py`/`openrouter_adapter.py` (Phase 3a) + `mcp_server.py` (Phase 3b) — **all implemented**, described above. An OpenAI/local adapter — not requested, not built.
- `ai_os/sandbox/` — `container_runner.py`, `log_parser.py` — **implemented (Phase 3b)**, described above.
- `ai_os/core/task_runner.py` (Phase 3b), `ai_os/core/epic_planner.py` + `ai_os/core/epic_runner.py` (Phase 4a) — **implemented**, described above. Not in doc 14's original tree — deliberate additions.
- `ui/` — React + Vite + Tailwind + React Flow + Monaco frontend (Phase 4b) — **not yet created**.

When starting Phase 3+ implementation, follow this structure unless there's a concrete reason to deviate, since the docs (and Phase 1/2's own test suites) assume it.

## Where to look for detail (docs/, still authoritative for anything not implemented)

| Doc | Subsystem |
| --- | --- |
| `01_ARCHITECTURE_OVERVIEW.md` | Full system diagram, end-to-end flow, responsibility matrix |
| `02_ORCHESTRATOR_CORE.md` | DAG Planner (`TaskNode` schema), Dynamic Scheduler (risk→model matrix), async `LockManager` — **TaskNode/planner/lock_manager in Phase 2, LLM decomposition + risk→model scheduler in Phase 4a** (`epic_planner.py`/`scheduler.py`); TPM/RPM/cost-based scheduling still not built |
| `03_POLYGLOT_ANALYZER.md` | Tree-sitter language support, symbol/call-graph extraction, incremental re-parse on file change (Phase 1 implements the non-incremental parts; incremental re-parse is not built) |
| `04_KNOWLEDGE_CONTEXT_ENGINE.md` | Knowledge Graph node/edge types, skeleton/stub context compression, event-driven cache invalidation (Phase 1 implements the graph + stubs; event-driven invalidation is not built) |
| `05_EXECUTION_VALIDATION_SANDBOX.md` | Git worktree isolation + ephemeral Docker validation + feedback/HITL state machine — worktree isolation (Phase 2) + Docker sandbox (Phase 3b) + retry loop (`task_runner.py`) all **implemented**; full HITL UI (Phase 4) not built |
| `06_GLASS_BOX_UI.md` | Observability dashboard concept, WebSocket event shape, HITL control panel options — not built |
| `07_MCP_ADAPTER_ROUTER.md` | Kernel-vs-cores framing, exact MCP JSON-RPC tool schemas, Python blueprint — **implemented**: adapter/routing half in Phase 3a, the JSON-RPC tool-server half in Phase 3b (`mcp_server.py`) |
| `08_KNOWLEDGE_GRAPH_AND_SUBGRAPH_EXTRACTION.md` | Full graph schema, k-hop subgraph algorithm, skeleton extractor — **implemented in Phase 1**, see `graph_engine.py`/`skeleton_extractor.py` above |
| `09_GIT_WORKTREE_STAGING_ENGINE.md` | Worktree lifecycle, async merge queue, rebase/re-validate rule — **implemented in Phase 2**, see `staging.py` above |
| `10_EPHEMERAL_CONTAINER_SANDBOX_SPEC.md` | Container hardening flags, per-language sandbox profile matrix — **implemented in Phase 3b**, see `sandbox/container_runner.py` above (structured per-toolchain error parsing deliberately not built, see `log_parser.py` note) |
| `11_ORCHESTRATOR_TECH_STACK_AND_DEPL.md` | Language/library choices, SQLite storage design, Docker Compose layout, endpoint list — DB/storage design implemented in Phase 2 (`db/`); Docker Compose/REST/WebSocket endpoints not built |
| `12_GLASS_BOX_UI_AND_HITL_SPEC.md` | Full 3-stage HITL workflow, UI component blueprint — not built |
| `13_DB_SCHEMA_AND_MODELS.md` | ER diagram and SQLAlchemy 2.0 async models — **implemented in Phase 2**, see `db/models.py` above (Alembic migrations deliberately skipped) |
| `14_PROJECT_DIRECTORY_STRUCTURE.md` | Intended repo layout — see "Intended directory layout" above for current-vs-planned |
| `15_PROVIDER_AUTHENTICATION_AND_ROUTING.md` | Dual auth model per provider, `.env` shape, adapter blueprints — **implemented in Phase 3a with a deliberate deviation**: the "Native Web Session" cookie-scraping mode was rejected as ToS-risky and replaced with the official `claude` CLI scripting interface for Anthropic; see "Phase 3a" above |
| `16_MVP_DEVELOPMENT_ROADMAP.md` | 4-phase build order: (1) Analyzer & Knowledge Graph **[done]**, (2) Core/Locking/Git **[done]**, (3) MCP Router & Sandbox **[done]**, (4) Glass Box UI & HITL **[4a done: Epic decomposition + multi-model distribution + CLI plan-review gate; 4b (React UI/WebSocket/Monaco) not built]** |
| `17_YOUTUBE_SERIES_AND_OPENSOURCE_PLAN.md` | Content/marketing plan for a companion YouTube devlog series (not architecture) |
| `18_YOUTUBE_PRODUCTION_AND_EDITING_GUIDE.md` | Video production/editing playbook for the same series (not architecture) |

## Language note

The docs and README are written in Hungarian. Code identifiers, comments, and commit messages in this project are in **English** (the convention established by Phase 1's implementation).
