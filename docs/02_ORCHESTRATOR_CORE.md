# 02. Orchestrator Core Specification

Az **Orchestrator Core** az AI-OS kozponti vezerloegysege. Python 3.12+ nyelven irodott, es aszinkron esemenyhurkot (`asyncio`) hasznal a nagyfoku parhuzamosithatosag es alacsony valaszido erdekeben.

---

## 1. DAG Planner (Tervezo Modul)

A **DAG Planner** is responsible for felhasznalo altal megadott magas szintu keresek (Epic / User Story) felbontasaert atomi feladatokra (Tasks), as well as ezekbol egy **Directed Acyclic Graph (DAG)** felepiteseert.

### 1.1. Feladat Csomo (Task Node) Struktura
Minden feladatcsomo az alabbi attributumokkal rendelkezik:

```python
from pydantic import BaseModel, Field
from typing import List, Set, Optional, Literal

class TaskNode(BaseModel):
    id: str = Field(..., description="Egyedi feladat azonosito (pl. TASK-001)")
    title: str
    description: str
    risk_level: Literal["LOW", "MEDIUM", "HIGH", "CRITICAL"]
    target_files: List[str] = Field(..., description="Erintett fajlok listaja")
    read_set: Set[str] = Field(default_factory=set, description="Olvasasi zarolas ala eso fajlok")
    write_set: Set[str] = Field(default_factory=set, description="Irasi zarolas ala eso fajlok")
    dependencies: List[str] = Field(default_factory=list, description="Szulo feladat azonositok")
    status: Literal["PENDING", "READY", "RUNNING", "COMPLETED", "FAILED", "BLOCKED"] = "PENDING"
    max_retries: int = 3
    retry_count: int = 0
```

### 1.2. Topologiai Sorrend es Ciklusdetektalas
A DAG Planner a `networkx` konyvtarat hasznalja a feladatfuggosegi graf kezelesere:
- A ciklikus fuggosegeket a kodgeneralas elott detektalja es visszadobja a Planner LLM-nek korrigalasra (`networkx.is_directed_acyclic_graph`).
- A vegrehajtas topologiai sorrendben (`topological_sort`) vagy fuggosegi szintenkenti parhuzamositassal tortenik.

---

## 2. Dynamic Scheduler (Utemezo Modul)

A **Dynamic Scheduler** is responsible for `READY` allapotu feladatok kiosztasaert a legoptimalisabb AI modellhez (MCP Adapter).

### 2.1. Modell Kivalasztasi Matrix (Cost & Risk Awareness)

A rendszer a feladat kockazati szintje and the modositando reteg based on valaszt modellt:

| Risk Level | Feladat Tipusa | Ajanlott AI Modell | Koltseg / Token Profil |
| :--- | :--- | :--- | :--- |
| **LOW** | CSS/Style igazitasok, dokumentacio frissites, trivialis unit tesztek | Gemini 1.5 Flash / DeepSeek V3 | Extremely Low Cost |
| **MEDIUM** | Uj UI komponens, meglevo fuggveny refaktoralasa, bugfix | GPT-4o-mini / Claude 3.5 Haiku | Low/Medium Cost |
| **HIGH** | Uj API vegpont, adatbazis sema modositas, uzleti logika | Claude 3.5 Sonnet / GPT-4o | Premium Model |
| **CRITICAL** | Architektura valtas, biztonsagi modul, komplex algoritmikus DAG | Claude 3.5 Sonnet (High Temp/Reasoning) | Maximum Reasoning |

### 2.2. Terheles- es Kota-Kezeles (Rate Limiting)
- A Scheduler nyomon koveti a meglevo API kulcsok **TPM (Token Per Minute)** es **RPM (Request Per Minute)** korlatait.
- Ha egy premium modell elerte a limitet, a Scheduler feladat-varakoztatast (Backoff) leptet eletbe, vagy atiranyitja a feladatot egy egyenerteku tartalek (fallback) modellhez.

---

## 3. Lock Manager (Parhuzamossagi Zarolas Kezelo)

A tobb agens altali egyideju kodmodositas and the Merge Konfliktusok elkerulese erdekeben az AI-OS egy **Granularis Fajl Zarolo Rendszert** alkalmaz.

### 3.1. Read Set / Write Set Szabalyok
Minden feladat deklaralja a hozzaferesi igenyet:
- **Shared Read Lock (`read_set`)**: Tobb feladat is olvashatja ugyanazt a fajlt egyidejuleg.
- **Exclusive Write Lock (`write_set`)**: Egy fajlt egy adott pillanatban csak egyetlen aktiv feladat modosithat (`write_set`).

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
                # Ellenorizzuk, hogy barmelyik write_set fajl zarolva van-e (akar olvasasra, akar irasra)
                write_conflict = any(f in self._write_locks or self._read_locks.get(f, 0) > 0 for f in write_set)
                read_conflict = any(f in self._write_locks for f in read_set)
                
                if not write_conflict and not read_conflict:
                    # Zarolasok lefoglalasa
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

### 3.2. Parhuzamos Vegrehajtas Git Worktrees Segitsegevel
Amennyiben ket fuggetlen feladat Write Set-je diszjunkt (`TaskA.write_set ∩ TaskB.write_set = ∅`), a Lock Manager engedelyezi a ket feladat **parhuzamos futtatasat** kulon Git Worktree kornyezetben.
