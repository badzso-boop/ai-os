# 01. AI-OS Architecture Overview

## 1. Rendszerarchitektúra Részletezése

Az **AI-OS** egy moduláris, aszinkron eseményvezérelt (event-driven) architektúrára épülő orkesztrációs platform. A rendszer célja a szoftverfejlesztési feladatok automatizált lebonyolítása determinisztikus elemzőeszközök és mesterséges intelligencia modellek szimbiózisában.

```mermaid
graph TD
    User([Felhasználó / Admin]) -->|Epic / Feature Request| DAGPlanner[DAG Planner]
    
    subgraph OrchestratorCore [Orchestrator Core (Python AsyncIO)]
        DAGPlanner -->|DAG Spec / Task Nodes| Scheduler[Dynamic Scheduler]
        Scheduler <-->|Lock Request / Release| LockManager[Lock Manager]
        Scheduler -->|Dispatch Task| TaskRunner[Agent Task Runner]
    end

    subgraph DeterministicLayer [Determinisztikus Elemző Réteg]
        Repo[Git Repository] -->|Source Code| PolyglotAnalyzer[Polyglot Analyzer Engine]
        PolyglotAnalyzer -->|AST / Call Graph / Symbols| KnowledgeEngine[Knowledge Engine]
    end

    subgraph KnowledgeLayer [Tudás és Kontextus Réteg]
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
        Container -->|Run Compiler/Linter/Tests| ValidationResult{Validáció Sikeres?}
        ValidationResult -->|Igen| Merge[Commit & Merge Branch]
        ValidationResult -->|Nem: Retry < N| PromptFeedback[Prompt Feedback Loop]
        PromptFeedback --> TaskRunner
        ValidationResult -->|Nem: Retry >= N| HITL[Human-In-The-Loop Preemption]
    end

    subgraph Observability [Observability & Control]
        OrchestratorCore -.->|State & Logs| GlassBoxUI[Glass Box UI Dashboard]
    end
```

---

## 2. Adatáramlás és Életciklus (End-to-End Flow)

1. **Beérkező Kérés**: A felhasználó megad egy magas szintű feladatot (Epic / Feature).
2. **DAG Generálás**: A **DAG Planner** lebontja a feladatot mentális lépésekre, kiszámítja a függőségeket, és deklarálja a feladatok várható **Read Set** és **Write Set** fájllistáját.
3. **Kódbázis Elemzés**: A **Polyglot Analyzer** determinisztikusan feltérképezi az AST-t, függőségeket és hívási gráfokat, frissítve a **Knowledge Graph**-ot.
4. **Kontextus Generálás**: A **Context Cache** kizárólag a releváns szimbólumokat és fájlfejléceket csomagolja be az ágens számára.
5. **Erőforrás Zárolás & Ütemezés**: A **Lock Manager** ellenőrzi, hogy a Write Set elemei nem zároltak-e. Ha szabadok, a **Dynamic Scheduler** kiválasztja a feladathoz illő legolcsóbb/legjobb modellt az MCP adaptereken keresztül.
6. **Szigetelt Végrehajtás**: Az ágens egy elkülönített Git Worktree-ben hajtja végre a módosításokat.
7. **Konténeres Validáció**: A módosított kód egy eldobható Docker konténerben fut le (fordítás, linting, unit tesztek).
8. **Eseményvezérelt Invalidáció**: Sikeres teszt esetén a rendszer módosítja a fő ágat, és az **Event Bus** üzenetet küld a többi ágensnek a megváltozott interfészekről.

---

## 3. Determinisztikus vs. Heurisztikus Felelősségi Mátrix

| Feladat | Felelős Komponens | Típus | Indoklás |
| :--- | :--- | :--- | :--- |
| AST és szimbólum kinyerés | Polyglot Analyzer (Tree-sitter) | **Determinisztikus** | 100%-os pontosság, 0 token költség, azonnali lefutás. |
| Függőségi / Call Graph építés | Polyglot Analyzer | **Determinisztikus** | Statikus kódanalízissel szabványosan számítható. |
| Feladat-bontás (DAG) | DAG Planner (LLM) | **Heurisztikus / AI** | Komplex feladatok értelmezése és absztrakciója. |
| Kódírás & Refaktorálás | MCP Agent Runner (LLM) | **Heurisztikus / AI** | Kreatív és generatív kódgenerálási képesség. |
| Fájl zárolás (Read/Write Locks) | Lock Manager | **Determinisztikus** | Párhuzamossági és adatkonfliktus-kezelési algoritmusok. |
| Kód fordítás és tesztelés | Ephemeral Docker Sandbox | **Determinisztikus** | A fordítók és tesztfuttatók (pytest, npm test, javac) szigorú kimenete. |
| Költség & Modell kiválasztás | Dynamic Scheduler | **Determinisztikus / Szabályalapú** | Kockázati mátrix és árképzési szabályok alapján. |
