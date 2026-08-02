# 14. Project Directory Layout & Package Blueprint

Ez a dokumentum az **AI-OS Repository Könyvtárstruktúrájának és Modul-Elrendezésének** teljes specifikációja.

---

## 1. Teljes Könyvtárstruktúra (Repository Layout)

```
ai-os/
├── README.md
├── pyproject.toml
├── docker-compose.yml
├── docs/
│   ├── 01_ARCHITECTURE_OVERVIEW.md
│   ├── 02_ORCHESTRATOR_CORE.md
│   ├── 03_POLYGLOT_ANALYZER.md
│   ├── 04_KNOWLEDGE_CONTEXT_ENGINE.md
│   ├── 05_EXECUTION_VALIDATION_SANDBOX.md
│   ├── 06_GLASS_BOX_UI.md
│   ├── 07_MCP_ADAPTER_ROUTER.md
│   ├── 08_KNOWLEDGE_GRAPH_AND_SUBGRAPH_EXTRACTION.md
│   ├── 09_GIT_WORKTREE_STAGING_ENGINE.md
│   ├── 10_EPHEMERAL_CONTAINER_SANDBOX_SPEC.md
│   ├── 11_ORCHESTRATOR_TECH_STACK_AND_DEPL.md
│   ├── 12_GLASS_BOX_UI_AND_HITL_SPEC.md
│   ├── 13_DB_SCHEMA_AND_MODELS.md
│   └── 14_PROJECT_DIRECTORY_STRUCTURE.md
├── docker/
│   ├── Dockerfile.core
│   └── Dockerfile.ui
├── ai_os/
│   ├── __init__.py
│   ├── main.py                          # FastAPI & MCP Server Entry Point
│   ├── core/                            # Orchestrator Kernel Engine
│   │   ├── __init__.py
│   │   ├── planner.py                   # DAG Planner & LLM Task Decomposition
│   │   ├── scheduler.py                 # Dynamic Cost & Risk Scheduler
│   │   ├── lock_manager.py              # Async Read-Set / Write-Set Lock Manager
│   │   ├── staging.py                   # Git Worktree & Merge/Rebase Staging Engine
│   │   └── db/
│   │       ├── __init__.py
│   │       ├── database.py              # Async Engine & Sessionmaker
│   │       └── models.py                # SQLAlchemy 2.0 Async Models
│   ├── analyzer/                        # Polyglot Deterministic Parser
│   │   ├── __init__.py
│   │   ├── tree_sitter_engine.py        # Py-tree-sitter AST & Symbol Extractor
│   │   └── call_graph_builder.py        # Static Call Graph & Import Resolver
│   ├── knowledge/                       # Knowledge Graph & Context Engine
│   │   ├── __init__.py
│   │   ├── graph_engine.py              # NetworkX Graph Manager & Subgraph Traversal
│   │   ├── skeleton_extractor.py        # AST Code Stub Compression
│   │   └── event_bus.py                 # AsyncIO Invalidation Event Bus
│   ├── mcp/                             # MCP Server & Adapter Engine
│   │   ├── __init__.py
│   │   ├── mcp_server.py                # MCP JSON-RPC Server & SSE Transport
│   │   ├── protocol_router.py           # Unified LLM Protocol Router & Cost Tracker
│   │   └── adapters/
│   │       ├── base_adapter.py
│   │       ├── anthropic_adapter.py
│   │       ├── openai_adapter.py
│   │       ├── gemini_adapter.py
│   │       └── local_adapter.py
│   └── sandbox/                         # Ephemeral Container Execution Engine
│       ├── __init__.py
│       ├── container_runner.py          # Docker SDK Ephemeral Runner
│       └── log_parser.py                # ANSI Log Stripper & JSON Formatter
└── ui/                                  # Glass Box Web Dashboard (React)
    ├── package.json
    ├── vite.config.ts
    ├── src/
    │   ├── App.tsx
    │   ├── components/
    │   │   ├── DagCanvas.tsx            # React Flow Interactive Graph
    │   │   ├── LiveLogsConsole.tsx      # WebSocket Streaming Console
    │   │   ├── LockMonitorTable.tsx     # Active Read/Write Lock Monitor
    │   │   └── HitlDrawer.tsx           # Plan Review & Monaco Code Editor Modal
    │   └── services/
    │       └── websocket.ts             # WS Client Connection Engine
```

---

## 2. Python Függőségek Specifikációja (`pyproject.toml`)

```toml
[build-system]
requires = ["setuptools>=68.0", "wheel"]
build-backend = "setuptools.build_meta"

[project]
name = "ai-os"
version = "0.1.0"
description = "AI Software Engineering Orchestrator & Build System"
authors = [{ name = "AI-OS Team" }]
readme = "README.md"
requires-python = ">=3.12"
dependencies = [
    # Fast Web & API Framework
    "fastapi>=0.110.0",
    "uvicorn[standard]>=0.28.0",
    "websockets>=12.0",
    "pydantic>=2.6.0",

    # Async Database & ORM
    "sqlalchemy>=2.0.28",
    "aiosqlite>=0.20.0",
    "alembic>=1.13.0",

    # Deterministic Code Parsing & Graph Engine
    "tree-sitter>=0.21.0",
    "tree-sitter-typescript>=0.20.0",
    "tree-sitter-python>=0.20.0",
    "tree-sitter-java>=0.20.0",
    "tree-sitter-css>=0.20.0",
    "tree-sitter-html>=0.20.0",
    "networkx>=3.2.1",

    # Docker Engine & Git Controls
    "docker>=7.0.0",

    # MCP Protocol & AI Provider SDKs
    "mcp>=0.1.0",
    "anthropic>=0.18.0",
    "openai>=1.12.0",
    "google-generativeai>=0.4.0",
]

[project.scripts]
ai-os = "ai_os.main:cli_entrypoint"
```
