# 02. Orchestrator Core Specification

The **Orchestrator Core** is the central control unit of AI-OS. Written in Python 3.12+, it utilizes an asynchronous event loop (`asyncio`) for high concurrency and low latency.

---

## 1. DAG Planner (Planner Module)

The **DAG Planner** is responsible for decomposing high-level user requests (Epic / User Story) into atomic tasks (Tasks) and building a **Directed Acyclic Graph (DAG)** from them.

### 1.1. Task Node Structure
Every task node contains the following attributes:

```python
from pydantic import BaseModel, Field
from typing import List, Set, Optional, Literal

class TaskNode(BaseModel):
    id: str = Field(..., description="Unique task identifier (e.g. TASK-001)")
    title: str
    description: str
    risk_level: Literal["LOW", "MEDIUM", "HIGH", "CRITICAL"]
    target_files: List[str] = Field(..., description="List of affected files")
    read_set: Set[str] = Field(default_factory=set, description="Files subject to read locks")
    write_set: Set[str] = Field(default_factory=set, description="Files subject to write locks")
    dependencies: List[str] = Field(default_factory=list, description="Parent task identifiers")
    status: Literal["PENDING", "READY", "RUNNING", "COMPLETED", "FAILED", "BLOCKED"] = "PENDING"
    max_retries: int = 3
    retry_count: int = 0
```

### 1.2. Topological Sorting and Cycle Detection
The DAG Planner uses the `networkx` library to manage the task dependency graph:
- Cyclic dependencies are detected before code generation and returned to the Planner LLM for correction (`networkx.is_directed_acyclic_graph`).
- Execution proceeds in topological order (`topological_sort`) or via parallelization across dependency levels.

---

## 2. Dynamic Scheduler (Scheduler Module)

The **Dynamic Scheduler** is responsible for dispatching `READY` tasks to the optimal AI model (MCP Adapter).

### 2.1. Model Selection Matrix (Cost & Risk Awareness)

The system selects a model based on the task risk level and the target component layer:

| Risk Level | Task Type | Recommended AI Model | Cost / Token Profile |
| :--- | :--- | :--- | :--- |
| **LOW** | CSS/Style adjustments, documentation updates, trivial unit tests | Gemini 1.5 Flash / DeepSeek V3 | Extremely Low Cost |
| **MEDIUM** | New UI component, existing function refactoring, bugfix | GPT-4o-mini / Claude 3.5 Haiku | Low/Medium Cost |
| **HIGH** | New API endpoint, database schema modification, business logic | Claude 3.5 Sonnet / GPT-4o | Premium Model |
| **CRITICAL** | Architectural shift, security module, complex algorithmic DAG | Claude 3.5 Sonnet (High Temp/Reasoning) | Maximum Reasoning |

### 2.2. Rate Limiting and Quota Management
- The Scheduler tracks **TPM (Token Per Minute)** and **RPM (Request Per Minute)** limits of configured API keys.
- If a premium model reaches its quota limit, the Scheduler enforces task backoff or redirects the task to an equivalent fallback model.

---

## 3. Lock Manager (Concurrency Lock Manager)

To prevent simultaneous code modifications by multiple agents and avoid merge conflicts, AI-OS employs a **Granular File Locking System**.

### 3.1. Read Set / Write Set Rules
Every task declares its access requirements:
- **Shared Read Lock (`read_set`)**: Multiple tasks can read the same file simultaneously.
- **Exclusive Write Lock (`write_set`)**: A file can only be modified by a single active task at any given moment (`write_set`).

```python
import asyncio
from typing import Dict, Set

class LockManager:
    def __init__(self):
        self._read_locks: Dict[str, int] = {}  # filepath -> active readers count
        self._write_locks: Set[str] = set()    # filepath in active write lock
        self._lock_condition = asyncio.Condition()

    async def acquire_locks(self, task_id: str, read_set: Set[str], write_set: Set[str]) -> bool:
        async with self._lock_condition:
            while True:
                # Verify if any write_set file is locked (for reading or writing)
                write_conflict = any(f in self._write_locks or self._read_locks.get(f, 0) > 0 for f in write_set)
                read_conflict = any(f in self._write_locks for f in read_set)
                
                if not write_conflict and not read_conflict:
                    # Acquire locks
                    for f in read_set:
                        self._read_locks[f] = self._read_locks.get(f, 0) + 1
                    for f in write_set:
                        self._write_locks.add(f)
                    return True
                
                await self._lock_condition.wait()

    async def release_locks(self, read_set: Set[str], write_set: Set[str]):
        async with self._lock_condition:
            for f in read_set:
                if f in self._read_locks:
                    self._read_locks[f] -= 1
                    if self._read_locks[f] == 0:
                        del self._read_locks[f]
            for f in write_set:
                self._write_locks.discard(f)
            self._lock_condition.notify_all()
```

### 3.2. Parallel Execution via Git Worktrees
If the Write Sets of two independent tasks are disjoint (`TaskA.write_set ∩ TaskB.write_set = ∅`), the Lock Manager permits **parallel execution** of both tasks in isolated Git Worktree environments.

