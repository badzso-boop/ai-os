# 11. Orchestrator Tech Stack, Database & Deployment Spec

This document is the **AI-OS Orchestrator Engine es MCP Szerver** technologiai megvalositasanak, adatbazis-architekturajanak, Docker kontenerizaciojanak es HTTP/WebSocket/MCP API vegpontjainak teljesseggel reszletezett specifikacioja.

---

## 1. Programozasi Nyelv es Konyvtar-Stakk

Az Orchestrator Core and the MCP Szerver nyelve: **Python 3.12+**.

### Miert Python?
1. **Aszinkron Concurrency**: Az `asyncio` lehetove teszi tobb szaz parhuzamos LLM hivas, hatterfolyamat (Git, Docker) es WebSocket kapcsolat nem-blokkolo kezeleset alacsony memoria-labnyom along with.
2. **Determinisztikus Elemzo es Graf Okoszisztema**: Nativ integracio a `py-tree-sitter` (AST parser), `networkx` (Tudasgraf) es `docker` (Python SDK) konyvtarakkal.
3. **Adatstruktura Validacio**: `pydantic` v2 a szigoru tipusellenorzeshez es JSON semak automatikus eloallitasahoz az LLM-ek szamara.

---

## 2. Adatbazis es Tarolasi Architektura

Az AI-OS hibrid tarolasi modellt alkalmaz a sebesseg and the alacsony konfiguracios igeny erdekeben:

```mermaid
graph TD
    subgraph Storage & Database Layer
        SQLite[(SQLite 3 WAL Mode)] -->|Perzisztens Allapot| CoreDB[Tasks, DAGs, Cost Metrics, Audit Logs]
        NetworkX[In-Memory NetworkX Graph] -->|Graph Persistence| JSONGraph[JSON / SQLite Graph Table]
        AsyncQueue[AsyncIO Queue / Redis] -->|In-Memory Event Bus| LiveEvents[Real-time Events & Locks]
    end
```

### 2.1. Elsodleges Relacios Adatbazis: **SQLite 3 (WAL Mode)**
- **Koltseghatekony es Zero-Config**: Nincs szukseg kulso adatbazis-szerver telepitesere lokalis hasznalat in case of.
- **WAL (Write-Ahead Logging) Mod**: Magas parhuzamos olvasasi es irasi teljesitmenyt biztosit az aszinkron Python ORM-mel (`SQLAlchemy 2.0 async` / `aiosqlite`).
- **Tarolt Adatok**:
  - `epics` & `tasks`: DAG feladatok allapota (`PENDING`, `RUNNING`, `COMPLETED`, `FAILED`, `HITL`).
  - `lock_audits`: Aktiv es elozo fajl zarolasok tortenete.
  - `cost_metrics`: Hasznalt tokenek szama modellcsoportonkent es dollarkoltseg.

### 2.2. Tudasgraf Tarolo: **NetworkX + SQLite Csomopont Tar**
- A Tudasgraf elsodlegesen a RAM-ban el (`NetworkX` iranyitott graf) az ezredmasodperces $k$-hop lekerdezesekert.
- Rendszerinditaskor es fajlmodositaskor a graf szerkezete elmentodik az SQLite `graph_nodes` es `graph_edges` tablaiba.

---

## 3. Docker Kontenerizacio es Telepitesi Architektura

Az Orchestrator rendszermag sajat magat is futtathatja egy elszeparalt Docker Compose kornyezetben:

```mermaid
graph TD
    subgraph Host OS / Developer Machine
        DockerSocket[/var/run/docker.sock/]
        WorkspaceDir[/mnt/g/Projects/ai-os]
    end

    subgraph Docker Compose Stack
        CoreContainer[Container 1: ai-os-core FastAPI + MCP Server]
        UIContainer[Container 2: ai-os-ui Glass Box React Frontend]
    end

    CoreContainer <-->|Volume Mount| DockerSocket
    CoreContainer <-->|Volume Mount| WorkspaceDir
    UIContainer <-->|HTTP / WS| CoreContainer
```

### `docker-compose.yml` Specifikacio:
```yaml
version: '3.8'

services:
  ai-os-core:
    build:
      context: .
      dockerfile: docker/Dockerfile.core
    container_name: ai-os-core
    ports:
      - "8000:8000"   # REST & WebSocket API
      - "8001:8001"   # MCP Server SSE Port
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock  # Dinamikus kontener inditasi jog
      - .:/workspace                                # Gazdagep kodbazis
    environment:
      - PYTHONUNBUFFERED=1
      - DATABASE_URL=sqlite+aiosqlite:////workspace/.ai-os/ai_os.db
      - ANTHROPIC_API_KEY=${ANTHROPIC_API_KEY}
      - OPENAI_API_KEY=${OPENAI_API_KEY}

  ai-os-ui:
    build:
      context: ./ui
      dockerfile: Dockerfile.ui
    container_name: ai-os-ui
    ports:
      - "3000:80"     # Glass Box Web Dashboard
    depends_on:
      - ai-os-core
```

---

## 4. Kommunikacios Protokollok es API Vegpontok

Az Orchestrator haromfele interfeszt biztosit: **MCP JSON-RPC**, **FastAPI REST API**, es **WebSockets**.

```mermaid
graph LR
    Agents[AI Agents / MCP Clients] <-->|MCP SSE / Stdio| MCPServer[MCP Server Engine :8001]
    UI[Glass Box UI / Web Dashboard] <-->|REST API| REST[FastAPI REST API :8000]
    UI <-->|WebSockets| WS[FastAPI WS Server :8000]
```

### 4.1. MCP Szerver Interfesz (JSON-RPC 2.0 over SSE / Stdio)
- **SSE Vegpont**: `GET /mcp/sse` (Server-Sent Events kapcsolat letesitese az LLM agensekkel)
- **Message Vegpont**: `POST /mcp/messages` (JSON-RPC tool hivasok fogadasa)

---

### 4.2. REST API Vegpontok (`FastAPI`)

| Metodus | Vegpont | Leiras / Funkcio |
| :--- | :--- | :--- |
| `POST` | `/api/v1/epics` | Uj felhasznaloi keres felvetele es DAG tervezes elinditasa. |
| `GET` | `/api/v1/dags/{dag_id}` | Az aktualis DAG graf allapotanak es feladat-nodejainak lekerese. |
| `GET` | `/api/v1/locks` | Aktiv fajl zarolasok (`Read-Set` / `Write-Set`) lekerese. |
| `GET` | `/api/v1/metrics/cost` | Osszesitett token-fogyasztas es dollarkoltseg lekerdezese. |
| `POST` | `/api/v1/tasks/{task_id}/retry` | **HITL**: Sikertelen feladat ujraiditasa egyedi fejlesztoi instrukcioval. |
| `POST` | `/api/v1/tasks/{task_id}/resume` | **HITL**: Kezi kodmodositas jovahagyasa and the feladat tovabbengedese. |

---

### 4.3. WebSocket Vegpont (Valos Ideju Esemenyek)
- **Vegpont**: `WS /api/v1/ws/events`
- **Payload Pelda (Task Allapotvaltozas & Log Streaming)**:
```json
{
  "event_type": "TASK_STATE_CHANGED",
  "timestamp": "2026-08-02T10:35:00Z",
  "data": {
    "task_id": "TASK-102",
    "status": "RUNNING",
    "model_assigned": "claude-3-5-sonnet",
    "worktree": ".ai-os/worktrees/TASK-102",
    "active_locks": {
      "write": ["src/controllers/UserController.ts"]
    }
  }
}
```

---

## 5. Python Rendszermag Belepesi Pont Blueprint (`main.py`)

```python
import asyncio
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager

# In-memory WebSocket manager a Glass Box UI-hoz
class ConnectionManager:
    def __init__(self):
        self.active_connections: list[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)

    def disconnect(self, websocket: WebSocket):
        self.active_connections.remove(websocket)

    async def broadcast(self, message: dict):
        for connection in self.active_connections:
            await connection.send_json(message)

ws_manager = ConnectionManager()

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Inditasi architektura: Adatbazis migralas, Knowledge Engine betoltese
    print("[AI-OS Core] Starting Orchestrator Engine & SQLite Storage...")
    yield
    print("[AI-OS Core] Shutting down Orchestrator Engine...")

app = FastAPI(title="AI-OS Orchestrator Core API", version="1.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/api/v1/health")
async def health_check():
    return {"status": "HEALTHY", "engine": "AI-OS Python Core 3.12"}

@app.websocket("/api/v1/ws/events")
async def websocket_endpoint(websocket: WebSocket):
    await ws_manager.connect(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        ws_manager.disconnect(websocket)
```
