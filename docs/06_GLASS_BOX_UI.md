# 06. Glass Box UI & Observability Spec

A **Glass Box UI** az AI-OS megfigyelhetőségi (observability) és vezérlő felülete. Célja, hogy teljes átláthatóságot ("üvegdoboz") biztosítson a fejlesztő számára a háttérben futó AI ágensek, fájlzárolások és DAG állapotok felett.

---

## 1. UI Koncepció és Felületek

A Glass Box UI két formában áll a fejlesztő rendelkezésére:
1. **CLI Terminal Dashboard**: Term terminál alapú, Rich/Textual alapon működő valós idejű dashboard.
2. **Web Dashboard**: React + WebSockets alapon működő vizuális DAG és állapot-grafikon.

---

## 2. Fő Funkcionális Modulok

### 2.1. DAG Execution Visualizer (DAG Gráf Vizuális Megjelenítő)
- Megjeleníti az aktuális Epic feladat-gráfját (Node-ok és élek).
- **Színkódolt Állapotok**:
  - ⚪ `PENDING`: Várakozik a szülő feladatokra.
  - 🟡 `READY`: Várakozik a zárolásra / modell ütemezésre.
  - 🔵 `RUNNING`: Aktívan kódoló ágens (mutatja a kiválasztott modellt, pl. Claude 3.5 Sonnet).
  - 🟢 `COMPLETED`: Sikeresen lefordult és tesztelt feladat.
  - 🔴 `FAILED / HITL`: Felfüggesztett feladat, amely emberi beavatkozásra vár.

### 2.2. Lock Manager & Resource Status (Zárolás Monitor)
- Valós idejű táblázat a jelenleg aktív fájl-zárolásokról:

| Zárolt Fájl | Zárolás Típusa | Tartó Task ID | Várakozó Task-ok |
| :--- | :--- | :--- | :--- |
| `src/controllers/UserController.ts` | **EXCLUSIVE WRITE** | `TASK-104` | `TASK-108` |
| `src/types/user.ts` | **SHARED READ** | `TASK-104`, `TASK-105` | None |

### 2.3. Agent Terminal & Log Streamer
- Kiválasztható bármely aktív ágens, és élőben nézhető a prompt generálás, a modell válasza, a worktree git diff-je, valamint a Docker konténeres teszt kimenet.

### 2.4. Cost & Token Usage Analytics
- Valós idejű költségkövető dashboard:
  - Teljes felhasznált Input/Output tokenek száma modellcsoportonként.
  - Az adott session/Epic jelenlegi dollárköltsége ($).
  - Determinisztikus spórolás mutató (hány token lett megspórolva a Polyglot Analyzer és Context Cache használatával).

---

## 3. WebSockets & Real-Time Event Architecture

Az Orchestrator Core egy belső **FastAPI + WebSocket** szervert futtat a nézet valós idejű frissítéséhez.

```json
{
  "event_type": "TASK_STATUS_CHANGED",
  "timestamp": "2026-08-02T10:15:30Z",
  "payload": {
    "task_id": "TASK-104",
    "previous_status": "READY",
    "new_status": "RUNNING",
    "assigned_model": "claude-3-5-sonnet",
    "write_set": ["src/controllers/UserController.ts"]
  }
}
```

---

## 4. Emberi Beavatkozás (HITL Control Panel)

Amikor a rendszer Preemption állapotba lép (`HITL` státusz):
- A dashboard kiemeli a blokkolt feladatot.
- A fejlesztőnek 3 opciója van az UI-on keresztül:
  1. **Retry with Feedback**: Egyedi instrukció/útmutatás megadása az ágensnek.
  2. **Manual Edit & Resume**: A fejlesztő maga módosítja a kódot a worktree-ben, majd `PASS` gombbal felülbírálja a tesztet.
  3. **Skip / Abort**: A feladat átugrása vagy a teljes DAG leállítása.
