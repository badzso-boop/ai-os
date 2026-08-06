# 20. Startup Generator — `ai-os startup`

> **Státusz: tervdokumentum (design doc).** Ez a parancs MÉG NINCS implementálva.
> A dokumentum a `ai-os startup` teljes tervét írja le: bemenet, pipeline,
> determinisztikus váz + LLM-kitöltés, validáció, és a live deployra kötés (a
> deploy részleteit a `21_STATIC_SUBDOMAIN_DEPLOY.md` írja le). Kód angol, próza
> magyar.

---

## 1. Mit csinál egy mondatban

`ai-os startup` egy **részletes szöveges startup-leírásból** egy **tiszta,
önmagában futó statikus HTML/CSS/JS demót** generál, ami bemutatja a startupot és
**szimulálja a működését** — valódi backend nélkül —, validálja, majd (opcionálisan)
azonnal ki is deployolja egy saját subdomainre.

Nem termék, hanem **koncepció-demó**: gyorsan elkészíthető, a startupra tökéletesen
illeszkedő landing + interaktív mintaműködés (fake adat, fake auth, fake API), amit
be lehet mutatni befektetőnek/ügyfélnek vagy validálni lehet vele az ötletet.

---

## 2. A kimenet pontosan

- **Önmagában futó (self-contained) statikus site.** Se build-lépés futásidőben,
  se külső hálózati hívás — minden asset a bundle-ben (a `claude.ai` Artifact-ok
  szigorú CSP-filozófiájával rokon: inline/embedded assetek, nincs külső CDN).
  Ez teszi a deployt triviálissá (csak fájlokat kell kiszolgálni) és biztonságossá.
- **Szimulált backend (`sim` réteg).** Egy determinisztikus JS-könyvtár
  (`sim/sim.js`) biztosítja: mock adatmodell, kamu-latency, `localStorage`
  perzisztencia (a demó „emlékszik" reload után), szimulált auth (bejelentkezés
  bármilyen adattal), fake API-válaszok, seedelt minta-adat. Az LLM csak
  *bekötögeti* a flow-kat ehhez — a réteget mi adjuk (Compiler First).
- **Több oldal / IA.** A leírásból levezetett információs architektúra: landing,
  „hogyan működik", ár, egy interaktív „termék-demó" oldal (a lényeg), esetleg
  dashboard-szimuláció.
- **Márka-illeszkedés.** Színpaletta, tipográfia, hangnem, ikonográfia a leírás
  alapján — de a `dataviz`/artifact-design elveivel (light+dark, kontraszt,
  konzisztens tokenek).
- **Alap SEO + a11y + reszponzivitás** beépítve a determinisztikus vázba.

---

## 3. Bemenet — a startup-prompt

A parancs egy **részletes** leírást vár. Minél konkrétabb, annál pontosabb a demó.
Kétféleképp adható:

- **Inline:** `ai-os startup --prompt "..."`.
- **Brief-fájl:** `ai-os startup --brief startup.md` — egy strukturált leírás,
  amit érdemes verziózni. Ajánlott váz (`.ai-os/startup.md` sablon):

```markdown
# Startup brief

## Név + egymondatos value prop
FreshBox — heti dobozos, helyi termelői zöldség-előfizetés budapesti háztartásoknak.

## Célközönség
Egészségtudatos, elfoglalt 28–45 évesek, akik támogatnák a helyi termelőket.

## A demó fő flow-ja (EZT szimuláljuk működőként)
1. Kiválaszt egy doboz-méretet és gyakoriságot.
2. Megnézi a heti dobozt (mock termék-lista), cserél 1-2 tételt.
3. „Előfizet" (fake checkout, fake fizetés), lát egy megerősítést + egy
   dashboard-ot a következő szállítással.

## Oldalak
Landing, Hogyan működik, Árazás, Termék-demó (interaktív), Dashboard (szimulált).

## Márka / hangnem
Friss, zöld, barátságos, minimál. Kézzel rajzolt zöldség-illusztrációk hangulat.
Elsődleges szín: természetes zöld. Világos + sötét mód.

## Amit NEM kell
Valódi fizetés, valódi user-fiók, admin, e-mail — minden szimulált.
```

A briefből egy strukturált **design brief** készül (4.1), ami a pipeline gerince.

---

## 4. A pipeline

```
[startup-prompt / brief]
     │
     ▼
(1) Design Brief expanzió  ── erős modell ──►  strukturált brief (IA, flow-k, márka-tokenek, komponensek, tartalom-vázlat)
     │
     ▼
(2) Determinisztikus váz  ── scaffold.py "startup" preset ──►  reset/tokenek/layout/sim.js/oldal-vázak  (0 token)
     │
     ▼
(3) Oldalankénti build     ── közepes modell, PÁRHUZAMOS ──►  minden oldal HTML+CSS+interakció, a vázba illesztve
     │
     ▼
(4) Copy / tartalom        ── olcsó modell ──►  reális szövegek, mock-adat seed
     │
     ▼
(5) Sim-réteg bekötés      ── közepes modell ──►  a fő flow rákötése a sim.js-re (fake auth/checkout/adat)
     │
     ▼
(6) Összeszerelés          ── determinisztikus ──►  self-contained bundle (asset-inline, CSP-tiszta)  (0 token)
     │
     ▼
(7) Validáció              ── determinisztikus (Playwright sandbox) ──►  betölt? fő flow kattintható? nincs konzol-hiba? a11y/SEO baseline?
     │
     ▼
(8) Deploy (opcionális)    ── SCRIPT (nem AI) ──►  élő subdomain  (lásd 21. doc)
```

### 4.1. Design Brief expanzió (erős modell)

A nyers promptból egy **gépi séma** készül: `pages[]` (mindegyikhez cél + fő
komponensek), `core_flow[]` (a szimulálandó lépések), `brand` (színtokenek,
tipográfia, hangnem), `sim_model` (milyen mock-entitások kellenek: pl. Box,
Product, Subscription, User). Ez a decompose-hoz hasonló architekturális lépés →
a legerősebb konfigurált modellre routol (mint az `epic_planner`
`planning_assignment`-je).

### 4.2. Determinisztikus váz — `scaffold.py` „startup" preset

Az AI-OS 1. alapelve: a boilerplate-re **nem égetünk tokent**. A meglévő
`ai_os/core/scaffold.py` preset-mechanizmusát bővítjük egy `startup` (vagy
`static-landing`) presettel, ami kész, működő vázat ad:

```
index.html                # semantic shell, <head> meta/OG/SEO, theme-toggle
styles/reset.css          # modern reset
styles/tokens.css         # CSS custom properties: színek, tipó, spacing (light+dark)
styles/layout.css         # container/grid/flex primitívek, reszponzív breakpointok
sim/sim.js                # a szimulált backend könyvtár (mock store, fake API, auth, latency)
sim/seed.js               # a demó seed-adat (a design brief sim_model-jéből)
pages/*.html              # oldalankénti váz (fejléc/lábléc include-dal)
app.js                    # oldal-routing (statikus, hash/He History), theme, sim-init
.ai-os/ui.json            # a Playwright-validációhoz (dev/preview parancs + fő flow lépések)
```

Az LLM így **csak a tartalmat, a márkát és a flow-bekötést** tölti ki — a
váz-döntéseket (reszponzivitás, tokenek, sim-API) determinisztikusan kapja. Ez
gyorsabb, olcsóbb és konzisztensebb, mint minden alkalommal a nulláról.

### 4.3. A `sim` réteg (a „szimulált működés" magja)

`sim.js` egy kis, függőség nélküli JS-könyvtár, amit mi karbantartunk:

- **`sim.store`** — `localStorage`-alapú perzisztens mock adatbázis (entitások a
  `seed.js`-ből).
- **`sim.api(path, body)`** — fake HTTP: konfigurálható késleltetéssel, reális
  válaszokkal ad vissza a store-ból (a valódi `fetch` érzetét kelti, de nincs
  hálózat).
- **`sim.auth`** — bármilyen e-maillel „bejelentkezik", session a `localStorage`-
  ban; kijelentkezés; védett-oldal átirányítás — mind kamu, de meggyőző.
- **`sim.pay`** — fake checkout: egy hihető fizetési UI, ami mindig „sikerül"
  (vagy szkriptelten hibázik demóhoz).
- Az LLM a fő flow-t **ezekre hívja rá**, nem talál ki saját mock-mechanizmust —
  így determinisztikus, tesztelhető, és a Playwright-smoke stabilan validálja.

### 4.4. Validáció (determinisztikus, Playwright)

Újrahasznosítja a `19_UI_DEBUG_TOOLCHAIN.md` Playwright-rétegét, de itt
**smoke-tesztként**: az ephemeral sandboxban (Playwright-image) betölti az oldalt
és ellenőrzi: minden oldal 200-nal renderel, nincs konzol-hiba, a `core_flow`
lépései végigkattinthatók (a `.ai-os/ui.json`-ban deklarált lépések), a
theme-toggle működik, alap a11y (minden interaktív elemnek van elérhető neve),
alap SEO (title/meta/OG jelen). A Phase 6 **test-presence** itt is elvárja a
`core_flow` Playwright-tesztjét a bundle mellé.

---

## 5. Determinisztikus vs LLM felelősség

| Feladat | Ki |
| ------- | -- |
| Váz, tokenek, layout-primitívek, sim.js | **determinisztikus** (scaffold preset) |
| Brief → strukturált design brief | **erős modell** |
| Oldalak tartalma + komponensei | **közepes modell** (párhuzamos) |
| Copy / mock-seed szövegek | **olcsó modell** |
| Fő flow bekötése a sim-re | **közepes modell** |
| Bundle összeszerelés, asset-inline | **determinisztikus** |
| Smoke-validáció | **determinisztikus** (Playwright) |
| Deploy | **script** (nem AI — lásd 21.) |
| „Jó ez így?" | **ember** (HITL preview a deploy előtt) |

---

## 6. Modell-routing és költség

A generálás **nem egy nagy monolit prompt**, hanem a fenti lépések, amelyek nagy
része **párhuzamosítható** (az oldalak függetlenek). Ez ráilleszthető a meglévő
`EpicRunner` batch-mechanizmusára (generation = design brief → oldalak
párhuzamosan → sim-bekötés → összeszerelés), a `DynamicScheduler` risk→model
routingjával: a brief CRITICAL, az oldalak MEDIUM, a copy LOW. Így a drága modellt
csak az architekturális döntésre használjuk, a tömegmunkát olcsóra visszük — és a
`AI_OS_EPIC_BUDGET_USD` cap itt is véd a túlköltekezéstől.

Alternatíva (egyszerűbb induláshoz): egy dedikált, könnyű pipeline az `EpicRunner`
teljes DAG-apparátusa nélkül, mert a greenfield generálásnál nincs lock/rebase/
merge-konfliktus (üres worktree, nincs mit ütköztetni). Ez a `scaffold` + néhány
párhuzamos completion-hívás. Az implementáció eldöntheti; a doksi mindkettőt
megengedi.

---

## 7. CLI

```
ai-os startup --prompt "<részletes leírás>"          # generál + validál
ai-os startup --brief startup.md                     # brief-fájlból
ai-os startup --prompt "..." --subdomain freshbox    # generál + validál + DEPLOY
ai-os startup --prompt "..." --no-deploy             # csak lokálisan, deploy nélkül
ai-os startup --prompt "..." --out ./freshbox-demo   # hova generálja
ai-os startup --prompt "..." --variants 2            # több design-variáns (A/B)
```

Flow: brief → generál → **HITL preview kapu** (mutatja a lokális előnézet URL-t /
screenshotot, jóváhagyásra vár, mint az epic plan-review) → validál → (ha
`--subdomain`) meghívja a **deploy scriptet** (21. doc) a subdomain-névvel →
kiírja az élő URL-t. Az AI maga **soha** nem futtat docker/nginx/cloudflared
parancsot — azt a script intézi (kontextus + biztonság, lásd 21.).

---

## 8. Generált projekt-elrendezés

```
freshbox-demo/
  index.html
  styles/{reset,tokens,layout}.css
  sim/{sim,seed}.js
  pages/{how-it-works,pricing,demo,dashboard}.html
  app.js
  assets/…                # inline-olva a végső bundle-be, vagy kis statikus fájlok
  tests/flow.spec.ts      # a core_flow Playwright smoke-tesztje (Phase 6)
  .ai-os/ui.json          # preview + flow-lépések a validációhoz
  README.md               # mi ez, hogyan futtatható lokálisan, hogy deployolható
```

A bundle **statikus** — lokálisan egy `python -m http.server`-rel is fut, deployra
pedig épp emiatt elég egy nginx-konténerbe bemountolni (21. doc).

---

## 9. Deploy-kötés (a híd a 21. dokumentumhoz)

Ha `--subdomain <név>` meg van adva, a sikeres validáció után a parancs meghívja
a **`scripts/deploy_static.sh`** scriptet a `--dir <generált bundle>` és
`--subdomain <név>` paraméterekkel. A script (nem az AI) végez minden
infrastruktúra-műveletet: izolált nginx Docker-konténer, additív host-nginx site,
Cloudflare Tunnel wildcard-on át élő URL. **A megosztott live szerver védelme
miatt ez determinisztikus, additív-only és auditálható script** — a részleteket és
a szigorú biztonsági szabályokat a `21_STATIC_SUBDOMAIN_DEPLOY.md` írja le.

---

## 10. Korlátok (őszintén)

- **Nincs valódi backend.** Ez szándék: koncepció-demó, nem MVP. A `sim` réteg
  meggyőző, de nem perzisztál szerveroldalon, nincs többfelhasználós valós adat.
  (Egy jövőbeli „export to real project" híd az `ai-os init`-re válthatná a demót
  igazi FastAPI/Next backendre.)
- **Design-minőség modellfüggő.** A determinisztikus váz + tokenek garantálják a
  konzisztens alapot, de a „wow" a modelltől függ — a `--variants` több
  változatot ad, amiből az ember választ.
- **Tartalom-valósság.** A copy szimulált; jogi/orvosi/pénzügyi állításoknál
  vigyázni kell (a demó ne keltsen valós termék látszatát félrevezetően — lásd a
  publikálási elveket).

---

## 11. Biztonság és etika

- A generált oldal **statikus, self-contained, titok nélküli** — nincs mit
  kiszivárogtatni, nincs külső hívás.
- **Ne imitáljon valós céget/személyt.** A generátor a felhasználó saját
  startup-ötletét demózza; tiltott valós márka/logó/domain meghamisítása vagy
  megtévesztő „valódi terméknek látszó" tartalom (ugyanaz az elv, mint az
  Artifact-oknál). A HITL-preview kapu ad emberi kontrollt a deploy előtt.
- A deploy izolált és additív (21.), a live szerver más projektjeit nem érinti.

---

## 12. Jövőbeli kiterjesztések

- **Több design-variáns egy futásból** (A/B/C), választható előnézettel.
- **Export igazi projektté** — a demó `sim`-rétegét lecserélni valódi
  backendre az `ai-os init` presetjeivel (a demó lesz a spec).
- **Analytics-snippet** (privacy-barát, önhosztolt) a demó-subdomainre.
- **TTL / auto-lejárat** a demó-subdomainekre (a deploy-registryből, 21.).
- **Tartalom-import** (logó, brand-kit feltöltés) a márka-illeszkedéshez.

---

## Kapcsolódó dokumentumok

- `19_UI_DEBUG_TOOLCHAIN.md` — a Playwright-réteg, amit a smoke-validáció használ.
- `21_STATIC_SUBDOMAIN_DEPLOY.md` — az élő subdomain-deploy script és a megosztott
  szerver biztonsági szabályai.
- `README.md` → `ai-os init` — a scaffold preset-mechanizmus, amire a „startup"
  preset épül.
```
