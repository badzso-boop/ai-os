# 19. UI Debug Toolchain — determinisztikus UI-elemzes + ketlepcsos modell-routing

> **Statusz: design document (design doc).** This module NOT YET implemented. A
> dokumentum a beepites tervet, a modularis felepitest and the meglevo AI-OS
> architekturaba (Phase 1 analyzer, MCP tools, sandbox, scheduler) illesztest
> irja le. A kod konvencio according to angol; a proza magyar (lasd `CLAUDE.md`
> "Language note").

---

## 1. Motivacio

A felhasznalo tipikus hibabejelentese nem kod, hanem **viselkedes**:

> „Az oldalon a *Mentes* gomb nem csinal semmit."
> „A mobil menu nem nyilik ki."
> „A kosarba rakas utan nem frissul a szam a fejlecben."

A naiv megoldas az volna, that the egesz `index.html` + a teljes CSS + a teljes JS
bundle-t bezuditjuk egy draga modellbe, es megkerjuk, „talald meg a hibat". Ez
harom okbol rossz:

1. **Kontextus-robbanas.** Egy modern SPA tobb szazezer token nyers kodot jelent
   (bundle, framework, CSS). A hiba viszont jellemzoen 1–3 elem + 1 handler
   korul van. A tobbi tiszta token-pazarlas — szemben az AI-OS 2. alapelvevel
   (*Knowledge Before Generation*).
2. **Az LLM rosszul „lat".** A statikus kodbol nehez megmondani, hogy egy gomb
   *futasidoben* le van-e takarva egy overlay-jel, `pointer-events: none`-e, van-e
   tenylegesen bekotott listener, vagy a kattintasra indulo fetch 404-et ad. Ez
   **futasideju, determinisztikusan merheto** informacio.
3. **Draga modellt olvasasra hasznalunk.** Egy nagy DOM/CSS/halozati dump
   *atolvasasa es szurese* nem igenyel csucsmodellt — ezt egy olcso modell is
   elvegzi. A draga modellt csak a tenyleges **javitasra** kell hivni.

This module az AI-OS 1. alapelvet (*Compiler First* — amit determinisztikusan meg
lehet csinalni, arra ne egess AI tokent) alkalmazza a UI-ra: **elobb
determinisztikus eszkozok terkepezik fel a UI-t es gyujtik a bizonyitekokat,
aztan egy olcso modell triage-el, es csak a fokuszalt javitas megy draga
modellhez.**

---

## 2. A ketlepcsos filozofia egy mondatban

```
Determinisztikus gyujtes (0 token)  →  Olcso modell: diagnozis/triage  →  Draga modell: javitas  →  Sandbox (Playwright) validacio  →  PR
```

A draga modell soha nem lat nyers, tobb szazezer tokenes dumpot — csak egy
tomoritett, gyanu-rangsorolt diagnozist + a 1–3 erintett fajl/selector fokuszalt
kontextusat.

---

## 3. Architektura-attekintes

```
[Felhasznaloi hibabejelentes: "a Mentes gomb nem mukodik"]
        │
        ▼
┌─────────────────────────────────────────────────────────────────────┐
│  DETERMINISZTIKUS UI-COLLECTOR RETEG  (0 LLM token)                   │
│                                                                       │
│  ┌────────────────────┐   ┌────────────────────┐   ┌──────────────┐  │
│  │ Static UI Graph     │   │ Dynamic Playwright │   │ Console/Net   │  │
│  │ (HTML/CSS/JS AST-bol)│  │ Probe (headless)   │   │ Capture       │  │
│  └─────────┬──────────┘   └─────────┬──────────┘   └──────┬───────┘  │
│            └───────────────┬────────┴───────────────┬─────┘          │
│                            ▼                         ▼                │
│                   [UI Knowledge Graph]      [Interaction Report]      │
│                            │                         │                │
│                            ▼                         ▼                │
│                   [Determinisztikus gyanu-detektorok (heuristics)]    │
└────────────────────────────┬──────────────────────────────────────---┘
                             ▼
        [Tomoritett UI-kontextus + gyanu-rangsor]  (build_ui_context_cache)
                             │
                             ▼
        ┌────────────── OLCSO MODELL (triage) ──────────────┐
        │  Bemenet: a nagy determinisztikus dump             │
        │  Kimenet: "gyokerok-hipotezis + 1–3 erintett       │
        │           fajl/selector + a szukseges olvasnivalo" │
        └───────────────────────┬────────────────────────---┘
                             ▼
        ┌────────────── DRAGA MODELL (fix) ─────────────────┐
        │  Bemenet: CSAK a fokuszalt kontextus + a diagnozis │
        │  Eszkozok: apply_file_edit / propose_file_patch /  │
        │           ui_probe_element / trigger_sandbox_valid.│
        └───────────────────────┬────────────────────────---┘
                             ▼
        [Sandbox: Playwright reprodukcio + assertion + regresszios teszt]
                             │
                             ▼
                       [PR / merge]  (a meglevo EpicRunner/PR flow-n)
```

---

## 4. A determinisztikus collector reteg

Harom, egymast kiegeszito forras. Mindegyik **0 AI token**, mindegyik gepi
kimenetet ad, amit graffa/riportta konszolidalunk.

### 4.1. Static UI Graph builder (`ai_os/ui/static_graph.py`)

Epit a **Phase 1 analyzerre**: a `LanguageProfile` mar parse-olja a HTML-t
(`tree-sitter-html`) and the CSS-t (`tree-sitter-css`), a JS/TS-t pedig teljes
szimbolum- es hivasi graffal (`ai_os/analyzer/`). This module ezekbol egy
**UI-specifikus grafot** epit, ami osszekoti a harom vilagot (DOM ↔ CSS ↔ JS).

Amit kinyer:

- **Interaktiv elemek** a HTML-bol: `<button>`, `<a href>`, `<input>`,
  `<select>`, `<textarea>`, `<form>`, as well as barmi `role="button"`,
  `onclick=`, `tabindex`, `contenteditable` attributummal. Framework-sablonoknal
  (JSX/TSX/Vue/Svelte) a template-reszt is (lasd 4.4 korlatok).
- **Selectorok / azonositok** minden elemhez: `id`, `class` lista,
  `data-testid`, `aria-label`, `name`, lathato szoveg (text content) — ez a
  horgony, amivel a hibabejelentes („a *Mentes* gomb") az elemre kepezheto.
- **CSS-szabalyok**, amelyek egy elemre hatnak: a `tree-sitter-css`-bol kinyert
  szelektorok illesztese az elemekre (specificitas-sorrendben), kulon kiemelve a
  **viselkedest befolyasolo** tulajdonsagokat: `display`, `visibility`,
  `opacity`, `pointer-events`, `position`+`z-index`, `cursor`, `disabled`
  pszeudo, `@media` lathatosag.
- **Esemenykezelok** a JS-bol: inline `onclick`; `addEventListener('click', …)`
  hivasok (a Phase 1 call-graph mar latja a hivasi helyeket); framework-handlerek
  (`onClick={…}` JSX-ben, `@click` Vue-ban, `on:click` Svelte-ben). Minden
  handlerhez visszakeressuk, **melyik JS-szimbolum** a kezelo (`fetch_symbol_
  definition`-nel a skeleton is elerheto).
- **Bekotes (wiring)**: melyik selector melyik handlerhez van kotve, es melyik
  handler melyik halozati hivast/DOM-mutaciot inditja (statikusan, best-effort).

Grafsema (a Phase 1 `KnowledgeEngine` `networkx.DiGraph` mintajara):

| Node tipus     | Jelentes |
| -------------- | -------- |
| `ElementNode`  | egy interaktiv DOM-elem (selector-keszlettel, forras fajl:sor) |
| `SelectorNode` | egy id/class/data-testid/aria-label horgony |
| `StyleRuleNode`| egy CSS-szabaly (viselkedes-relevans tulajdonsagokkal) |
| `HandlerNode`  | egy esemenykezelo (tipus + a kezelo JS-szimbolum FQN-je) |
| `JsSymbolNode` | (a meglevo `FunctionNode`) a handler implementacio |
| `NetworkNode`  | egy statikusan latott `fetch`/`axios`/`XHR` endpoint |

| El kind        | Irany / jelentes |
| -------------- | ---------------- |
| `MATCHES`      | SelectorNode → ElementNode |
| `STYLED_BY`    | ElementNode → StyleRuleNode |
| `HANDLED_BY`   | ElementNode → HandlerNode |
| `IMPLEMENTED_BY`| HandlerNode → JsSymbolNode |
| `CALLS`        | JsSymbolNode → NetworkNode/JsSymbolNode (Phase 1 CALLS) |
| `OCCLUDES`     | ElementNode → ElementNode (egy elem a stacking/pozicio based on lefedi a masikat — best-effort statikus becsles, futasidoben a dinamikus probe erositi meg) |

### 4.2. Dynamic Playwright probe (`ai_os/ui/playwright_probe.py`)

A statikus graf megmondja, *mi lehet* — a dinamikus probe megmondja, *mi van
valojaban* futasidoben. **Determinisztikus, szkriptelt, LLM nelkul.** Headless
Chromiumot indit (Playwright) egy futo dev-serveren vagy a buildelt statikus
oldalon, es minden interaktiv elemre kimer egy strukturalt riportot:

- **Lathatosag & geometria**: `boundingBox`, `isVisible`, `isEnabled`, a
  viewportban van-e, atfedesek (a tenyleges *elementFromPoint* a gomb kozepen —
  ha nem a gomb jon vissza, valami **letakarja**; ez a klasszikus „a gomb ott
  van, de nem kattinthato" hiba).
- **Szamitott stilusok**: `pointer-events`, `opacity`, `visibility`, `display`,
  `cursor`, `z-index` — a futasideju, effektiv ertek (nem a statikus CSS).
- **Tenyleg bekotott listenerek**: CDP-n keresztul (`DOMDebugger.
  getEventListeners`) — van-e egyaltalan `click` listener, es hany (dupla-
  bekotes detektalas).
- **Kattintas-szimulacio** (izolaltan, egyenkent): mi tortenik kattintasra —
  indul-e halozati keres (es milyen statusszal ter vissza), dob-e a konzol
  hibat, valtozik-e a DOM (mutation observer), van-e navigacio, `preventDefault`
  megeszi-e az esemenyt.
- **Akadalymentessegi fa** (accessibility tree) reszlet az elemre — a `role`/
  `name` tenyleges, kiszamitott erteke (ha a gombnak nincs elerheto neve, az is
  egy hiba-osztaly).
- **Screenshot** az elemrol + a kornyezeterol (a HITL PR-be teheto vizualis
  bizonyitek).

Kimenet: egy `InteractionReport` — elemenkent a fenti mezok, JSON-ban.

> **Miert izolaltan, egyenkent kattint?** Mert egy „minden gombot vegigkattinto"
> szkript mellekhatasai (navigacio, allapotvaltozas) elrontanak a tobbi elem
> mereset. A probe minden elem elott visszaallitja az oldalt (reload vagy
> snapshot-restore), igy minden meres reprodukalhato es fuggetlen.

### 4.3. Console / Network capture (`ai_os/ui/runtime_capture.py`)

A probe futasa alatt globalisan gyujtjuk: JS-hibak (`pageerror`), konzol-
`error`/`warn`, sikertelen halozati keresek (4xx/5xx), CSP-violation-ok,
mixed-content figyelmeztetesek. Ezek gyakran onmagukban ramutatnak a gyokerokra
(pl. a *Mentes* kattintas `POST /api/save` → 405-ot kap → a handler nema).

### 4.4. Framework-tudatossag es korlatok

- **Sablon-alapu frameworkok (React/Vue/Svelte/Angular):** a *statikus* handler-
  detektalas best-effort (a JSX `onClick={fn}` felismerheto, de a runtime-ban
  generalt handlerek nem mindig). Ezt a **dinamikus probe kompenzalja**: a
  renderelt DOM-on a tenyleges listenerek merhetok, fuggetlenul attol, that the
  framework hogyan kototte be oket. A ket forras egyutt robusztus.
- **SPA hidratacio / idozites:** a probe megvarja a `networkidle`-t es egy
  konfiguralhato „ready" jelet (pl. egy selector megjeleneset), mielott mer.
- **Auth-gated oldalak:** a probe elfogad egy opcionalis elokeszito szkriptet
  (login-lepesek vagy egy beinjektalt session-cookie/token), `.ai-os/ui.json`-
  ban deklaralva — soha nem hardcode-olt titok (lasd 9. Biztonsag).

---

## 5. Determinisztikus gyanu-detektorok (`ai_os/ui/detectors.py`)

A graf + a riport folott **LLM nelkuli heurisztikak** futnak, amelyek a
leggyakoribb UI-bughoz vezeto mintakat megjelolik es **rangsoroljak**. Ez adja a
triage-modellnek a „hova nezz eloszor" listat. Nehany detektor:

| Detektor | Gyanu |
| -------- | ----- |
| `no_handler` | interaktiv elem, amelyhez futasidoben NINCS click-listener |
| `dead_handler` | a handler egy nem letezo JS-szimbolumra/fuggvenyre hivatkozik |
| `occluded` | az elem kozepen az `elementFromPoint` mast ad vissza (letakarva) |
| `pointer_events_none` | effektiv `pointer-events: none` az elemen vagy egy osen |
| `hidden_but_present` | `opacity:0` / `visibility:hidden` / `display:none`, de a bejelentes according to latszania kene |
| `disabled` | `disabled` attributum / `aria-disabled` (szandekos vagy hibas?) |
| `submit_outside_form` | `type="submit"` gomb `<form>`-on kivul |
| `duplicate_id` | ugyanaz az `id` tobbszor — a selector nem determinisztikus |
| `double_bound` | ugyanarra az elemre ket click-listener (dupla muvelet/verseny) |
| `handler_throws` | a kattintas-szimulacio JS-hibat dobott |
| `failed_request` | a kattintasra indulo fetch 4xx/5xx-ot adott |
| `no_accessible_name` | a gombnak nincs elerheto neve (a11y + gyakran torott szelektor) |
| `nav_swallowed` | `<a>`-ra `preventDefault`, de nincs helyettesito navigacio |

Minden talalat hordozza: az erintett elemet (selector + fajl:sor), a bizonyitekot
(mit mert), es egy sulyt. A hibabejelentes szoveget (a felhasznalo „*Mentes*
gomb") **fuzzy-illesztjuk** a selectorokra/szovegre, that the gyanukat a bejelentett
elem kore rangsoroljuk.

> Fontos: a detektorok **nem dontenek**, csak jelolnek. A vegso diagnozist and the
> javitast a modellek adjak — a detektorok csak drasztikusan szukitik a
> keresesi teret (Compiler First).

---

## 6. MCP tool-felulet — mit hiv az LLM

A meglevo `ai_os/mcp/mcp_server.py` mintajara uj eszkozok, amelyeket az agent
(barmely provider nativ tool-callingjan at, `dispatch_tool_call`) hivhat. Az
eszkozok **determinisztikusak** — az LLM dolga csak eldonteni, *melyiket* hivja,
and the kimenetet ertelmezni:

- **`ui_scan(target, focus_hint?)`** — lefuttatja a teljes collector reteget
  (static graph + dynamic probe + capture + detektorok) es visszaadja a
  **tomoritett UI-kontextust** (a `build_ui_context_cache` kimenetet — a Phase 1
  `build_context_cache` UI-megfeleloje): a gyanu-rangsor + az erintett elemek
  skeletonjai + a relevans CSS/handler kivonatok. Ez a belepo eszkoz.
- **`ui_list_interactive(target)`** — a felhasznalo altal kert „elore listazott
  gombok": minden interaktiv elem + a bekotesi statusza (handler van/nincs,
  lathato, engedelyezett, letakart). Olcso, gyors terkep.
- **`ui_probe_element(target, selector)`** — egyetlen elem mely vizsgalata
  (effektiv stilusok, listenerek, kattintas-eredmeny, screenshot). Ezt hivja a
  draga modell, ha egy konkret elemre kell fokuszalnia — nem az egesz oldalt
  olvassa ujra.
- **`ui_reproduce(target, steps)`** — egy megadott lepes-sorozat (kattints X,
  irj be Y, vard Z) Playwright-lefuttatasa; visszaadja, sikerult-e + a
  konzol/halozati eredmeny. Ez a „reprodukald a hibat / igazold a javitast"
  eszkoz — a **sandbox-validacio** is ezt hasznalja.
- **`fetch_symbol_definition(fqn)`** — (meglevo) a handler JS-szimbolum
  skeletonjanak lekerese.
- **`apply_file_edit` / `propose_file_patch`** — (meglevo) a tenyleges javitas.
- **`trigger_sandbox_validation`** — (meglevo, Playwright-profillal) a javitas
  validalasa.

`target` = a projekt + egy futtathato elonezet (dev-server URL vagy egy
`build`-parancs, amit a sandbox futtat) — `.ai-os/ui.json`-ban deklaralva.

---

## 7. Ketlepcsos modell-routing

A meglevo `DynamicScheduler` + `build_output_summarizer` mintat altalanositjuk
egy **UI-triage** lepesse:

1. **Triage (olcso, LOW-risk modell).** Bemenet: a `ui_scan` teljes
   determinisztikus dumpja (nagy, de olcso modellnek adjuk). Kimenet egy szigoru
   semaban: `{root_cause_hypothesis, suspected_elements[selector], files_to_read,
   fix_strategy_summary}`. Ez az „olvasasi feladat" — a nagy kontextus itt eg el,
   de olcson.
2. **Fix (draga, HIGH/CRITICAL modell).** Bemenet: CSAK a triage kimenete + a
   `files_to_read` fokuszalt skeletonjai + a gyanus elemek `ui_probe_element`
   riportjai. A draga modell sosem latja a teljes bundle-t. Eszkozokkel javit,
   majd `ui_reproduce`/`trigger_sandbox_validation`-nel igazol.

Ez pontosan a projekt koltseg-tudatos filozofiaja (risk → model), a UI-debug
domainre szabva. A routing az `AI_OS_MODEL_*` / `AI_OS_PROVIDER_ORDER_*` env-eken
finomhangolhato, mint minden mas taskra.

**Koltseg-intuicio.** Egy 300 000 tokenes nyers oldal draga modellen ≈ sokszorosa
annak, mint amikor egy olcso modell egyszer atolvassa (triage), and the draga modell
csak egy ~5 000 tokenes fokuszalt kontextust kap. A determinisztikus reteg pedig
eleve leszuri a 300k-t a relevans toredekre, mielott barmelyik modell latna.

---

## 8. Sandbox-validacio UI-ra

A javitas igazolasa a meglevo **ephemeral Docker sandboxban** tortenik, egy
**Playwright-image profillal** (a `.ai-os/sandbox.json` mar tamogatja az `image`
override-ot — pl. `mcr.microsoft.com/playwright:...`). A validacio:

1. buildeli az oldalt (a projekt `build`/`dev` parancsa),
2. lefuttatja a `ui_reproduce` lepeseit **assertion-okkel** (a gomb kattinthato,
   a vart halozati hivas 2xx, a vart DOM-valtozas bekovetkezik, nincs konzol-hiba),
3. a Phase 6 **test-presence** ellenorzes elvarja, that the javitashoz **regresszios
   teszt** (egy Playwright/komponens-teszt) is keszuljon — kulonben a PR-ben
   megjelolve „nincs teszt a valtozashoz".

A DB-hez hasonloan a Playwright a `--internal`/`--network none` politikat koveti
(a demo statikus vagy a sandboxon beluli mock-backend ellen fut) — untrusted
oldal-JS-t futtatunk, ezert izolalva (lasd 9.).

---

## 9. Biztonsag

- **Untrusted page JS.** A dinamikus probe idegen JavaScriptet futtat headless
  bongeszoben. Ezt a sandbox-kontenerben, halozati izolacioval kell futtatni, hogy
  ne erje el a host belso szolgaltatasait (a live szerver mas projektjeit!). Soha
  ne a hoston, kozvetlenul.
- **Titkok.** Az auth-gated probe login-adatai/tokenjei sosem hardcode-oltak es
  sosem kerulnek promptba/PR-be — a `.ai-os/ui.json` env-referenciat tartalmaz,
  a titok a szokasos `.env`-bol (gitignore-olt) jon, and the `sensitive_files` guard
  figyeli.
- **Screenshotok.** A PR-be tett kepek tartalmazhatnak erzekeny UI-t; a HITL-
  reviewer dont a megosztasrol (nem publikaljuk automatikusan).

---

## 10. Integracio a meglevo flow-ba

- **Uj CLI:** `ai-os ui-debug <project> --report "a Mentes gomb nem mukodik"
  [--url http://localhost:5173] [--merge-to-main]`. Lefut a collector →
  triage → fix → sandbox → PR pipeline, a szokasos HITL plan-review kapuval es
  observability-eventekkel (`on_event`).
- **Epic-taskkent is:** egy UI-hibajavitas beillesztheto egy nagyobb epicbe mint
  egy `ui`-tipusu task (a `resolve_task_language` mintajara a task „ui" profilt
  kap). Igy egy „javitsd a checkout flow-t" epic vegyithet backend + UI taskokat.
- **`.ai-os/ui.json`** (repo-side config, a `sandbox.json` testvere): a preview
  inditasa (`dev_command`/`build_command`/`url`), a „ready" jel, opcionalis
  auth-elokeszito, and the reprodukcios alap-lepesek.

---

## 11. Determinisztikus vs LLM felelossegi matrix

| Feladat | Ki csinalja |
| ------- | ----------- |
| HTML/CSS/JS parse, UI-graf epites | **determinisztikus** (Tree-sitter + Phase 1) |
| Futasideju meres (lathatosag, listener, kattintas-hatas) | **determinisztikus** (Playwright) |
| Gyanus mintak jelolese + rangsor | **determinisztikus** (detektorok) |
| Nagy dump atolvasasa → gyokerok-hipotezis | **olcso modell** (triage) |
| A tenyleges kodjavitas | **draga modell** (fix) |
| A javitas igazolasa | **determinisztikus** (Playwright sandbox) |
| Merge-dontes | **ember** (HITL / PR review) |

---

## 12. Tervezett modul-terkep (`ai_os/ui/`)

```
ai_os/ui/
  static_graph.py     # HTML/CSS/JS → UI Knowledge Graph (Phase 1-re epitve)
  playwright_probe.py # headless futasideju meres → InteractionReport
  runtime_capture.py  # console/network/CSP gyujtes
  detectors.py        # determinisztikus gyanu-heurisztikak + rangsor
  ui_graph.py         # a UI-graf sema + build_ui_context_cache (tomorites)
  ui_config.py        # .ai-os/ui.json betoltes/validalas
  mcp_tools.py        # ui_scan / ui_list_interactive / ui_probe_element / ui_reproduce
```

CLI: `ai-os ui-debug` a `cli.py`-ban; sandbox: Playwright-profil a
`container_runner.py`-ban (image override mar van).

---

## 13. Ismert korlatok es trade-offok (nem elhallgatva)

- **Futo elonezet kell.** A dinamikus probe-hoz buildelheto/indithato oldal kell
  (dev-server vagy statikus build). Tisztan statikus HTML-nel trivialis; komplex
  monoreponal a `.ai-os/ui.json` build-parancsara tamaszkodunk.
- **Nem-determinisztikus oldalak.** Animaciok, idozitok, veletlen adat → a probe
  `networkidle` + explicit ready-jel + reload-per-elem strategiaval stabilizal,
  de a teljes determinizmus nem garantalt minden SPA-ra. A tobb forras (statikus
  + dinamikus) redundanciaja ezt csokkenti.
- **Framework-handler statikus felismeres** reszleges — a dinamikus meres
  kompenzal (4.4).
- **Vizualis/pixel-szintu bugok** (elcsuszott layout, rossz szin) ezen a
  toolchain-en kivul esnek — az egy jovobeli *visual regression* kiterjesztes
  (14.), nem This module.

---

## 14. Jovobeli kiterjesztesek

- **Visual regression** — screenshot-diff a javitas elott/utan (pixelmatch),
  layout-eltolodas (CLS) meres.
- **Akadalymentessegi audit** — teljes a11y-fa ellenorzes (axe-core) beepitese a
  detektorok koze.
- **Cross-browser probe** — Firefox/WebKit is a Playwrighttal.
- **Teljesitmeny** — a Cloudflare `web-perf` mintaju Core Web Vitals meres a
  probe-ba.
- **„Record" mod** — a felhasznalo egyszer vegigkattintja a hibas flow-t, abbol
  determinisztikus reprodukcios szkript generalodik.

---

## Kapcsolodo dokumentumok

- `03_POLYGLOT_ANALYZER.md` — a HTML/CSS/JS parse-reteg, amire a static graph epul.
- `08_KNOWLEDGE_GRAPH_AND_SUBGRAPH_EXTRACTION.md` — a k-hop / context-cache minta,
  amit a `build_ui_context_cache` UI-ra altalanosit.
- `10_EPHEMERAL_CONTAINER_SANDBOX_SPEC.md` — a sandbox, amiben a Playwright-
  validacio fut (image override).
- `20_STARTUP_GENERATOR.md` — a statikus-oldal generator, ami ugyanezt a
  Playwright-reteget hasznalja smoke-tesztre.
```
