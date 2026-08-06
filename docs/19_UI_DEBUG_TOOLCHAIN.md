# 19. UI Debug Toolchain — determinisztikus UI-elemzés + kétlépcsős modell-routing

> **Státusz: tervdokumentum (design doc).** Ez a modul MÉG NINCS implementálva. A
> dokumentum a beépítés tervét, a moduláris felépítést és a meglévő AI-OS
> architektúrába (Phase 1 analyzer, MCP tools, sandbox, scheduler) illesztést
> írja le. A kód konvenció szerint angol; a próza magyar (lásd `CLAUDE.md`
> "Language note").

---

## 1. Motiváció

A felhasználó tipikus hibabejelentése nem kód, hanem **viselkedés**:

> „Az oldalon a *Mentés* gomb nem csinál semmit."
> „A mobil menü nem nyílik ki."
> „A kosárba rakás után nem frissül a szám a fejlécben."

A naiv megoldás az volna, hogy az egész `index.html` + a teljes CSS + a teljes JS
bundle-t bezúdítjuk egy drága modellbe, és megkérjük, „találd meg a hibát". Ez
három okból rossz:

1. **Kontextus-robbanás.** Egy modern SPA több százezer token nyers kódot jelent
   (bundle, framework, CSS). A hiba viszont jellemzően 1–3 elem + 1 handler
   körül van. A többi tiszta token-pazarlás — szemben az AI-OS 2. alapelvével
   (*Knowledge Before Generation*).
2. **Az LLM rosszul „lát".** A statikus kódból nehéz megmondani, hogy egy gomb
   *futásidőben* le van-e takarva egy overlay-jel, `pointer-events: none`-e, van-e
   ténylegesen bekötött listener, vagy a kattintásra induló fetch 404-et ad. Ez
   **futásidejű, determinisztikusan mérhető** információ.
3. **Drága modellt olvasásra használunk.** Egy nagy DOM/CSS/hálózati dump
   *átolvasása és szűrése* nem igényel csúcsmodellt — ezt egy olcsó modell is
   elvégzi. A drága modellt csak a tényleges **javításra** kell hívni.

Ez a modul az AI-OS 1. alapelvét (*Compiler First* — amit determinisztikusan meg
lehet csinálni, arra ne égess AI tokent) alkalmazza a UI-ra: **előbb
determinisztikus eszközök térképezik fel a UI-t és gyűjtik a bizonyítékokat,
aztán egy olcsó modell triage-el, és csak a fókuszált javítás megy drága
modellhez.**

---

## 2. A kétlépcsős filozófia egy mondatban

```
Determinisztikus gyűjtés (0 token)  →  Olcsó modell: diagnózis/triage  →  Drága modell: javítás  →  Sandbox (Playwright) validáció  →  PR
```

A drága modell soha nem lát nyers, több százezer tokenes dumpot — csak egy
tömörített, gyanú-rangsorolt diagnózist + a 1–3 érintett fájl/selector fókuszált
kontextusát.

---

## 3. Architektúra-áttekintés

```
[Felhasználói hibabejelentés: "a Mentés gomb nem működik"]
        │
        ▼
┌─────────────────────────────────────────────────────────────────────┐
│  DETERMINISZTIKUS UI-COLLECTOR RÉTEG  (0 LLM token)                   │
│                                                                       │
│  ┌────────────────────┐   ┌────────────────────┐   ┌──────────────┐  │
│  │ Static UI Graph     │   │ Dynamic Playwright │   │ Console/Net   │  │
│  │ (HTML/CSS/JS AST-ből)│  │ Probe (headless)   │   │ Capture       │  │
│  └─────────┬──────────┘   └─────────┬──────────┘   └──────┬───────┘  │
│            └───────────────┬────────┴───────────────┬─────┘          │
│                            ▼                         ▼                │
│                   [UI Knowledge Graph]      [Interaction Report]      │
│                            │                         │                │
│                            ▼                         ▼                │
│                   [Determinisztikus gyanú-detektorok (heuristics)]    │
└────────────────────────────┬──────────────────────────────────────---┘
                             ▼
        [Tömörített UI-kontextus + gyanú-rangsor]  (build_ui_context_cache)
                             │
                             ▼
        ┌────────────── OLCSÓ MODELL (triage) ──────────────┐
        │  Bemenet: a nagy determinisztikus dump             │
        │  Kimenet: "gyökérok-hipotézis + 1–3 érintett       │
        │           fájl/selector + a szükséges olvasnivaló" │
        └───────────────────────┬────────────────────────---┘
                             ▼
        ┌────────────── DRÁGA MODELL (fix) ─────────────────┐
        │  Bemenet: CSAK a fókuszált kontextus + a diagnózis │
        │  Eszközök: apply_file_edit / propose_file_patch /  │
        │           ui_probe_element / trigger_sandbox_valid.│
        └───────────────────────┬────────────────────────---┘
                             ▼
        [Sandbox: Playwright reprodukció + assertion + regressziós teszt]
                             │
                             ▼
                       [PR / merge]  (a meglévő EpicRunner/PR flow-n)
```

---

## 4. A determinisztikus collector réteg

Három, egymást kiegészítő forrás. Mindegyik **0 AI token**, mindegyik gépi
kimenetet ad, amit gráffá/riporttá konszolidálunk.

### 4.1. Static UI Graph builder (`ai_os/ui/static_graph.py`)

Épít a **Phase 1 analyzerre**: a `LanguageProfile` már parse-olja a HTML-t
(`tree-sitter-html`) és a CSS-t (`tree-sitter-css`), a JS/TS-t pedig teljes
szimbólum- és hívási gráffal (`ai_os/analyzer/`). Ez a modul ezekből egy
**UI-specifikus gráfot** épít, ami összeköti a három világot (DOM ↔ CSS ↔ JS).

Amit kinyer:

- **Interaktív elemek** a HTML-ből: `<button>`, `<a href>`, `<input>`,
  `<select>`, `<textarea>`, `<form>`, valamint bármi `role="button"`,
  `onclick=`, `tabindex`, `contenteditable` attribútummal. Framework-sablonoknál
  (JSX/TSX/Vue/Svelte) a template-részt is (lásd 4.4 korlátok).
- **Selectorok / azonosítók** minden elemhez: `id`, `class` lista,
  `data-testid`, `aria-label`, `name`, látható szöveg (text content) — ez a
  horgony, amivel a hibabejelentés („a *Mentés* gomb") az elemre képezhető.
- **CSS-szabályok**, amelyek egy elemre hatnak: a `tree-sitter-css`-ből kinyert
  szelektorok illesztése az elemekre (specificitás-sorrendben), külön kiemelve a
  **viselkedést befolyásoló** tulajdonságokat: `display`, `visibility`,
  `opacity`, `pointer-events`, `position`+`z-index`, `cursor`, `disabled`
  pszeudo, `@media` láthatóság.
- **Eseménykezelők** a JS-ből: inline `onclick`; `addEventListener('click', …)`
  hívások (a Phase 1 call-graph már látja a hívási helyeket); framework-handlerek
  (`onClick={…}` JSX-ben, `@click` Vue-ban, `on:click` Svelte-ben). Minden
  handlerhez visszakeressük, **melyik JS-szimbólum** a kezelő (`fetch_symbol_
  definition`-nel a skeleton is elérhető).
- **Bekötés (wiring)**: melyik selector melyik handlerhez van kötve, és melyik
  handler melyik hálózati hívást/DOM-mutációt indítja (statikusan, best-effort).

Gráfséma (a Phase 1 `KnowledgeEngine` `networkx.DiGraph` mintájára):

| Node típus     | Jelentés |
| -------------- | -------- |
| `ElementNode`  | egy interaktív DOM-elem (selector-készlettel, forrás fájl:sor) |
| `SelectorNode` | egy id/class/data-testid/aria-label horgony |
| `StyleRuleNode`| egy CSS-szabály (viselkedés-releváns tulajdonságokkal) |
| `HandlerNode`  | egy eseménykezelő (típus + a kezelő JS-szimbólum FQN-je) |
| `JsSymbolNode` | (a meglévő `FunctionNode`) a handler implementáció |
| `NetworkNode`  | egy statikusan látott `fetch`/`axios`/`XHR` endpoint |

| Él kind        | Irány / jelentés |
| -------------- | ---------------- |
| `MATCHES`      | SelectorNode → ElementNode |
| `STYLED_BY`    | ElementNode → StyleRuleNode |
| `HANDLED_BY`   | ElementNode → HandlerNode |
| `IMPLEMENTED_BY`| HandlerNode → JsSymbolNode |
| `CALLS`        | JsSymbolNode → NetworkNode/JsSymbolNode (Phase 1 CALLS) |
| `OCCLUDES`     | ElementNode → ElementNode (egy elem a stacking/pozíció alapján lefedi a másikat — best-effort statikus becslés, futásidőben a dinamikus probe erősíti meg) |

### 4.2. Dynamic Playwright probe (`ai_os/ui/playwright_probe.py`)

A statikus gráf megmondja, *mi lehet* — a dinamikus probe megmondja, *mi van
valójában* futásidőben. **Determinisztikus, szkriptelt, LLM nélkül.** Headless
Chromiumot indít (Playwright) egy futó dev-serveren vagy a buildelt statikus
oldalon, és minden interaktív elemre kimér egy strukturált riportot:

- **Láthatóság & geometria**: `boundingBox`, `isVisible`, `isEnabled`, a
  viewportban van-e, átfedések (a tényleges *elementFromPoint* a gomb közepén —
  ha nem a gomb jön vissza, valami **letakarja**; ez a klasszikus „a gomb ott
  van, de nem kattintható" hiba).
- **Számított stílusok**: `pointer-events`, `opacity`, `visibility`, `display`,
  `cursor`, `z-index` — a futásidejű, effektív érték (nem a statikus CSS).
- **Tényleg bekötött listenerek**: CDP-n keresztül (`DOMDebugger.
  getEventListeners`) — van-e egyáltalán `click` listener, és hány (dupla-
  bekötés detektálás).
- **Kattintás-szimuláció** (izoláltan, egyenként): mi történik kattintásra —
  indul-e hálózati kérés (és milyen státusszal tér vissza), dob-e a konzol
  hibát, változik-e a DOM (mutation observer), van-e navigáció, `preventDefault`
  megeszi-e az eseményt.
- **Akadálymentességi fa** (accessibility tree) részlet az elemre — a `role`/
  `name` tényleges, kiszámított értéke (ha a gombnak nincs elérhető neve, az is
  egy hiba-osztály).
- **Screenshot** az elemről + a környezetéről (a HITL PR-be tehető vizuális
  bizonyíték).

Kimenet: egy `InteractionReport` — elemenként a fenti mezők, JSON-ban.

> **Miért izoláltan, egyenként kattint?** Mert egy „minden gombot végigkattintó"
> szkript mellékhatásai (navigáció, állapotváltozás) elrontanák a többi elem
> mérését. A probe minden elem előtt visszaállítja az oldalt (reload vagy
> snapshot-restore), így minden mérés reprodukálható és független.

### 4.3. Console / Network capture (`ai_os/ui/runtime_capture.py`)

A probe futása alatt globálisan gyűjtjük: JS-hibák (`pageerror`), konzol-
`error`/`warn`, sikertelen hálózati kérések (4xx/5xx), CSP-violation-ök,
mixed-content figyelmeztetések. Ezek gyakran önmagukban rámutatnak a gyökérokra
(pl. a *Mentés* kattintás `POST /api/save` → 405-öt kap → a handler néma).

### 4.4. Framework-tudatosság és korlátok

- **Sablon-alapú frameworkök (React/Vue/Svelte/Angular):** a *statikus* handler-
  detektálás best-effort (a JSX `onClick={fn}` felismerhető, de a runtime-ban
  generált handlerek nem mindig). Ezt a **dinamikus probe kompenzálja**: a
  renderelt DOM-on a tényleges listenerek mérhetők, függetlenül attól, hogy a
  framework hogyan kötötte be őket. A két forrás együtt robusztus.
- **SPA hidratáció / időzítés:** a probe megvárja a `networkidle`-t és egy
  konfigurálható „ready" jelet (pl. egy selector megjelenését), mielőtt mér.
- **Auth-gated oldalak:** a probe elfogad egy opcionális előkészítő szkriptet
  (login-lépések vagy egy beinjektált session-cookie/token), `.ai-os/ui.json`-
  ban deklarálva — soha nem hardcode-olt titok (lásd 9. Biztonság).

---

## 5. Determinisztikus gyanú-detektorok (`ai_os/ui/detectors.py`)

A gráf + a riport fölött **LLM nélküli heurisztikák** futnak, amelyek a
leggyakoribb UI-bughoz vezető mintákat megjelölik és **rangsorolják**. Ez adja a
triage-modellnek a „hova nézz először" listát. Néhány detektor:

| Detektor | Gyanú |
| -------- | ----- |
| `no_handler` | interaktív elem, amelyhez futásidőben NINCS click-listener |
| `dead_handler` | a handler egy nem létező JS-szimbólumra/függvényre hivatkozik |
| `occluded` | az elem közepén az `elementFromPoint` mást ad vissza (letakarva) |
| `pointer_events_none` | effektív `pointer-events: none` az elemen vagy egy ősén |
| `hidden_but_present` | `opacity:0` / `visibility:hidden` / `display:none`, de a bejelentés szerint látszania kéne |
| `disabled` | `disabled` attribútum / `aria-disabled` (szándékos vagy hibás?) |
| `submit_outside_form` | `type="submit"` gomb `<form>`-on kívül |
| `duplicate_id` | ugyanaz az `id` többször — a selector nem determinisztikus |
| `double_bound` | ugyanarra az elemre két click-listener (dupla művelet/verseny) |
| `handler_throws` | a kattintás-szimuláció JS-hibát dobott |
| `failed_request` | a kattintásra induló fetch 4xx/5xx-öt adott |
| `no_accessible_name` | a gombnak nincs elérhető neve (a11y + gyakran törött szelektor) |
| `nav_swallowed` | `<a>`-ra `preventDefault`, de nincs helyettesítő navigáció |

Minden találat hordozza: az érintett elemet (selector + fájl:sor), a bizonyítékot
(mit mért), és egy súlyt. A hibabejelentés szövegét (a felhasználó „*Mentés*
gomb") **fuzzy-illesztjük** a selectorokra/szövegre, hogy a gyanúkat a bejelentett
elem köré rangsoroljuk.

> Fontos: a detektorok **nem döntenek**, csak jelölnek. A végső diagnózist és a
> javítást a modellek adják — a detektorok csak drasztikusan szűkítik a
> keresési teret (Compiler First).

---

## 6. MCP tool-felület — mit hív az LLM

A meglévő `ai_os/mcp/mcp_server.py` mintájára új eszközök, amelyeket az agent
(bármely provider natív tool-callingján át, `dispatch_tool_call`) hívhat. Az
eszközök **determinisztikusak** — az LLM dolga csak eldönteni, *melyiket* hívja,
és a kimenetet értelmezni:

- **`ui_scan(target, focus_hint?)`** — lefuttatja a teljes collector réteget
  (static graph + dynamic probe + capture + detektorok) és visszaadja a
  **tömörített UI-kontextust** (a `build_ui_context_cache` kimenetét — a Phase 1
  `build_context_cache` UI-megfelelője): a gyanú-rangsor + az érintett elemek
  skeletonjai + a releváns CSS/handler kivonatok. Ez a belépő eszköz.
- **`ui_list_interactive(target)`** — a felhasználó által kért „előre listázott
  gombok": minden interaktív elem + a bekötési státusza (handler van/nincs,
  látható, engedélyezett, letakart). Olcsó, gyors térkép.
- **`ui_probe_element(target, selector)`** — egyetlen elem mély vizsgálata
  (effektív stílusok, listenerek, kattintás-eredmény, screenshot). Ezt hívja a
  drága modell, ha egy konkrét elemre kell fókuszálnia — nem az egész oldalt
  olvassa újra.
- **`ui_reproduce(target, steps)`** — egy megadott lépés-sorozat (kattints X,
  írj be Y, várd Z) Playwright-lefuttatása; visszaadja, sikerült-e + a
  konzol/hálózati eredmény. Ez a „reprodukáld a hibát / igazold a javítást"
  eszköz — a **sandbox-validáció** is ezt használja.
- **`fetch_symbol_definition(fqn)`** — (meglévő) a handler JS-szimbólum
  skeletonjának lekérése.
- **`apply_file_edit` / `propose_file_patch`** — (meglévő) a tényleges javítás.
- **`trigger_sandbox_validation`** — (meglévő, Playwright-profillal) a javítás
  validálása.

`target` = a projekt + egy futtatható előnézet (dev-server URL vagy egy
`build`-parancs, amit a sandbox futtat) — `.ai-os/ui.json`-ban deklarálva.

---

## 7. Kétlépcsős modell-routing

A meglévő `DynamicScheduler` + `build_output_summarizer` mintát általánosítjuk
egy **UI-triage** lépéssé:

1. **Triage (olcsó, LOW-risk modell).** Bemenet: a `ui_scan` teljes
   determinisztikus dumpja (nagy, de olcsó modellnek adjuk). Kimenet egy szigorú
   sémában: `{root_cause_hypothesis, suspected_elements[selector], files_to_read,
   fix_strategy_summary}`. Ez az „olvasási feladat" — a nagy kontextus itt ég el,
   de olcsón.
2. **Fix (drága, HIGH/CRITICAL modell).** Bemenet: CSAK a triage kimenete + a
   `files_to_read` fókuszált skeletonjai + a gyanús elemek `ui_probe_element`
   riportjai. A drága modell sosem látja a teljes bundle-t. Eszközökkel javít,
   majd `ui_reproduce`/`trigger_sandbox_validation`-nel igazol.

Ez pontosan a projekt költség-tudatos filozófiája (risk → model), a UI-debug
domainre szabva. A routing az `AI_OS_MODEL_*` / `AI_OS_PROVIDER_ORDER_*` env-eken
finomhangolható, mint minden más taskra.

**Költség-intuíció.** Egy 300 000 tokenes nyers oldal drága modellen ≈ sokszorosa
annak, mint amikor egy olcsó modell egyszer átolvassa (triage), és a drága modell
csak egy ~5 000 tokenes fókuszált kontextust kap. A determinisztikus réteg pedig
eleve leszűri a 300k-t a releváns töredékre, mielőtt bármelyik modell látná.

---

## 8. Sandbox-validáció UI-ra

A javítás igazolása a meglévő **ephemeral Docker sandboxban** történik, egy
**Playwright-image profillal** (a `.ai-os/sandbox.json` már támogatja az `image`
override-ot — pl. `mcr.microsoft.com/playwright:...`). A validáció:

1. buildeli az oldalt (a projekt `build`/`dev` parancsa),
2. lefuttatja a `ui_reproduce` lépéseit **assertion-ökkel** (a gomb kattintható,
   a várt hálózati hívás 2xx, a várt DOM-változás bekövetkezik, nincs konzol-hiba),
3. a Phase 6 **test-presence** ellenőrzés elvárja, hogy a javításhoz **regressziós
   teszt** (egy Playwright/komponens-teszt) is készüljön — különben a PR-ben
   megjelölve „nincs teszt a változáshoz".

A DB-hez hasonlóan a Playwright a `--internal`/`--network none` politikát követi
(a demó statikus vagy a sandboxon belüli mock-backend ellen fut) — untrusted
oldal-JS-t futtatunk, ezért izolálva (lásd 9.).

---

## 9. Biztonság

- **Untrusted page JS.** A dinamikus probe idegen JavaScriptet futtat headless
  böngészőben. Ezt a sandbox-konténerben, hálózati izolációval kell futtatni, hogy
  ne érje el a host belső szolgáltatásait (a live szerver más projektjeit!). Soha
  ne a hoston, közvetlenül.
- **Titkok.** Az auth-gated probe login-adatai/tokenjei sosem hardcode-oltak és
  sosem kerülnek promptba/PR-be — a `.ai-os/ui.json` env-referenciát tartalmaz,
  a titok a szokásos `.env`-ből (gitignore-olt) jön, és a `sensitive_files` guard
  figyeli.
- **Screenshotok.** A PR-be tett képek tartalmazhatnak érzékeny UI-t; a HITL-
  reviewer dönt a megosztásról (nem publikáljuk automatikusan).

---

## 10. Integráció a meglévő flow-ba

- **Új CLI:** `ai-os ui-debug <project> --report "a Mentés gomb nem működik"
  [--url http://localhost:5173] [--merge-to-main]`. Lefut a collector →
  triage → fix → sandbox → PR pipeline, a szokásos HITL plan-review kapuval és
  observability-eventekkel (`on_event`).
- **Epic-taskként is:** egy UI-hibajavítás beilleszthető egy nagyobb epicbe mint
  egy `ui`-típusú task (a `resolve_task_language` mintájára a task „ui" profilt
  kap). Így egy „javítsd a checkout flow-t" epic vegyíthet backend + UI taskokat.
- **`.ai-os/ui.json`** (repo-side config, a `sandbox.json` testvére): a preview
  indítása (`dev_command`/`build_command`/`url`), a „ready" jel, opcionális
  auth-előkészítő, és a reprodukciós alap-lépések.

---

## 11. Determinisztikus vs LLM felelősségi mátrix

| Feladat | Ki csinálja |
| ------- | ----------- |
| HTML/CSS/JS parse, UI-gráf építés | **determinisztikus** (Tree-sitter + Phase 1) |
| Futásidejű mérés (láthatóság, listener, kattintás-hatás) | **determinisztikus** (Playwright) |
| Gyanús minták jelölése + rangsor | **determinisztikus** (detektorok) |
| Nagy dump átolvasása → gyökérok-hipotézis | **olcsó modell** (triage) |
| A tényleges kódjavítás | **drága modell** (fix) |
| A javítás igazolása | **determinisztikus** (Playwright sandbox) |
| Merge-döntés | **ember** (HITL / PR review) |

---

## 12. Tervezett modul-térkép (`ai_os/ui/`)

```
ai_os/ui/
  static_graph.py     # HTML/CSS/JS → UI Knowledge Graph (Phase 1-re építve)
  playwright_probe.py # headless futásidejű mérés → InteractionReport
  runtime_capture.py  # console/network/CSP gyűjtés
  detectors.py        # determinisztikus gyanú-heurisztikák + rangsor
  ui_graph.py         # a UI-gráf séma + build_ui_context_cache (tömörítés)
  ui_config.py        # .ai-os/ui.json betöltés/validálás
  mcp_tools.py        # ui_scan / ui_list_interactive / ui_probe_element / ui_reproduce
```

CLI: `ai-os ui-debug` a `cli.py`-ban; sandbox: Playwright-profil a
`container_runner.py`-ban (image override már van).

---

## 13. Ismert korlátok és trade-offök (nem elhallgatva)

- **Futó előnézet kell.** A dinamikus probe-hoz buildelhető/indítható oldal kell
  (dev-server vagy statikus build). Tisztán statikus HTML-nél triviális; komplex
  monorepónál a `.ai-os/ui.json` build-parancsára támaszkodunk.
- **Nem-determinisztikus oldalak.** Animációk, időzítők, véletlen adat → a probe
  `networkidle` + explicit ready-jel + reload-per-elem stratégiával stabilizál,
  de a teljes determinizmus nem garantált minden SPA-ra. A több forrás (statikus
  + dinamikus) redundanciája ezt csökkenti.
- **Framework-handler statikus felismerés** részleges — a dinamikus mérés
  kompenzál (4.4).
- **Vizuális/pixel-szintű bugok** (elcsúszott layout, rossz szín) ezen a
  toolchain-en kívül esnek — az egy jövőbeli *visual regression* kiterjesztés
  (14.), nem ez a modul.

---

## 14. Jövőbeli kiterjesztések

- **Visual regression** — screenshot-diff a javítás előtt/után (pixelmatch),
  layout-eltolódás (CLS) mérés.
- **Akadálymentességi audit** — teljes a11y-fa ellenőrzés (axe-core) beépítése a
  detektorok közé.
- **Cross-browser probe** — Firefox/WebKit is a Playwrighttal.
- **Teljesítmény** — a Cloudflare `web-perf` mintájú Core Web Vitals mérés a
  probe-ba.
- **„Record" mód** — a felhasználó egyszer végigkattintja a hibás flow-t, abból
  determinisztikus reprodukciós szkript generálódik.

---

## Kapcsolódó dokumentumok

- `03_POLYGLOT_ANALYZER.md` — a HTML/CSS/JS parse-réteg, amire a static graph épül.
- `08_KNOWLEDGE_GRAPH_AND_SUBGRAPH_EXTRACTION.md` — a k-hop / context-cache minta,
  amit a `build_ui_context_cache` UI-ra általánosít.
- `10_EPHEMERAL_CONTAINER_SANDBOX_SPEC.md` — a sandbox, amiben a Playwright-
  validáció fut (image override).
- `20_STARTUP_GENERATOR.md` — a statikus-oldal generátor, ami ugyanezt a
  Playwright-réteget használja smoke-tesztre.
```
