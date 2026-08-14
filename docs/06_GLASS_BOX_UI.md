# 06. Glass Box UI & Observability Spec

A **Glass Box UI** az AI-OS megfigyelhetosegi (observability) es vezerlo interfacee. Celja, hogy teljes atlathatosagot ("uvegdoboz") biztositson a developer szamara a hatterben futo AI agensek, fajlzarolasok es DAG allapotok felett.

---

## 1. UI Koncepcio es Interfaceek

A Glass Box UI ket formaban all a developer rendelkezesere:
1. **CLI Terminal Dashboard**: Term terminal alapu, Rich/Textual alapon mukodo valos ideju dashboard.
2. **Web Dashboard**: React + WebSockets alapon mukodo vizualis DAG es allapot-grafikon.

---

## 2. Fo Funkcionalis Modulok

### 2.1. DAG Execution Visualizer (DAG Graf Vizualis Megjelenito)
- Megjeleniti az aktualis Epic feladat-grafjat (Node-ok es elek).
- **Szinkodolt Allapotok**:
  - ⚪ `PENDING`: Varakozik a szulo feladatokra.
  - 🟡 `READY`: Varakozik a zarolasra / modell utemezesre.
  - 🔵 `RUNNING`: Aktivan kodolo agens (mutatja a kivalasztott modellt, pl. Claude 3.5 Sonnet).
  - 🟢 `COMPLETED`: Sikeresen lefordult es tesztelt feladat.
  - 🔴 `FAILED / HITL`: Felfuggesztett feladat, amely emberi beavatkozasra var.

### 2.2. Lock Manager & Resource Status (Zarolas Monitor)
- Valos ideju tablazat a jelenleg aktiv fajl-zarolasokrol:

| Zarolt Fajl | Zarolas Tipusa | Tarto Task ID | Varakozo Task-ok |
| :--- | :--- | :--- | :--- |
| `src/controllers/UserController.ts` | **EXCLUSIVE WRITE** | `TASK-104` | `TASK-108` |
| `src/types/user.ts` | **SHARED READ** | `TASK-104`, `TASK-105` | None |

### 2.3. Agent Terminal & Log Streamer
- Kivalaszthato barmely aktiv agens, es eloben nezheto a prompt generalas, a modell valasza, a worktree git diff-je, valamint a Docker konteneres teszt kimenet.

### 2.4. Cost & Token Usage Analytics
- Valos ideju koltsegkoveto dashboard:
  - Teljes felhasznalt Input/Output tokenek szama modellcsoportonkent.
  - Az adott session/Epic jelenlegi dollarkoltsege ($).
  - Determinisztikus sporolas mutato (hany token lett megsporolva a Polyglot Analyzer es Context Cache hasznalataval).

---

## 3. WebSockets & Real-Time Event Architecture

Az Orchestrator Core egy belso **FastAPI + WebSocket** szervert futtat a nezet valos ideju frissitesehez.

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

## 4. Emberi Beavatkozas (HITL Control Panel)

Amikor a rendszer Preemption allapotba lep (`HITL` statusz):
- A dashboard kiemeli a blokkolt feladatot.
- A developernek 3 opcioja van az UI-on keresztul:
  1. **Retry with Feedback**: Egyedi instrukcio/utmutatas megadasa az agensnek.
  2. **Manual Edit & Resume**: A developer maga modositja a kodot a worktree-ben, majd `PASS` gombbal felulbiralja a tesztet.
  3. **Skip / Abort**: A feladat atugrasa vagy a teljes DAG leallitasa.
