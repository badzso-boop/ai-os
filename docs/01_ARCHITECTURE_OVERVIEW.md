# 01. AI-OS Architecture Overview

## 1. System Architecture Details

**AI-OS** is an orchestration platform built on a modular, asynchronous event-driven architecture. The goal of the system is to automate software engineering tasks through the symbiosis of deterministic analysis tools and artificial intelligence models.

```mermaid
graph TD
    User([User / Admin]) -->|Epic / Feature Request| DAGPlanner[DAG Planner]
    
    subgraph OrchestratorCore [Orchestrator Core (Python AsyncIO)]
        DAGPlanner -->|DAG Spec / Task Nodes| Scheduler[Dynamic Scheduler]
        Scheduler <-->|Lock Request / Release| LockManager[Lock Manager]
        Scheduler -->|Dispatch Task| TaskRunner[Agent Task Runner]
    end

    subgraph DeterministicLayer [Deterministic Analysis Layer]
        Repo[Git Repository] -->|Source Code| PolyglotAnalyzer[Polyglot Analyzer Engine]
        PolyglotAnalyzer -->|AST / Call Graph / Symbols| KnowledgeEngine[Knowledge Engine]
    end

    subgraph KnowledgeLayer [Knowledge and Context Layer]
        KnowledgeEngine -->|Graph Database| KG[(Knowledge Graph)]
        KnowledgeEngine -->|Context Invalidation| EventBus((Event Bus))
        EventBus -->|Updated Context| ContextCache[Context Cache]
        ContextCache -->|Filtered Prompt Context| TaskRunner
    end

    subgraph ModelLayer [AI Executing Cores (MCP Adapters)]
        TaskRunner <-->|MCP Protocol| FastModel[Gemini Flash / DeepSeek - Low Cost]
        TaskRunner <-->|MCP Protocol| AdvancedModel[Claude 3.5 Sonnet / GPT-4o - High Risk]
    end

    subgraph ExecutionSandbox [Execution & Validation Sandbox]
        TaskRunner -->|Write Code| Worktree[Git Worktree]
        Worktree -->|Mount Directory| Container[Ephemeral Docker/Podman Container]
        Container -->|Run Compiler/Linter/Tests| ValidationResult{Validation Successful?}
        ValidationResult -->|Yes| Merge[Commit & Merge Branch]
        ValidationResult -->|No: Retry < N| PromptFeedback[Prompt Feedback Loop]
        PromptFeedback --> TaskRunner
        ValidationResult -->|No: Retry >= N| HITL[Human-In-The-Loop Preemption]
    end

    subgraph Observability [Observability & Control]
        OrchestratorCore -.->|State & Logs| GlassBoxUI[Glass Box UI Dashboard]
    end
```

---

## 2. End-to-End Data Flow and Lifecycle

1. **Incoming Request**: The user specifies a high-level task (Epic / Feature).
2. **DAG Generation**: The **DAG Planner** decomposes the task into mental steps, calculates dependencies, and declares the expected **Read Set** and **Write Set** file lists for tasks.
3. **Codebase Analysis**: The **Polyglot Analyzer** deterministically maps the AST, dependencies, and call graphs, updating the **Knowledge Graph**.
4. **Context Generation**: The **Context Cache** packages exclusively relevant symbols and file headers for the agent.
5. **Resource Locking & Scheduling**: The **Lock Manager** verifies that elements of the Write Set are not locked. If available, the **Dynamic Scheduler** selects the cheapest/best model suitable for the task via MCP adapters.
6. **Isolated Execution**: The agent executes modifications inside an isolated Git Worktree.
7. **Containerized Validation**: Modified code runs inside an ephemeral Docker container (compilation, linting, unit tests).
8. **Event-Driven Invalidation**: Upon test success, the system merges changes to the main branch, and the **Event Bus** notifies other agents of modified interfaces.

---

## 3. Deterministic vs. Heuristic Responsibility Matrix

| Task | Responsible Component | Type | Rationale |
| :--- | :--- | :--- | :--- |
| AST and symbol extraction | PolyglotAnalyzer (Tree-sitter) | **Deterministic** | 100% accuracy, 0 token cost, instant execution. |
| Dependency / Call Graph construction | PolyglotAnalyzer | **Deterministic** | Standard calculation via static code analysis. |
| Task decomposition (DAG) | DAGPlanner (LLM) | **Heuristic / AI** | Interpretation and abstraction of complex requirements. |
| Code writing & Refactoring | MCP Agent Runner (LLM) | **Heuristic / AI** | Creative and generative code generation capabilities. |
| File locking (Read/Write Locks) | LockManager | **Deterministic** | Concurrency and data conflict management algorithms. |
| Code compilation and testing | Ephemeral Docker Sandbox | **Deterministic** | Strict verification output from compilers and runners (pytest, npm test, javac). |
| Cost & Model selection | DynamicScheduler | **Deterministic / Rule-based** | Evaluated via risk matrix and pricing rules. |

