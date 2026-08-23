# 23. Job Execution Layer & Safety Policy Service

> **Előfeltétele** a jövőbeli API rétegnek és a mobil kliensnek (worktree/agent kezelés szöveges inputtal), és a `docs/06_GLASS_BOX_UI.md`-ben már specifikált Web Dashboardnak. Ez a dokumentum a **backend**-oldalt írja le, amire mindkettő rá tud épülni.

## 0. Miért ez, és miért nem egy általános "vezessünk be repository/service/domain réteget" refactor

Egy alapos kódaudit (2026-08-23) megállapította, hogy az ai-os réteg-fegyelme **jó**:

- `ai_os/core/persistence.py` már egy valódi async repository Epic/Task/LockAudit/TokenCost fölött, dataclass-eket ad vissza, nem szivárogtat ORM-instance-t a hívóknak.
- `ai_os/mcp/protocol_router.py` + `ai_os/mcp/adapters/*` már egy tiszta ports-and-adapters minta (egy `base_adapter.py` interfész, cserélhető provider-implementációk).
- `EpicRunner`/`TaskRunner` dependency injectionnel épül (executor, sandbox runner, persistence mind paraméter, nem hardcodeolt import).

Egy generikus "repository/service/domain mappa-struktúra" bevezetés itt csak kód mozgatna, érdemi hibát vagy jövőbeli képességet nem oldana meg. **Két konkrét, valós hiány van**, és ez a dokumentum csak ezt a kettőt specifikálja.

## 1. Probléma: a biztonsági szabály a CLI parancsba van zárva

**Hol:** `ai_os/cli.py` kb. 809-826. sor, az `epic run` parancson belül.

```python
flagged = sensitive_paths({p for t in tasks for p in t.write_set})
if flagged:
    ...
    if merge_to_main:
        raise click.ClickException(
            "Refusing --merge-to-main: this plan touches security-sensitive files. ..."
        )
```

Ez a döntés — *"ha a terv CI/secrets/build/ai-os-config fájlt ír, `--merge-to-main` tilos, PR-en kell átmennie"* — üzleti/biztonsági szabály, nem CLI-prezentációs logika. Jelenleg csak akkor érvényesül, ha valaki a `click` parancson keresztül hívja az `epic run`-t. Egy jövőbeli API endpoint (amit a mobil app hívna) ezt **vagy újraírná (duplikáció, drift kockázata)**, vagy — rosszabb esetben — **elfelejtené**, és a guard megkerülhetővé válna egy másik belépési ponton.

### Javaslat

Kiemelni egy tiszta, I/O-mentes döntési függvénybe/osztályba:

```python
# ai_os/core/safety_policy.py (új fájl)

@dataclass(frozen=True)
class PlanSafetyDecision:
    flagged_paths: frozenset[str]
    merge_to_main_allowed: bool
    reason: str | None

def evaluate_plan_safety(tasks: Sequence[TaskNode], merge_to_main_requested: bool) -> PlanSafetyDecision:
    """Pure function. No I/O, no console, no click. Wraps sensitive_paths()."""
```

- A `cli.py` ezt hívja, és a jelenlegi `console.print`/`ClickException` réteg csak a `PlanSafetyDecision`-t formázza terminálra.
- Egy jövőbeli API endpoint (`POST /epics/{id}/plan/approve`) ugyanezt a függvényt hívja, és HTTP 409-et ad vissza `reason`-nel, ha `merge_to_main_allowed=False`.
- **Egy hely, egy szabály, két transport.**

Ez kis, izolált PR — a `sensitive_paths()` implementáció (`core/sensitive_files.py`) nem változik, csak a döntési pont kap nevet és költözik.

## 2. Probléma: nincs transport-agnosztikus Job/Event modell

**Hol:** `ai_os/cli.py`, `epic run` parancs, kb. 843-880. sor.

Jelenleg:
- Az `epic run` egy `asyncio.run(_execute())`-ba zárt, **a CLI-folyamat élettartamához kötött** blokkoló hívás. Amíg fut, a terminál blokkol.
- Az `on_event`/`on_status_change` callback-ek közvetlenül `console.print`-elnek (Rich).
- A HITL "Plan Review" gate (`docs/12`) egy `click.confirm(...)`, ami **csak ugyanazon a terminálon** válaszolható meg, ahonnan a parancs elindult.

Ez working CLI-re tökéletes, de nem tehető át semmilyen távoli kliensre (web dashboard, mobil app) anélkül, hogy valaki a folyamatot életben tartsa és a terminálhoz féljen hozzáférni. A `docs/06_GLASS_BOX_UI.md` már specifikál egy React+WebSocket Web Dashboardot (Phase 4b, még nincs megvalósítva) — de **nincs specifikálva az a backend-absztrakció, amit egy ilyen UI (vagy egy mobil app) ténylegesen meghívna.**

### Javaslat: `EpicJob` + `EventSink` Protocol

```python
# ai_os/core/job.py (új fájl)

class EventSink(Protocol):
    def emit(self, event: JobEvent) -> None: ...

@dataclass(frozen=True)
class JobEvent:
    job_id: str
    kind: Literal["task_status", "plan_ready", "awaiting_approval", "log_line", "completed", "failed"]
    payload: dict

class EpicJob:
    """Long-running handle over one `EpicRunner` execution. Owns its own
    asyncio task, can be cancelled, and fans events out to N EventSinks
    (not just one — a CLI console AND a persisted event log AND a WebSocket
    can all subscribe to the same run)."""

    def start(self) -> None: ...
    def cancel(self) -> None: ...
    def approve_plan(self) -> None: ...   # replaces click.confirm
    def status(self) -> JobStatus: ...
```

- `ConsoleEventSink` — a jelenlegi Rich-print viselkedés, változatlan UX a CLI-nek.
- `PersistedEventLogSink` — minden eseményt beír egy `job_events` táblába (új, kicsi tábla, ugyanabban a `Persistence` repóban). **Ez a darab teszi lehetővé, hogy egy mobil app *pollozva* vagy újracsatlakozva visszaolvassa, mi történt**, még ha épp nem volt kapcsolódva, amikor történt.
- A HITL "approve plan" gate `EpicJob.approve_plan()`-re vált a `click.confirm` helyett; a CLI ezt szinkron beviteli promptból hívja, egy jövőbeli API ezt egy `POST /jobs/{id}/approve` endpointból hívná — **ugyanaz a job-objektum, két hívó.**

### Mihez kell ez konkrétan a te két céljaidhoz

- **Mobil app:** enélkül nincs mit hívnia — a mobil kliens nem tud egy `asyncio.run()`-ba zárt terminál-parancsot "csatlakoztatni". Az `EpicJob` a legkisebb absztrakció, ami már API mögé tehető.
- **Belső karbantarthatóság:** a CLI parancs (`epic run`) ezután tényleg csak I/O-adapter lesz: parse args → `EpicJob` építés → `ConsoleEventSink` regisztrálás → `job.start()` → várakozás. Minden domain-döntés (safety, ütemezés, retry) már ma is a runnerekben van, ez nem változik.

## 3. Migrációs sorrend

1. **Előbb a 12 nyitott bug-issue (#10–33)** — ezek éles korrektségi/biztonsági kockázatok (task ID ütközés, PR-fallback merge, Gemini lockdown hiány, path traversal), és több közülük épp azokat a runnereket/CLI-utat érinti, amit ez a refactor is mozgatna. Kétszer dolgozni ugyanazon a kódon felesleges.
2. **Utána ez a két darab** (safety policy kiemelés, `EpicJob`/`EventSink`) — kis, izolált PR-ekben:
   - PR A: `safety_policy.py` kiemelés + `cli.py` hívja, tesztek átmozgatva.
   - PR B: `job.py` + `ConsoleEventSink`, `epic run` átírva rá, meglévő CLI UX identikus marad (regressziós teszt: a jelenlegi Rich-kimenet stringjei nem változnak).
   - PR C (opcionális, ha kell a `job_events` perzisztencia már most): `job_events` tábla + `PersistedEventLogSink`.
3. **Csak ezután** jöhet az API-réteg spec (`docs/24_API_LAYER.md`, még nem létezik) és a mobil app spec — mindkettő az `EpicJob`/`EventSink`/`safety_policy` hármasra épül majd, nem a runnerek belsejére közvetlenül.

## 4. Célfájl-térkép

| Fájl | Állapot | Tartalom |
|---|---|---|
| `ai_os/core/safety_policy.py` | **új** | `evaluate_plan_safety()`, tiszta függvény |
| `ai_os/core/job.py` | **új** | `EpicJob`, `JobEvent`, `EventSink` Protocol, `JobStatus` |
| `ai_os/core/job_sinks.py` | **új** | `ConsoleEventSink`, `PersistedEventLogSink` |
| `ai_os/core/db/models.py` | **bővül** | `JobEventModel` (job_id, kind, payload_json, created_at) |
| `ai_os/core/persistence.py` | **bővül** | `record_job_event()`, `list_job_events(job_id)` |
| `ai_os/cli.py` (`epic run`) | **átírva** | vékony I/O-adapter: `EpicJob` + `ConsoleEventSink` összerakása, safety-döntés hívása, `click.confirm` → `job.approve_plan()` |
| `ai_os/core/epic_runner.py`, `task_runner.py` | **változatlan** | a domain/orchestráció logika marad, csak a hívó fél (most `EpicJob`) cserélődik `cli.py` helyett |

## 5. Kapcsolat a meglévő specifikációkkal

- `docs/06_GLASS_BOX_UI.md` — a Web Dashboard (Phase 4b) ennek a `EventSink`/`JobEvent` modellnek lesz a WebSocket-fogyasztója; ez a dokumentum eddig nem specifikálta a backend-oldalt, ez a hiányzó darab.
- `docs/12_GLASS_BOX_UI_AND_HITL_SPEC.md` — a HITL gate-ek (`approve_plan` és a jövőbeli többi) az `EpicJob` metódusain keresztül lesznek elérhetők, transport-függetlenül.
