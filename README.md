# AI-OS: AI Software Engineering Orchestrator

> **AI Build System & Software Engineering Orchestrator** – Egy operációs rendszer a modern szoftverfejlesztési folyamatok determinisztikus és mesterséges intelligencia által vezérelt orkesztrációjára.

---

## 💡 Projekt Vízió

Az **AI-OS** nem egy újabb kódolási asszisztens vagy IDE bővítmény (mint a GitHub Copilot vagy a Cursor). Az AI-OS egy **AI Szoftvermérnöki Orkesztrátor**, amely operációs rendszerként irányítja és felügyeli a szoftverfejlesztés teljes életciklusát.

Ebben az architektúrában a különböző LLM-ek (Claude, OpenAI, Gemini, DeepSeek, lokális Ollama/vLLM modellek) csupán **felcserélhető végrehajtó magokként (CPU Cores)** funkcionálnak. Az **Orchestrator Core** feladata ezen magok ütemezése, a biztonsági keretek betartatása, a fájl-zárolások (locking) kezelése és a determinisztikus validáció.

---

## 🏛️ Alapelvek (Core Philosophy)

### 1. ⚙️ Compiler First (A fordító az első)
A rendszer **soha nem pazarol AI tokeneket** olyan feladatokra, amelyeket algoritmusok vagy fordítók 100%-os pontossággal és determinisztikusan el tudnak végezni:
- AST (Absztrakt Szintaxisfa) generálás
- Import/Export függőségi gráfok és hívási gráfok (Call Graphs)
- Típusellenőrzés, szintaxis ellenőrzés és linting

### 2. 🧠 Token Hatékonyság és Tudás (Knowledge Before Generation)
Az ágensek nem kapják meg a teljes kódbázist, elkerülve a kontextusablak elárasztását és a hallucinációkat. A rendszer egy tömörített **Context Cache**-t generál, amely kizárólag a feladathoz szükséges szimbólumokat, interfészeket, típusdefiníciókat és architekturális szabályokat tartalmazza.

### 3. 🔌 Modell-Függetlenség és Költségtudatosság (Model Agnostic & Cost Aware)
A modellekkel való kommunikáció szabványos **MCP (Model Context Protocol)** adaptereken keresztül történik. A **Dynamic Scheduler** a feladat kockázati besorolása és komplexitása alapján választja ki a legoptimálisabb (legolcsóbb / legmegbízhatóbb) modellt.

---

## 📦 Rendszerarchitektúra és Modulok

Az AI-OS négy fő rétegre és egy megfigyelhetőségi (observability) felületre tagozódik:

```
+-----------------------------------------------------------------------+
|                         Glass Box UI / Dashboard                      |
|              (DAG Vizualizáció, Agent Status, Lock Monitor)           |
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
|  (Tree-sitter Parsers)|          |  (NetworkX Graph & Event Bus)      |
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

### Részletes Dokumentációk:

1. 🏛️ [System Architecture Overview](docs/01_ARCHITECTURE_OVERVIEW.md) – A teljes rendszerarchitektúra, adatáramlások és eseményvezérelt kommunikáció.
2. 🧠 [Orchestrator Core Specs](docs/02_ORCHESTRATOR_CORE.md) – DAG Planner, Dynamic Scheduler és Lock Manager specifikációja.
3. 🔬 [Polyglot Analyzer Engine](docs/03_POLYGLOT_ANALYZER.md) – Determinisztikus parser réteg (Tree-sitter, AST, Call Graph).
4. 📚 [Knowledge Graph & Context Cache](docs/04_KNOWLEDGE_CONTEXT_ENGINE.md) – Tudásgráf, szoftver entitások és eseményvezérelt cache érvénytelenítés.
5. 🛡️ [Execution & Validation Sandbox](docs/05_EXECUTION_VALIDATION_SANDBOX.md) – Git Worktrees, Ephemeral Docker/Podman konténerek és HITL Preemption Engine.
6. 📊 [Glass Box UI & Observability](docs/06_GLASS_BOX_UI.md) – Valós idejű CLI/Web dashboard a rendszer működésének felügyeletére.

---

## 🛠️ Technológiai Stakk

- **Orchestrator Core**: Python 3.12+ (`asyncio`, `pydantic`, `fastapi` / `click`)
- **Determinisztikus Parser**: `py-tree-sitter`, tree-sitter grammars (JS/TS, Python, Java, HTML, CSS, SQL)
- **Tudásgráf & Cache**: `networkx` / `Neo4j`, In-Memory Event Bus (`asyncio.Queue` / `redis`)
- **Modell Protokoll**: MCP (Model Context Protocol) client SDK
- **Végrehajtó Homokozó**: Git Worktree, Docker / Podman Python SDK (`docker-py`)
- **Glass Box UI**: React + Tailwind / Rich (Python CLI Terminal Dashboard) & WebSockets

---

## 📌 Igazságforrás (Single Source of Truth)

Ez a repository és a benne található `docs/` mappában lévő specifikációk képezik az **AI-OS** projekt egyetlen igazságforrását. Bármely új modul, interfész vagy osztály fejlesztése során a determinisztikus és heurisztikus feladatok szigorú szétválasztását kell alkalmazni.
