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
- **Execution sandbox**: Git worktrees + ephemeral, hardened Docker containers

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

The system works **end-to-end from the command line today**: give it a high-level request, it decomposes the work into a task DAG, routes each task to a model by risk, lets the model autonomously call tools to edit code, validates every change in a sandbox, and merges to `main` — recording token/USD spend along the way. See `CLAUDE.md` for the per-phase "how it actually works" detail and the documented trade-offs/limitations.

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
.venv/bin/pytest -q          # 285 tests (284 passed + 1 documented xfail), ~33s

# 3. One-time: build the sandbox image used to validate Python tasks
#    (bakes pytest in, since the sandbox runs with --network none)
docker build -t ai-os-sandbox-python:3.12 -f docker/python-sandbox.Dockerfile .

# 4. Configure provider credentials (only what you actually have)
cp .env.example .env
# then edit .env — see that file for every variable and what it does
.venv/bin/ai-os llm list     # shows which providers are actually configured
```

> `.env` is gitignored — never commit real credentials. Every value is optional; only providers with real credentials present get configured.

For convenience, activate the venv so you can drop the `.venv/bin/` prefix:

```bash
source .venv/bin/activate
ai-os --help
```

---

## 🚀 Usage

AI-OS has two kinds of commands: **deterministic, zero-LLM analysis** (free, offline) and **agentic execution** (makes real LLM calls, consumes real usage/quota). They are clearly separated below.

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

Decomposes a high-level request into a task DAG, shows the proposed plan (HITL plan-review gate), then executes it — routing each task to a model by risk, letting tool-capable providers autonomously call the MCP tools, validating every change in the sandbox, and recording spend.

```bash
ai-os epic run my-project --prompt "add JWT authentication" --language python
# review the printed DAG, then approve at the prompt — or skip the gate:
ai-os epic run my-project --prompt "add JWT authentication" --language python --yes
```

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

## 📌 Single Source of Truth

This repository and the specifications in the `docs/` folder are the single source of truth for the **AI-OS** project. When developing any new module, interface, or class, keep a strict separation between deterministic and heuristic (AI) work — deterministic first, AI only where judgement is genuinely required.
