# AI-OS: AI Software Engineering Orchestrator

> **AI Build System & Software Engineering Orchestrator** — an operating system for deterministic, AI-driven orchestration of the modern software development lifecycle.

---

## 💡 Project Vision

**AI-OS** is not another coding assistant or IDE plugin (like GitHub Copilot or Cursor). It is an **AI Software Engineering Orchestrator** that supervises the whole development lifecycle like an operating-system kernel.

In this architecture the various LLMs (Claude, OpenAI, Gemini, DeepSeek, local Ollama/vLLM models) act merely as **interchangeable execution cores (CPU cores)**. The **Orchestrator Core** schedules those cores, enforces the safety boundaries, manages file locking, and drives deterministic validation.

---

## 🏛️ Core Philosophy

### 1. ⚙️ Compiler First
The system **never spends AI tokens** on work an algorithm or compiler can do 100% deterministically:
- AST (Abstract Syntax Tree) generation
- Import/export dependency graphs and call graphs
- Type checking, syntax checking, and linting

### 2. 🧠 Knowledge Before Generation
Agents never receive the whole codebase — avoiding context-window flooding and hallucinations. The system builds a compressed **Context Cache** containing only the symbols, interfaces, type definitions, and architectural rules relevant to the task.

### 3. 🔌 Model-Agnostic & Cost-Aware
All model communication goes through standardized **MCP (Model Context Protocol)** adapters. A **Dynamic Scheduler** picks the most optimal (cheapest / most reliable) model per task based on the task's risk classification, with adaptive rate-limit backoff, provider fallback, and an optional per-epic cost cap.

---

## 📦 System Architecture

AI-OS is organized into four main layers plus an observability surface:

```
+-----------------------------------------------------------------------+
|                         Glass Box UI / Dashboard                      |
|              (DAG visualization, agent status, lock monitor)          |
+-----------------------------------------------------------------------+
                                   |
                                   v
+-----------------------------------------------------------------------+
|                         Orchestrator Core (Python)                    |
|  +-------------------+   +--------------------+   +----------------+  |
|  |    DAG Planner    | --> | Dynamic Scheduler  | --> | Lock Manager   |  |
|  +-------------------+   +--------------------+   +----------------+  |
+-----------------------------------------------------------------------+
           |                                  |
           v                                  v
+-----------------------+          +------------------------------------+
|  Polyglot Analyzer    |          |    Knowledge Engine & Cache        |
|  (Tree-sitter parsers)|          |  (NetworkX graph & event bus)      |
+-----------------------+          +------------------------------------+
           |                                  |
           +-----------------+----------------+
                             |
                             v
+-----------------------------------------------------------------------+
|                    Execution & Validation Sandbox                     |
|    +-----------------------+        +----------------------------+    |
|    |  Git Worktrees (Host) | -----> | Ephemeral Docker Containers|    |
|    +-----------------------+        +----------------------------+    |
+-----------------------------------------------------------------------+
```

### Detailed documentation

1. 🏛️ [System Architecture Overview](docs/01_ARCHITECTURE_OVERVIEW.md) — full system architecture, data flows, and event-driven communication.
2. 🧠 [Orchestrator Core Specs](docs/02_ORCHESTRATOR_CORE.md) — DAG Planner, Dynamic Scheduler, and Lock Manager specification.
3. 🔬 [Polyglot Analyzer Engine](docs/03_POLYGLOT_ANALYZER.md) — deterministic parser layer (Tree-sitter, AST, call graph).
4. 📚 [Knowledge Graph & Context Cache](docs/04_KNOWLEDGE_CONTEXT_ENGINE.md) — knowledge graph, software entities, and event-driven cache invalidation.
5. 🛡️ [Execution & Validation Sandbox](docs/05_EXECUTION_VALIDATION_SANDBOX.md) — Git worktrees, ephemeral Docker/Podman containers, and the HITL preemption engine.
6. 📊 [Glass Box UI & Observability](docs/06_GLASS_BOX_UI.md) — real-time CLI/web dashboard for supervising the system.

> The `docs/` specs are written in Hungarian and remain the authoritative design source for anything not yet implemented. `CLAUDE.md` documents exactly what is built today, per phase.

---

## 🛠️ Tech Stack

- **Orchestrator Core**: Python 3.13+ (`asyncio`, `pydantic`, `click`)
- **Deterministic parser**: `py-tree-sitter`, Tree-sitter grammars (Python, Java, JavaScript, TypeScript, HTML, CSS, SQL)
- **Knowledge graph & cache**: `networkx`, in-process polling file-watcher
- **Model protocol**: MCP (Model Context Protocol) client + server (`mcp` SDK) plus native function-calling adapters (Anthropic, Gemini, OpenRouter)
- **Persistence**: async SQLAlchemy 2.0 + SQLite (`aiosqlite`)
- **Execution sandbox**: Git worktrees + ephemeral, hardened Docker containers (pnpm/yarn/npm, Maven, pip; optional Postgres sidecar on an `--internal` network)
- **Integration**: opens PRs via the `gh` CLI (with the DAG in the description)

---

## ✅ Current status

The first four phases of the planned architecture are **implemented, tested, and validated on a real repository**:

| Phase | Scope | Status |
| --- | --- | --- |
| **1** | Polyglot Analyzer & Knowledge Graph | ✅ done |
| **2** | Orchestrator Core, Lock Manager, Git worktree engine | ✅ done |
| **3a** | MCP provider adapters & router | ✅ done |
| **3b** | Ephemeral Docker sandbox & MCP tool server | ✅ done |
| **4a** | Epic decomposition & multi-model distribution | ✅ done |
| **5** | Multi-provider autonomous tool-calling, edit-based patching, cost/lock accounting, adaptive scheduling | ✅ done |
| **4b** | Glass Box UI (React) & full 3-stage HITL web flow | ⛔ not built |

The system works **end-to-end from the command line today**: give it a high-level request, it decomposes the work into a task DAG, routes each task to a model by risk, lets the model autonomously call tools to edit code, validates every change in a sandbox, and **opens a pull request** (or merges to `main` with `--merge-to-main`) — recording token/USD spend along the way. See `CLAUDE.md` for the per-phase "how it actually works" detail and the documented trade-offs/limitations.

**Notable capabilities beyond the phase table:**
- **Pull requests by default** — an epic's tasks gather on a `ai-os/epic-<id>` integration branch and one PR is opened via `gh`, its description containing the decomposed DAG (task → title → risk → routed model → deps → outcome).
- **Sandbox that installs real dependencies** — a two-phase flow installs a project's deps (Python `pip`, Node `pnpm`/`yarn`/`npm` auto-detected, Java `mvn`) with network, then runs the tests network-free; an optional **Postgres sidecar** on a `--internal` network handles DB-backed tests without ever exposing the internet.
- **Project conventions** — a committed `.ai-os/conventions.md` (i18n rules, UI-library gotchas, …) is injected into both the plan and every task's prompt, provider-agnostically.
- **Live observability** — the CLI streams what each task is doing (attempt, routed model + tokens, sandbox pass/fail with output, retries, merges); `-v` shows full logs.
- **Resilience & cost control** — a usage/rate-limit blocks just that task (the PR still finalizes the completed ones), a cheap model summarizes large failure logs before the expensive one retries, adaptive 429 backoff + provider fallback, and an optional per-epic USD cap.
- **Operational hardening** — a cross-run lock stops two epics clobbering the same repo; a task touching CI/secrets/`.ai-os` config is flagged at plan-review and can't be blind-merged to main (it goes through a reviewable PR); a crashed epic *resumes on its existing branch* (keeping completed work); and a BLOCKED task **keeps its branch + surfaces the error in the PR body** so you can see why it failed instead of the code vanishing.
- **Crash resume** — `ai-os epic resume` re-runs only the not-yet-completed tasks of a crashed epic.

---

## 📥 Install

**Requirements:**
- Python **3.13+**
- **Docker** — required for the sandbox validation step (`task run` / `epic run`). Not needed for `scan` / `watch` / `llm` commands.
- The **`claude` CLI**, already logged in, if you want to use your Claude subscription's included usage (the default Anthropic mode). Optional; API keys work too.

```bash
# 1. Clone and create a virtualenv
git clone https://github.com/badzso-boop/ai-os.git
cd ai-os
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"

# 2. Run the test suite (real Docker containers; never makes a real LLM/network call)
.venv/bin/pytest -q          # 399 tests (392 passed + 6 skipped opt-in + 1 documented xfail), ~55s

# 3. One-time: build the sandbox image used to validate Python tasks
#    (bakes pytest in, since the sandbox runs with --network none)
docker build -t ai-os-sandbox-python:3.12 -f docker/python-sandbox.Dockerfile .

# 4. Configure provider credentials (only what you actually have)
cp .env.example .env
# then edit .env — see that file for every variable and what it does
.venv/bin/ai-os llm list     # shows which providers are actually configured
```

> `.env` is gitignored — never commit real credentials. Every value is optional; only providers with real credentials present get configured.

> **Project dependencies in the sandbox.** Validation is a two-phase flow: a project's third-party dependencies are installed in a network-enabled per-task image build, then the tests run against it with `--network none` (so the agent's own code never gets network). This works for **Python** (`requirements.txt`), **Node** (`package.json` — **pnpm / yarn / npm auto-detected** from the lockfile, with the code + `node_modules` baked into one image so Vite/Vitest/Next resolve normally), and **Java** (`pom.xml`). The agent is instructed to add any new dependency it introduces to the manifest. Projects can override the base image (e.g. a Playwright image for e2e) via `.ai-os/sandbox.json`.

> **Database-backed tests.** A project whose tests need a real database declares it in a committed `.ai-os/sandbox.json` (a DB service + migration/seed commands + the connection env). Validation then starts a throwaway DB sidecar on a Docker `--internal` network (reachable by the tests, but with **no route to the internet** — so isolation holds), runs the project's reference-data seed, then the tests. See **[docs/SANDBOX_CONFIG.md](docs/SANDBOX_CONFIG.md)** for the format and Python/Node/Java examples.

For convenience, activate the venv so you can drop the `.venv/bin/` prefix:

```bash
source .venv/bin/activate
ai-os --help
```

---

## 🚀 Usage

AI-OS has two kinds of commands: **deterministic, zero-LLM analysis** (free, offline) and **agentic execution** (makes real LLM calls, consumes real usage/quota). They are clearly separated below.

### A0. Start a new project from zero — `ai-os init`

Scaffold a working baseline (source + a passing test + a wired `.ai-os/sandbox.json`), make it a git repo with an initial `main` commit, and register it — so `ai-os epic run` can build on it immediately. Deterministic (no network / host toolchain at init time).

```bash
ai-os init ./myapp --stack fastapi-react --name myapp   # FastAPI backend + React frontend monorepo
ai-os init ./api   --stack fastapi                      # Python backend only
ai-os init ./web   --stack react                        # Vite + React + TS frontend only
ai-os init ./svc   --stack spring                       # Java Spring Boot (Maven)
ai-os init ./shop  --stack next-prisma                  # Next.js + Prisma (TypeScript)

# --with-db adds a throwaway Postgres sidecar + a migration/schema + a
# reference-data seed + a DB-backed test (fastapi / fastapi-react / spring / next-prisma):
ai-os init ./shop  --stack next-prisma --with-db
ai-os init ./api   --stack fastapi     --with-db
```

For the monorepo, run epics **per language** (one epic can also span both — each task validates with its own sandbox profile, derived from its file extensions):

```bash
ai-os epic run myapp --prompt "add a /users CRUD API with tests" --language python
ai-os epic run myapp --prompt "add a users list page calling the API" --language typescript
```

> **Sweet spot vs. from-scratch.** AI-OS shines on **incremental changes to an existing codebase** (the Context Cache grounds it in real code, the sandbox validates against real tests). `ai-os init` gives you a working baseline so you get that benefit immediately instead of hand-building scaffolding.

### A. Register projects

Project roots live in an external, updatable registry (`~/.ai-os/projects.json`). You can register a name, or just pass a filesystem path directly to any command.

```bash
ai-os project add my-project /path/to/project
ai-os project list
ai-os project remove my-project
```

### B. Analyze a project (deterministic, no LLM, no network)

```bash
# Scan: extract symbols + import/call/inheritance graph, print a report
ai-os scan my-project

# Export the full Knowledge Graph as pretty-printed JSON
ai-os scan my-project --out graph.json

# Scan a subset of languages / skip extra directories
ai-os scan my-project --languages python,typescript --exclude vendor --exclude fixtures

# Print one symbol's signature-only "skeleton stub" (FQN = <relpath>::<QualifiedName>)
ai-os scan my-project --skeleton "src/Foo.java::Foo.getX"
```

### C. Keep the graph fresh while you work — `ai-os watch`

Runs a lightweight polling watcher: on any add / modify / delete of a source file it re-scans the project and (with `--out`) re-writes the graph JSON. Still zero-LLM, zero-network. Runs until `Ctrl-C`.

```bash
# Watch a project, re-writing graph.json on every change (poll every 1s by default)
ai-os watch my-project --out graph.json

# Slower polling, only watch python, skip a directory
ai-os watch my-project --interval 2 --languages python --exclude migrations
```

Example output:

```
Watching /path/to/project (every 1.0s). Initial scan in 0.42s: 128 nodes, 210 edges. Graph -> graph.json.
Press Ctrl-C to stop.
14:03:11 re-scanned (+1 files) -> 130 nodes, 214 edges, graph updated
14:03:25 re-scanned (~1 files) -> 130 nodes, 215 edges, graph updated
```

### C2. Reclaim disk after crashes — `ai-os clean`

AI-OS tears down its sandbox containers/networks on exit, but a hard crash (OOM/SIGKILL) can't — and per-task dependency images accumulate. `ai-os clean` removes AI-OS's own Docker artifacts (only objects matching its naming, never anything else). With a project path it also prunes stale git worktrees; `--branches` deletes leftover `ai-os/*` branches too. Run it when no epic is active.

```bash
ai-os clean --dry-run                    # show what would be removed
ai-os clean --yes                        # remove leaked images/containers/networks
ai-os clean my-project --branches --yes  # also prune worktrees + delete ai-os/* branches
```

### D. Test a provider (⚠️ real LLM call, consumes usage)

```bash
ai-os llm list                                            # which providers are configured
ai-os llm test anthropic --prompt "say hi"                # real call
ai-os llm test openrouter --prompt "say hi" --model "anthropic/claude-sonnet-4.5"
```

### E. Run a single task end-to-end (⚠️ real usage + Docker)

Builds one task, scans the project for a Context Cache, and runs it through the real agent → sandbox-validate → retry → merge pipeline.

```bash
ai-os task run my-project \
  --task-id T-1 \
  --title "Add input validation to parse_config" \
  --description "parse_config must raise ValueError on a missing 'name' key; add a unit test." \
  --target-files "src/config.py,tests/test_config.py" \
  --language python \
  --risk-level MEDIUM \
  --max-retries 3
```

### F. Run a full epic — decompose & distribute across models (⚠️ real usage + Docker)

Decomposes a high-level request into a task DAG, shows the proposed plan (HITL plan-review gate) — **including a rough per-task token/USD estimate for the configured models** so you can gauge spend before approving (Anthropic session tasks show `sub` = subscription usage) — then executes it, routing each task to a model by risk, letting tool-capable providers autonomously call the MCP tools, validating every change in the sandbox, and recording spend.

**By default it opens a pull request**: the epic's tasks are gathered on a per-epic integration branch (`ai-os/epic-<id>`) and one PR is opened via the `gh` CLI. Pass `--merge-to-main` to merge directly instead. If there's no git remote or `gh`, PR mode falls back to a local merge so nothing is lost.

```bash
ai-os epic run my-project --prompt "add JWT authentication" --language python
# review the printed DAG, then approve at the prompt — or skip the gate:
ai-os epic run my-project --prompt "add JWT authentication" --language python --yes
# merge straight to main instead of opening a PR:
ai-os epic run my-project --prompt "add JWT authentication" --language python --merge-to-main
# -v / --verbose: show the FULL sandbox output on a failure (default: just the tail)
ai-os epic run my-project --prompt "add JWT authentication" --language python -v

# Resume a crashed/interrupted epic — re-runs only the tasks that weren't
# already COMPLETED (their merged work is kept). Get the epic id from
# `ai-os epic history`.
ai-os epic resume my-project --epic <epic-id> --language python
```

The run streams live what each task is doing, so it isn't a black box:

```
▶ TASK-4 attempt 1/2 — Serve landing page at root  → apps/web/src/app/page.tsx
  TASK-4 · agent: anthropic→sonnet · 4200in/1100out tok
  ✗ TASK-4 sandbox FAILED (exit 2) — tsc failed
  ╭── TASK-4 sandbox output (tail) ──╮
  │ src/app/page.tsx(12,5): error … │
  ╰──────────────────────────────────╯
  ↻ TASK-4 retrying (attempt 2)
  ✓ TASK-4 sandbox passed (exit 0)
  ✓ TASK-4 merged
```

**Project conventions.** Drop a committed `.ai-os/conventions.md` in your repo (i18n rules, UI-library gotchas, "prefer the service layer over raw SQL", …) and AI-OS injects it into both the decomposition (so the DAG respects it — e.g. adds a translation task) and every task's prompt — provider-agnostic, unlike a `CLAUDE.md` only the `claude` CLI auto-loads.

### G. Read back accounting

Every `epic run` records per-task token/USD spend and lock audit rows to `~/.ai-os/ai-os.db`.

```bash
ai-os epic history                 # past epics: status, task counts, tokens, USD
ai-os cost                         # spend grouped by provider+model, all epics
ai-os cost --epic <epic-id>        # spend for one epic
```

### Configuration knobs (`.env`)

The most useful ones (see `.env.example` for the full list):

| Variable | What it does |
| --- | --- |
| `ANTHROPIC_MODE` / `ANTHROPIC_API_KEY` | Use the `claude` CLI subscription (`session`, default) or the metered API (`api_key`). |
| `GEMINI_API_KEY` / `OPENROUTER_API_KEY` | Enable the Gemini / OpenRouter adapters. |
| `AI_OS_PROVIDER_ORDER_<RISK>` | Which provider handles each risk level (e.g. cheap models for LOW, Claude for HIGH). |
| `AI_OS_MODEL_<PROVIDER>_<RISK>` | The specific model per provider per risk level. |
| `AI_OS_EPIC_BUDGET_USD` | Optional hard cost cap per epic — remaining tasks are skipped once it's hit. |
| `AI_OS_RATE_LIMIT_RETRIES` | How many times to back off on a 429 before falling back to the next provider. |

---

## 🌱 Database seed templates

When a project's tests need a database, it declares one in a committed
`.ai-os/sandbox.json` (see **[docs/SANDBOX_CONFIG.md](docs/SANDBOX_CONFIG.md)**).
Validation then starts a **throwaway DB on a `--internal` Docker network** (no
internet route), runs the project's migrations + a **reference-data seed**, then
the tests — and destroys the whole DB afterwards. So you never write a cleanup
step: every run (and every retry) starts from a fresh, freshly-seeded database.

The migration + seed are **maintained in your own repo**. When a task changes
the schema, the agent is instructed to update the migration and the seed too, so
DB-backed tests keep passing. Below are two dummy templates to copy and adapt.

### Template A — Prisma (Node / TypeScript)

`.ai-os/sandbox.json`:

```json
{
  "database": {
    "image": "postgres:16",
    "hostname": "db",
    "env": { "POSTGRES_USER": "app", "POSTGRES_PASSWORD": "app", "POSTGRES_DB": "app" }
  },
  "env": { "DATABASE_URL": "postgres://app:app@db:5432/app" },
  "setup_commands": [
    "npx prisma migrate deploy",
    "npx tsx prisma/seed-reference.ts"
  ],
  "test_command": "npm run test:unit"
}
```

`prisma/schema.prisma` (dummy model):

```prisma
model Widget {
  id        Int      @id @default(autoincrement())
  name      String   @unique
  createdAt DateTime @default(now())
}
```

`prisma/seed-reference.ts` (dummy seed — prefer going through your real
service/repository layer over raw SQL, so ids and side effects are realistic):

```typescript
import { PrismaClient } from "@prisma/client";

const prisma = new PrismaClient();

async function main() {
  // Idempotent so a re-run (or a partially-applied prior run) is safe.
  await prisma.widget.upsert({
    where: { name: "reference-widget" },
    update: {},
    create: { name: "reference-widget" },
  });
  // ...add the reference rows your tests assume exist.
}

main()
  .then(() => prisma.$disconnect())
  .catch((e) => {
    console.error(e);
    prisma.$disconnect();
    process.exit(1); // non-zero -> AI-OS reports a setup/seed failure
  });
```

### Template B — Flyway (Java / Spring Boot)

With Spring + Flyway, migrations (including a seed migration) run automatically
when the Spring context loads during `mvn test`, so `setup_commands` can be
empty — you just add the seed as a versioned migration.

`.ai-os/sandbox.json`:

```json
{
  "database": {
    "image": "postgres:16",
    "hostname": "db",
    "env": { "POSTGRES_USER": "app", "POSTGRES_PASSWORD": "app", "POSTGRES_DB": "app" }
  },
  "env": {
    "SPRING_DATASOURCE_URL": "jdbc:postgresql://db:5432/app",
    "SPRING_DATASOURCE_USERNAME": "app",
    "SPRING_DATASOURCE_PASSWORD": "app",
    "SPRING_FLYWAY_ENABLED": "true"
  }
}
```

`src/main/resources/db/migration/V1__widgets.sql` (dummy schema):

```sql
CREATE TABLE widgets (
    id   BIGSERIAL PRIMARY KEY,
    name TEXT NOT NULL UNIQUE
);
```

`src/main/resources/db/migration/V999__seed_reference_data.sql` (dummy seed — a
high version so it applies after every real schema migration; idempotent):

```sql
INSERT INTO widgets (name) VALUES ('reference-widget')
ON CONFLICT (name) DO NOTHING;
-- ...add the reference rows your tests assume exist.
```

> Keep seed migrations in a version range you reserve for seeds (e.g. `V900+`)
> so they never collide with real schema migrations, and always make them
> idempotent (`ON CONFLICT DO NOTHING` / `MERGE`).

---

## 📌 Single Source of Truth

This repository and the specifications in the `docs/` folder are the single source of truth for the **AI-OS** project. When developing any new module, interface, or class, keep a strict separation between deterministic and heuristic (AI) work — deterministic first, AI only where judgement is genuinely required.
