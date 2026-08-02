# 02. Orchestrator Core Specification

Az **Orchestrator Core** az AI-OS központi vezérlőegysége. Python 3.12+ nyelven íródott, és aszinkron eseményhurkot (`asyncio`) használ a nagyfokú párhuzamosíthatóság és alacsony válaszidő érdekében.

---

## 1. DAG Planner (Tervező Modul)

A **DAG Planner** felelős a felhasználó által megadott magas szintű kérések (Epic / User Story) felbontásáért atomi feladatokra (Tasks), valamint ezekből egy **Directed Acyclic Graph (DAG)** felépítéséért.

### 1.1. Feladat Csomó (Task Node) Struktúra
Minden feladatcsomó az alábbi attribútumokkal rendelkezik:

```python
from pydantic import BaseModel, Field
from typing import List, Set, Optional, Literal

class TaskNode(BaseModel):
    id: str = Field(..., description="Egyedi feladat azonosító (pl. TASK-001)")
    title: str
    description: str
    risk_level: Literal["LOW", "MEDIUM", "HIGH", "CRITICAL"]
    target_files: List[str] = Field(..., description="Érintett fájlok listája")
    read_set: Set[str] = Field(default_factory=set, description="Olvasási zárolás alá eső fájlok")
    write_set: Set[str] = Field(default_factory=set, description="Írási zárolás alá eső fájlok")
    dependencies: List[str] = Field(default_factory=list, description="Szülő feladat azonosítók")
    status: Literal["PENDING", "READY", "RUNNING", "COMPLETED", "FAILED", "BLOCKED"] = "PENDING"
    max_retries: int = 3
    retry_count: int = 0
```

### 1.2. Topológiai Sorrend és Ciklusdetektálás
A DAG Planner a `networkx` könyvtárat használja a feladatfüggőségi gráf kezelésére:
- A ciklikus függőségeket a kódgenerálás előtt detektálja és visszadobja a Planner LLM-nek korrigálásra (`networkx.is_directed_acyclic_graph`).
- A végrehajtás topológiai sorrendben (`topological_sort`) vagy függőségi szintenkénti párhuzamosítással történik.

---

## 2. Dynamic Scheduler (Ütemező Modul)

A **Dynamic Scheduler** felelős a `READY` állapotú feladatok kiosztásáért a legoptimálisabb AI modellhez (MCP Adapter).

### 2.1. Modell Kiválasztási Mátrix (Cost & Risk Awareness)

A rendszer a feladat kockázati szintje és a módosítandó réteg alapján választ modellt:

| Risk Level | Feladat Típusa | Ajánlott AI Modell | Költség / Token Profil |
| :--- | :--- | :--- | :--- |
| **LOW** | CSS/Style igazítások, dokumentáció frissítés, triviális unit tesztek | Gemini 1.5 Flash / DeepSeek V3 | Extremely Low Cost |
| **MEDIUM** | Új UI komponens, meglévő függvény refaktorálása, bugfix | GPT-4o-mini / Claude 3.5 Haiku | Low/Medium Cost |
| **HIGH** | Új API végpont, adatbázis séma módosítás, üzleti logika | Claude 3.5 Sonnet / GPT-4o | Premium Model |
| **CRITICAL** | Architektúra váltás, biztonsági modul, komplex algoritmikus DAG | Claude 3.5 Sonnet (High Temp/Reasoning) | Maximum Reasoning |

### 2.2. Terhelés- és Kóta-Kezelés (Rate Limiting)
- A Scheduler nyomon követi a meglévő API kulcsok **TPM (Token Per Minute)** és **RPM (Request Per Minute)** korlátait.
- Ha egy prémium modell elérte a limitet, a Scheduler feladat-várakoztatást (Backoff) léptet életbe, vagy átirányítja a feladatot egy egyenértékű tartalék (fallback) modellhez.

---

## 3. Lock Manager (Párhuzamossági Zárolás Kezelő)

A több ágens általi egyidejű kódmódosítás és a Merge Konfliktusok elkerülése érdekében az AI-OS egy **Granuláris Fájl Zároló Rendszert** alkalmaz.

### 3.1. Read Set / Write Set Szabályok
Minden feladat deklarálja a hozzáférési igényét:
- **Shared Read Lock (`read_set`)**: Több feladat is olvashatja ugyanazt a fájlt egyidejűleg.
- **Exclusive Write Lock (`write_set`)**: Egy fájlt egy adott pillanatban csak egyetlen aktív feladat módosíthat (`write_set`).

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
                # Ellenőrizzük, hogy bármelyik write_set fájl zárolva van-e (akár olvasásra, akár írásra)
                write_conflict = any(f in self._write_locks or self._read_locks.get(f, 0) > 0 for f in write_set)
                read_conflict = any(f in self._write_locks for f in read_set)
                
                if not write_conflict and not read_conflict:
                    # Zárolások lefoglalása
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

### 3.2. Párhuzamos Végrehajtás Git Worktrees Segítségével
Amennyiben két független feladat Write Set-je diszjunkt (`TaskA.write_set ∩ TaskB.write_set = ∅`), a Lock Manager engedélyezi a két feladat **párhuzamos futtatását** külön Git Worktree környezetben.
