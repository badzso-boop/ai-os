# 20. Startup Generator — `ai-os startup`

> **Status: Design Document.** This command is NOT YET implemented.
> A dokumentum a `ai-os startup` teljes tervet irja le: bemenet, pipeline,
> determinisztikus vaz + LLM-filling, validacio, es a live deployra kotes (a
> deploy reszleteit a `21_STATIC_SUBDOMAIN_DEPLOY.md` irja le). Kod angol, prose
> magyar.

---

## 1. What it does in one sentence

`ai-os startup` egy **detailed szoveges startup-descriptionbol** egy **tiszta,
onmagaban futo statikus HTML/CSS/JS demot** general, ami bemutatja a startupot es
**szimulalja a operation** — valodi backend nelkul —, validalja, majd (opcionalisan)
azonnal ki is deployolja egy sajat subdomainre.

Nem termek, hanem **koncepcio-demo**: gyorsan elkeszitheto, a startupra tokeletesen
illeszkedo landing + interaktiv mintamukodes (fake adat, fake auth, fake API), amit
be lehet mutatni befektetonek/ugyfelnek vagy validalni lehet vele az otletet.

---

## 2. Exact Output Structure

- **Onmagaban futo (self-contained) statikus site.** Se build-step futasidoben,
  se kulso halozati hivas — minden asset a bundle-ben (a `claude.ai` Artifact-ok
  szigoru CSP-filozofiajaval rokon: inline/embedded assetek, nincs kulso CDN).
  Ez teszi a deployt trivialissa (csak fajlokat kell kiszolgalni) es biztonsagossa.
- **Szimulalt backend (`sim` reteg).** Egy determinisztikus JS-konyvtar
  (`sim/sim.js`) biztositja: mock adatmodell, kamu-latency, `localStorage`
  perzisztencia (a demo „emlekszik" reload utan), szimulalt auth (bejelentkezes
  barmilyen adattal), fake API-valaszok, seedelt minta-adat. Az LLM csak
  *bekotogeti* a flow-kat ehhez — a reteget mi adjuk (Compiler First).
- **Tobb oldal / IA.** A descriptionbol levezetett informacios architektura: landing,
  „hogyan works", ar, egy interaktiv „termek-demo" oldal (a lenyeg), esetleg
  dashboard-szimulacio.
- **Brand-illeszkedes.** Szinpaletta, tipografia, tone, ikonografia a description
  based on — de a `dataviz`/artifact-design elveivel (light+dark, kontraszt,
  konzisztens tokenek).
- **Alap SEO + a11y + reszponzivitas** beepitve a determinisztikus vazba.

---

## 3. Input — The Startup Prompt

A parancs egy **detailed** descriptiont var. Minel konkretabb, annal pontosabb a demo.
Ketfelekepp adhato:

- **Inline:** `ai-os startup --prompt "..."`.
- **Brief-fajl:** `ai-os startup --brief startup.md` — egy strukturalt description,
  amit erdemes verziozni. Ajanlott vaz (`.ai-os/startup.md` sablon):

```markdown
# Startup brief

## Nev + egymondatos value prop
FreshBox — heti dobozos, helyi termeloi greenseg-elofizetes budapesti haztartasoknak.

## Target audience
Egeszsegtudatos, elfoglalt 28–45 evesek, akik tamogatnak a helyi termeloket.

## A demo fo flow-ja (EZT szimulaljuk mukodokent)
1. Kivalaszt egy doboz-meretet es gyakorisagot.
2. Megnezi a heti dobozt (mock termek-lista), cserel 1-2 tetelt.
3. „Elofizet" (fake checkout, fake fizetes), lat egy megerositest + egy
   dashboard-ot a kovetkezo szallitassal.

## Pages
Landing, Hogyan works, Arazas, Termek-demo (interaktiv), Dashboard (szimulalt).

## Brand / tone
Friss, green, baratsagos, minimal. Kezzel rajzolt greenseg-illusztraciok hangulat.
Elsodleges szin: termeszetes green. Light + sotet mod.

## Amit NEM kell
Valodi fizetes, valodi user-fiok, admin, e-mail — minden szimulalt.
```

A briefbol egy strukturalt **design brief** keszul (4.1), ami a pipeline gerince.

---

## 4. The Pipeline

```
[startup-prompt / brief]
     │
     ▼
(1) Design Brief expanzio  ── eros modell ──►  strukturalt brief (IA, flow-k, brand-tokenek, komponensek, tartalom-vazlat)
     │
     ▼
(2) Determinisztikus vaz  ── scaffold.py "startup" preset ──►  reset/tokenek/layout/sim.js/oldal-vazak  (0 token)
     │
     ▼
(3) Oldalankenti build     ── kozepes modell, PARHUZAMOS ──►  minden oldal HTML+CSS+interakcio, a vazba illesztve
     │
     ▼
(4) Copy / tartalom        ── olcso modell ──►  realis szovegek, mock-adat seed
     │
     ▼
(5) Sim-reteg bekotes      ── kozepes modell ──►  a fo flow rakotese a sim.js-re (fake auth/checkout/adat)
     │
     ▼
(6) Osszeszereles          ── determinisztikus ──►  self-contained bundle (asset-inline, CSP-tiszta)  (0 token)
     │
     ▼
(7) Validacio              ── determinisztikus (Playwright sandbox) ──►  betolt? fo flow clickable? nincs konzol-hiba? a11y/SEO baseline?
     │
     ▼
(8) Deploy (opcionalis)    ── SCRIPT (nem AI) ──►  elo subdomain  (lasd 21. doc)
```

### 4.1. Design Brief expanzio (eros modell)

A nyers promptbol egy **gepi sema** keszul: `pages[]` (mindegyikhez cel + fo
komponensek), `core_flow[]` (a szimulalando stepek), `brand` (szintokenek,
tipografia, tone), `sim_model` (milyen mock-entitasok kellenek: pl. Box,
Product, Subscription, User). Ez a decompose-hoz hasonlo architekturalis step →
a legerosebb konfiguralt modellre routol (mint az `epic_planner`
`planning_assignment`-je).

### 4.2. Determinisztikus vaz — `scaffold.py` „startup" preset

Az AI-OS 1. alapelve: a boilerplate-re **nem egetunk tokent**. A meglevo
`ai_os/core/scaffold.py` preset-mechanizmusat bovitjuk egy `startup` (vagy
`static-landing`) presettel, ami kesz, mukodo vazat ad:

```
index.html                # semantic shell, <head> meta/OG/SEO, theme-toggle
styles/reset.css          # modern reset
styles/tokens.css         # CSS custom properties: szinek, tipo, spacing (light+dark)
styles/layout.css         # container/grid/flex primitivek, reszponziv breakpointok
sim/sim.js                # a szimulalt backend konyvtar (mock store, fake API, auth, latency)
sim/seed.js               # a demo seed-adat (a design brief sim_model-jebol)
pages/*.html              # oldalankenti vaz (fejlec/lablec include-dal)
app.js                    # oldal-routing (statikus, hash/He History), theme, sim-init
.ai-os/ui.json            # a Playwright-validaciohoz (dev/preview parancs + fo flow stepek)
```

Az LLM igy **csak a tartalmat, a markat es a flow-bekotest** tolti ki — a
vaz-donteseket (reszponzivitas, tokenek, sim-API) determinisztikusan kapja. Ez
gyorsabb, olcsobb es konzisztensebb, mint minden alkalommal a nullarol.

### 4.3. A `sim` reteg (a „szimulalt mukodes" magja)

`sim.js` egy kis, fuggoseg nelkuli JS-konyvtar, amit mi karbantartunk:

- **`sim.store`** — `localStorage`-alapu perzisztens mock adatbazis (entitasok a
  `seed.js`-bol).
- **`sim.api(path, body)`** — fake HTTP: konfiguralhato kesleltetessel, realis
  valaszokkal ad vissza a store-bol (a valodi `fetch` erzetet kelti, de nincs
  halozat).
- **`sim.auth`** — barmilyen e-maillel „bejelentkezik", session a `localStorage`-
  ban; kijelentkezes; vedett-oldal atiranyitas — mind kamu, de meggyozo.
- **`sim.pay`** — fake checkout: egy hiheto fizetesi UI, ami mindig „sikerul"
  (vagy szkriptelten hibazik demohoz).
- Az LLM a fo flow-t **ezekre hivja ra**, nem talal ki sajat mock-mechanizmust —
  igy determinisztikus, tesztelheto, es a Playwright-smoke stabilan validalja.

### 4.4. Validacio (determinisztikus, Playwright)

Ujrahasznositja a `19_UI_DEBUG_TOOLCHAIN.md` Playwright-reteget, de itt
**smoke-tesztkent**: az ephemeral sandboxban (Playwright-image) betolti az oldalt
es ellenorzi: minden oldal 200-nal renderel, nincs konzol-hiba, a `core_flow`
stepei vegigclickablek (a `.ai-os/ui.json`-ban deklaralt stepek), a
theme-toggle works, alap a11y (minden interaktiv elemnek van elerheto neve),
alap SEO (title/meta/OG jelen). A Phase 6 **test-presence** itt is elvarja a
`core_flow` Playwright-tesztjet a bundle melle.

---

## 5. Determinisztikus vs LLM responsible forseg

| Feladat | Ki |
| ------- | -- |
| Vaz, tokenek, layout-primitivek, sim.js | **determinisztikus** (scaffold preset) |
| Brief → strukturalt design brief | **eros modell** |
| Pages tartalma + komponensei | **kozepes modell** (parhuzamos) |
| Copy / mock-seed szovegek | **olcso modell** |
| Fo flow bekotese a sim-re | **kozepes modell** |
| Bundle osszeszereles, asset-inline | **determinisztikus** |
| Smoke-validacio | **determinisztikus** (Playwright) |
| Deploy | **script** (nem AI — lasd 21.) |
| „Jo ez igy?" | **ember** (HITL preview a deploy elott) |

---

## 6. Model Routing and Cost

A generalas **nem egy nagy monolit prompt**, hanem a fenti stepek, amelyek nagy
resze **parhuzamosithato** (az pages fuggetlenek). Ez raillesztheto a meglevo
`EpicRunner` batch-mechanizmusara (generation = design brief → pages
parhuzamosan → sim-bekotes → osszeszereles), a `DynamicScheduler` risk→model
routingjaval: a brief CRITICAL, az pages MEDIUM, a copy LOW. Igy a draga modellt
csak az architekturalis dontesre hasznaljuk, a tomegmunkat olcsora visszuk — es a
`AI_OS_EPIC_BUDGET_USD` cap itt is ved a tulkoltekezestol.

Alternativa (egyszerubb indulashoz): egy dedikalt, konnyu pipeline az `EpicRunner`
teljes DAG-apparatusa nelkul, mert a greenfield generalasnal nincs lock/rebase/
merge-konfliktus (ures worktree, nincs mit utkoztetni). Ez a `scaffold` + nehany
parhuzamos completion-hivas. Az implementacio eldontheti; a doksi mindkettot
megengedi.

---

## 7. CLI Surface

```
ai-os startup --prompt "<detailed description>"          # general + validal
ai-os startup --brief startup.md                     # brief-fajlbol
ai-os startup --prompt "..." --subdomain freshbox    # general + validal + DEPLOY
ai-os startup --prompt "..." --no-deploy             # csak lokalisan, deploy nelkul
ai-os startup --prompt "..." --out ./freshbox-demo   # hova generalja
ai-os startup --prompt "..." --variants 2            # tobb design-varians (A/B)
```

Flow: brief → general → **HITL preview kapu** (mutatja a lokalis elonezet URL-t /
screenshotot, jovahagyasra var, mint az epic plan-review) → validal → (ha
`--subdomain`) meghivja a **deploy scriptet** (21. doc) a subdomain-nevvel →
kiirja az elo URL-t. Az AI maga **soha** nem futtat docker/nginx/cloudflared
parancsot — azt a script intezi (kontextus + biztonsag, lasd 21.).

---

## 8. Generated Project Layout

```
freshbox-demo/
  index.html
  styles/{reset,tokens,layout}.css
  sim/{sim,seed}.js
  pages/{how-it-works,pricing,demo,dashboard}.html
  app.js
  assets/…                # inline-olva a vegso bundle-be, vagy kis statikus fajlok
  tests/flow.spec.ts      # a core_flow Playwright smoke-tesztje (Phase 6)
  .ai-os/ui.json          # preview + flow-stepek a validaciohoz
  README.md               # mi ez, hogyan futtathato lokalisan, hogy deployolhato
```

A bundle **statikus** — lokalisan egy `python -m http.server`-rel is fut, deployra
pedig epp emiatt eleg egy nginx-kontenerbe bemountolni (21. doc).

---

## 9. Deploy Binding (Bridge to Document 21)

Ha `--subdomain <nev>` meg van adva, a sikeres validacio utan a parancs meghivja
a **`scripts/deploy_static.sh`** scriptet a `--dir <generalt bundle>` es
`--subdomain <nev>` parameterekkel. A script (nem az AI) vegez minden
infrastruktura-muveletet: izolalt nginx Docker-kontener, additiv host-nginx site,
Cloudflare Tunnel wildcard-on at elo URL. **A megosztott live szerver vedelme
miatt ez determinisztikus, additiv-only es auditalhato script** — a reszleteket es
a szigoru biztonsagi szabalyokat a `21_STATIC_SUBDOMAIN_DEPLOY.md` irja le.

---

## 10. Limitations (Honest Assessment)

- **Nincs valodi backend.** Ez szandek: koncepcio-demo, nem MVP. A `sim` reteg
  meggyozo, de nem perzisztal szerveroldalon, nincs tobbusers valos adat.
  (Egy jovobeli „export to real project" hid az `ai-os init`-re valthatna a demot
  igazi FastAPI/Next backendre.)
- **Design-minoseg modellfuggo.** A determinisztikus vaz + tokenek garantaljak a
  konzisztens alapot, de a „wow" a modelltol fugg — a `--variants` tobb
  valtozatot ad, amibol az ember valaszt.
- **Tartalom-valossag.** A copy szimulalt; jogi/orvosi/penzugyi allitasoknal
  vigyazni kell (a demo ne keltsen valos termek latszatat felrevezetoen — lasd a
  publikalasi elveket).

---

## 11. Security and Ethics

- A generalt oldal **statikus, self-contained, titok nelkuli** — nincs mit
  kiszivarogtatni, nincs kulso hivas.
- **Ne imitaljon valos ceget/szemelyt.** A generator a user sajat
  startup-otletet demozza; tiltott valos brand/logo/domain meghamisitasa vagy
  megteveszto „valodi termeknek latszo" tartalom (ugyanaz az elv, mint az
  Artifact-oknal). A HITL-preview kapu ad emberi kontrollt a deploy elott.
- A deploy izolalt es additiv (21.), a live szerver mas projektjeit nem erinti.

---

## 12. Future Extensions

- **Tobb design-varians egy futasbol** (A/B/C), valaszthato elonezettel.
- **Export igazi projektte** — a demo `sim`-reteget lecserelni valodi
  backendre az `ai-os init` presetjeivel (a demo lesz a spec).
- **Analytics-snippet** (privacy-barat, onhosztolt) a demo-subdomainre.
- **TTL / auto-lejarat** a demo-subdomainekre (a deploy-registrybol, 21.).
- **Tartalom-import** (logo, brand-kit feltoltes) a brand-illeszkedeshez.

---

## Related Documents

- `19_UI_DEBUG_TOOLCHAIN.md` — a Playwright-reteg, amit a smoke-validacio hasznal.
- `21_STATIC_SUBDOMAIN_DEPLOY.md` — az elo subdomain-deploy script es a megosztott
  szerver biztonsagi szabalyai.
- `README.md` → `ai-os init` — a scaffold preset-mechanizmus, amire a „startup"
  preset epul.
```
