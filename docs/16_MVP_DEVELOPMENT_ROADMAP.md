# 16. AI-OS MVP Development Roadmap & Testing Plan

Ez a dokumentum az **AI-OS 4-Fázisú Fejlesztési Ütemtervének (MVP Roadmap)** és Tesztelési Stratégiájának részletes specifikációja.

---

## 🗺️ Mérföldkő Mátrix (Phase Overview)

```mermaid
graph LR
    P1[1. Fázis: Polyglot Analyzer & Knowledge Graph] --> P2[2. Fázis: Orchestrator Core & Git Engine]
    P2 --> P3[3. Fázis: MCP Router & Ephemeral Sandbox]
    P3 --> P4[4. Fázis: Glass Box Web UI & HITL]
```

---

## 📌 1. Fázis: Determinisztikus Alapok (Polyglot Analyzer & Knowledge Graph)

> **Fókusz**: AI tokenek elégetése nélküli kódbázis-értelmezés, szimbólum kinyerés és kontextus-tömörítés.

### Megvalósítandó Modulok:
- **`ai_os/analyzer/tree_sitter_engine.py`**:
  - Tree-sitter parserek beállítása (JavaScript/TypeScript, Python, Java, HTML, CSS, SQL).
  - Szimbólumok (osztályok, függvények, típusok) kinyerése kezdő/záró sorszámokkal.
- **`ai_os/analyzer/call_graph_builder.py`**:
  - Fájlok közötti statikus `IMPORTS` és függvény-szintű `CALLS` függőségi gráf építés.
- **`ai_os/knowledge/graph_engine.py`**:
  - `NetworkX` irányított multi-gráf inicializálása.
  - $k$-hop szomszédsági bejárási algoritmus ($k=2$).
- **`ai_os/knowledge/skeleton_extractor.py`**:
  - Függvénytörzsek kiejtése, vázlatos interfész stub-ok generálása.

### 🧪 1. Fázis Tesztelési Kritériumok (Acceptance Tests):
- [x] Egy 100 fájlból álló mintaprojekt Tree-sitter beolvasása kevesebb mint 1 másodperc alatt.
- [x] A $k$-hop subgraph kinyerő 80-90%-os token-megtakarítást ér el a nyers fájlokhoz képest.

---

## 📌 2. Fázis: Rendszermag, Zárolás és Git Izoláció (Orchestrator Core & Git Engine)

> **Fókusz**: Párhuzamos feladat-végrehajtás, adatkonfliktus-mentesség és adatbázis perzisztencia.

### Megvalósítandó Modulok:
- **`ai_os/core/lock_manager.py`**:
  - Aszinkron `LockManager` megírása (`read_set` megosztott zárolás, `write_set` kizárólagos zárolás).
- **`ai_os/core/staging.py`**:
  - `GitStagingEngine` megírása (izolált Worktree mappák kreálása `.ai-os/worktrees/TASK-ID`, `git merge-tree`, rebase staging).
- **`ai_os/core/db/`**:
  - SQLite 3 (WAL Mode) adatbázis kapcsolat (`aiosqlite` + SQLAlchemy 2.0 Async).
  - `EpicModel`, `TaskModel`, `LockAuditModel`, `TokenCostModel` migrálása.
- **`ai_os/core/planner.py`**:
  - DAG topológiai sorrendezés (`networkx.topological_sort`) és ciklusdetektálás.

### 🧪 2. Fázis Tesztelési Kritériumok:
- [x] Két diszjunkt feladat (`Task A` és `Task B`) egyidejű lefutása külön Worktree-ben merge konfliktus nélkül.
- [x] Két azonos fájlt módosítani kívánó feladat esetén a Lock Manager sorba rendezi a végrehajtást.

---

## 📌 3. Fázis: MCP Engine & Eldobható Homokozó (MCP Router & Ephemeral Sandbox)

> **Fókusz**: AI modellek biztonságos integrációja és automata konténeres tesztelés.

### Megvalósítandó Modulok:
- **`ai_os/mcp/protocol_router.py`**:
  - Kockázat- és költségalapú modellválasztási mátrix (`LOW` ➔ Gemini Flash, `HIGH` ➔ Claude Sonnet).
- **`ai_os/mcp/adapters/`**:
  - DUAL-AUTH támogatás: API Key VAGY Native Web/OAuth Session Transport (Anthropic, OpenAI ChatGPT Plus, Google Gemini Free Tier, OpenRouter, Ollama).
- **`ai_os/sandbox/container_runner.py`**:
  - `EphemeralSandboxRunner` megírása Docker SDK-val (`--net none`, `-v /worktree:/app:ro`, 2GB RAM limit).
- **`ai_os/sandbox/log_parser.py`**:
  - ANSI terminál kódok letisztítása és JSON feedback formázás az LLM számára.

### 🧪 3. Fázis Tesztelési Kritériumok:
- [x] Az ágens által generált kód nem tud kimenő hálózati kérést indítani a konténerből.
- [x] Hibás kód esetén a konténer hibaüzenete alapján az ágens 2. próbálkozásra kijavítja a hibát.

---

## 📌 4. Fázis: Glass Box Web UI és HITL Integráció (Vezérlő Felület & HITL)

> **Fókusz**: Valós idejű fejlesztői átláthatóság és interaktív jóváhagyási munkafolyamat.

### Megvalósítandó Modulok:
- **`ai_os/main.py` & `ws_manager`**:
  - FastAPI REST API végpontok és WebSocket szerver az élő log- és állapot-közvetítéshez (`/api/v1/ws/events`).
- **`ui/src/components/DagCanvas.tsx`**:
  - React Flow interaktív DAG feladat-gráf vizualizáció.
- **`ui/src/components/HitlDrawer.tsx`**:
  - **Stage 1 Plan Review**: A DAG végrehajtás automatikus megállítása `PLAN_REVIEW` állapotban a fejlesztő jóváhagyásáig.
  - **Stage 2 Runtime Preemption**: "Pause / Közbeszólás" gomb.
  - **Stage 3 Monaco Editor**: Beépített VS Code kód-szerkesztő a felületen a sikertelen tesztek kézi javításához és felülbírálásához.

### 🧪 4. Fázis Tesztelési Kritériumok:
- [x] Új Epic indításakor a felület megállítja a folyamatot, amíg a fejlesztő rákattint az "Approve & Execute DAG" gombra.
- [x] Manuális kódmódosítás után a UI-ról futtatott teszt sikere esetén a DAG automatikusan folytatódik.
