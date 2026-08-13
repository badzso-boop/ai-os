# 22. CLI Wizard & Interactive Project Onboarding — `ai-os wizard` & AI-driven `ai-os project add`

> **Status: Design Document.** Ez a parancs es bovites NOT YET implemented.
> A dokumentum az `ai-os wizard` post-install varazslo es a tovabbfejlesztott, AI-alapu `ai-os project add` automatikus projekt-konfiguralo operation irja le. Code and prose are in English.

---

## 1. What it does in one sentence

`ai-os wizard` egy **interaktiv telepites utani varazslo**, amely vegigkiseri a usert a fuggosegek (Docker, Git, gh CLI) ellenorzesen, az LLM providerek/hitelesitesek beallitasan es a sandbox tesztelesen. A tovabbfejlesztott **`ai-os project add`** pedig automatikusan beolvassa a projekt dokumentacioit (`CLAUDE.md`, `README.md`, `docs/`, `package.json` stb.), es egy **LOW/MEDIUM AI modell segitsegevel automatikusan generates** a projekt `.ai-os/` konfiguracios fajljait (`instructions.json`, `conventions.md`, `sandbox.json`, `ui.json`) — dokumentacio hianyaban pedig felajanl egy **MEDIUM/HIGH AI melyelemzest** a kodstruktura based on.

---

## 2. Reszletes architektura es munkafolyamat

### 2.1. `ai-os wizard` — Interaktiv telepitesi varazslo

A parancs celja, hogy az elso `pip install` utan a user **egyetlen paranccsal elesiteni tudja az AI-OS rendszert**, kezi `.env` masolgatas nelkul.

```
ai-os wizard
```

#### A varazslo stepei:
1. **Kornyezeti ellenorzes (Environment Check):**
   - Python verzio (3.13+ elvart)
   - Docker demon allapota (`docker info`) — ha nem fut, figyelmeztet a sandbox fuggosegre
   - Git & GitHub CLI (`gh auth status`) jelenlete
2. **Provider hitelesites & Login varazslo:**
   - Detektalja a meglevo munkafuzet-munkameneteket:
     - Google Antigravity CLI (`agy`) fiok
     - Anthropic CLI (`claude`) fiok
   - Ellenorzi a kornyezeti valtozokat (`GEMINI_API_KEY`, `OPENROUTER_API_KEY`, `ANTHROPIC_API_KEY`)
   - Felajanlja az hianyzo kulcsok bekereset vagy a munkamenet-alapu fiokbejelentkezest
   - Futat egy teszt pinget (`ai-os llm list`)
3. **Sandbox Docker image ellenorzes:**
   - Ellenorzi az `ai-os-sandbox-python:3.12` Docker kep megletet
   - Ha hianyzik, automatikusan leallitja es felajanlja a `docker build -t ai-os-sandbox-python:3.12 -f docker/python-sandbox.Dockerfile .` lefuttatasat
4. **Kockazat-routing es budget beallitas:**
   - Interaktiv prompt a kockazati szintek (LOW, MEDIUM, HIGH, CRITICAL) providereinek hozzarendelesere
   - Opcionalis `AI_OS_EPIC_BUDGET_USD` beallitasa tulkoltekezes ellen

---

### 2.2. Tovabbfejlesztott `ai-os project add` — AI-alapu konfiguracio-generalas

Amikor a user hozzaad egy projektet az AI-OS regiszterhez:

```bash
ai-os project add <name> <path> [--deep]
```

#### A folyamat stepei:

```
[ai-os project add <name> <path>]
         │
         ▼
(1) Statikus dokumentacio kereses ── (CLAUDE.md, README.md, docs/, build manifestek)
         │
         ├──► VANNAK dokumentacios fajlok?
         │         │
         │         ├─► [IGEN] ──► (2) Gyors AI szintezis (LOW / MEDIUM modell)
         │         │                 Generalja: instructions.json, conventions.md, sandbox.json
         │         │
         │         └─► [NEM]  ──► (3) Figyelmeztetes + Interaktiv felajanlas:
         │                           "[warning] No documentation files found in <path>.
         │                           Would you like a MEDIUM/HIGH AI model to perform
         │                           a deep codebase inspection and auto-generate configs? [Y/n]"
         │                                 │
         │                                 └─► [IGEN] ──► (4) Melyelemzes (MEDIUM / HIGH modell)
         │                                                   Kodstruktura, AST, manifestek beolvasasa
         │                                                   Generalja a teljes .ai-os/ konfig csomagot
```

---

### 2.3. CLI Olvashatosag es Eletut-megjelenites (Enhanced Glass-Box CLI Stream)

Az `ai-os epic run` es `ai-os task run` folyaman a terminal kimenete **gazdag Rich formazassal** es jol olvashato step-statuszokkal jelenik meg:
- **Rich Stream Panel**: Szines statuszikonok (▶ futas, ✓ sikeres merge, ⚠ figyelmeztetes, ✗ sandbox hiba), futasi idozito (elapsed timer) es valos ideju token/USD koltsegkijelzes.
- **Attekintheto reszletek**: A sandbox kimeneti tail-jenek es a tesztkritikusi eszreveteleknek keretezett, atlathato megjelenitese.

---

### 2.4. Biztonsagos Git MCP Eszkozok (Safe Git MCP Tools)

Az `ai_os/mcp/mcp_server.py` szerver kibovul biztonsagi korlatokkal vedett Git MCP eszkozokkel:
- `git_status`: Visszaadja a jelenlegi git agat, a modositott es untracked fajlokat.
- `git_pull_main`: Biztonsagosan lehuzza a legfrissebb `main` agat (`git pull origin main`), garantalva, hogy uncommitted valtoztatasok in case of nem irja felul a fajlokat.
- `git_create_branch`: Uj feature agat hoz letre a megadott nevmintaval.
- `git_diff_summary`: Visszaadja a torzstol valo eltereseket a tesztekhez es kodulvizsgalathoz.

---

## 3. Generalt konfiguracios fajlok formatuma

### 3.1. `.ai-os/instructions.json`
Tartalmazza a projekt gepi specifikaciojat, parancsait es dokumentacios linkait:

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "project_name": "my-project",
  "architecture": {
    "monorepo": false,
    "package_manager": "pip",
    "stack": ["fastapi", "python", "pytest"]
  },
  "commands": {
    "setup": ["pip install -r requirements.txt"],
    "typecheck": "mypy .",
    "test_unit": "pytest -q"
  },
  "docs": ["README.md", "CLAUDE.md"],
  "conventions": ".ai-os/conventions.md",
  "sandbox": ".ai-os/sandbox.json",
  "ui_config": ".ai-os/ui.json"
}
```

### 3.2. `.ai-os/conventions.md`
A projekt specifikus konvencioi, i18n szabalyok, UI reszponzivitasi szabalyok (pl. 375px mobil nezet) es tesztelesi elvarasok.

### 3.3. `.ai-os/sandbox.json`
A Docker sandbox futtato parancsai es kornyezeti valtozoi:

```json
{
  "setup_commands": ["pip install -r requirements.txt"],
  "test_command": "pytest -q"
}
```

---

## 4. Implementacios modulok

- **`ai_os/core/wizard.py`**: Interaktiv kornyezetellenorzes, login teszteles, `.env` kezeles.
- **`ai_os/core/onboarding.py`**: Dokumentacio-pasztazo, LLM prompt-sablonok a konfig-generalashoz, melyelemzo fallback.
- **`ai_os/cli.py`**: `@main.command("wizard")` es a kibovitett `@project.command("add")`.
- **`tests/test_wizard.py`** & **`tests/test_onboarding.py`**: Unit es CLI tesztek `CliRunner` es `tmp_path` segitsegevel.

---

## Related Documents

- `docs/01_ARCHITECTURE_OVERVIEW.md` — a rendszer altalanos architekturaja.
- `docs/20_STARTUP_GENERATOR.md` — a statikus demo generator specification.
- `docs/21_STATIC_SUBDOMAIN_DEPLOY.md` — az elo subdomain deploy script.
