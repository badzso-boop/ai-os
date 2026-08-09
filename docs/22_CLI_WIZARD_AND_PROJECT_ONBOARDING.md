# 22. CLI Wizard & Interactive Project Onboarding — `ai-os wizard` & AI-driven `ai-os project add`

> **Státusz: tervdokumentum (design doc).** Ez a parancs és bővítés MÉG NINCS implementálva.
> A dokumentum az `ai-os wizard` post-install varázsló és a továbbfejlesztött, AI-alapú `ai-os project add` automatikus projekt-konfiguráló működését írja le. Kód angol, próza magyar.

---

## 1. Mit csinál egy mondatban

`ai-os wizard` egy **interaktív telepítés utáni varázsló**, amely végigkíséri a felhasználót a függőségek (Docker, Git, gh CLI) ellenőrzésén, az LLM providerek/hitelesítések beállításán és a sandbox tesztelésén. A továbbfejlesztött **`ai-os project add`** pedig automatikusan beolvassa a projekt dokumentációit (`CLAUDE.md`, `README.md`, `docs/`, `package.json` stb.), és egy **LOW/MEDIUM AI modell segítségével automatikusan előállítja** a projekt `.ai-os/` konfigurációs fájljait (`instructions.json`, `conventions.md`, `sandbox.json`, `ui.json`) — dokumentáció hiányában pedig felajánl egy **MEDIUM/HIGH AI mélyelemzést** a kódstruktúra alapján.

---

## 2. Részletes architektúra és munkafolyamat

### 2.1. `ai-os wizard` — Interaktív telepítési varázsló

A parancs célja, hogy az első `pip install` után a felhasználó **egyetlen paranccsal élesíteni tudja az AI-OS rendszert**, kézi `.env` másolgatás nélkül.

```
ai-os wizard
```

#### A varázsló lépései:
1. **Környezeti ellenőrzés (Environment Check):**
   - Python verzió (3.13+ elvárt)
   - Docker démon állapota (`docker info`) — ha nem fut, figyelmeztet a sandbox függőségre
   - Git & GitHub CLI (`gh auth status`) jelenléte
2. **Provider hitelesítés & Login varázsló:**
   - Detektálja a meglévő munkafüzet-munkameneteket:
     - Google Antigravity CLI (`agy`) fiók
     - Anthropic CLI (`claude`) fiók
   - Ellenőrzi a környezeti változókat (`GEMINI_API_KEY`, `OPENROUTER_API_KEY`, `ANTHROPIC_API_KEY`)
   - Felajánlja az hiányzó kulcsok bekérését vagy a munkamenet-alapú fiókbejelentkezést
   - Futat egy teszt pinget (`ai-os llm list`)
3. **Sandbox Docker image ellenőrzés:**
   - Ellenőrzi az `ai-os-sandbox-python:3.12` Docker kép meglétét
   - Ha hiányzik, automatikusan leállítja és felajánlja a `docker build -t ai-os-sandbox-python:3.12 -f docker/python-sandbox.Dockerfile .` lefuttatását
4. **Kockázat-routing és budget beállítás:**
   - Interaktív prompt a kockázati szintek (LOW, MEDIUM, HIGH, CRITICAL) providereinek hozzárendelésére
   - Opcionális `AI_OS_EPIC_BUDGET_USD` beállítása túlköltekezés ellen

---

### 2.2. Továbbfejlesztött `ai-os project add` — AI-alapú konfiguráció-generálás

Amikor a felhasználó hozzáad egy projektet az AI-OS regiszterhez:

```bash
ai-os project add <name> <path> [--deep]
```

#### A folyamat lépései:

```
[ai-os project add <name> <path>]
         │
         ▼
(1) Statikus dokumentáció keresés ── (CLAUDE.md, README.md, docs/, build manifestek)
         │
         ├──► VANNAK dokumentációs fájlok?
         │         │
         │         ├─► [IGEN] ──► (2) Gyors AI szintézis (LOW / MEDIUM modell)
         │         │                 Generálja: instructions.json, conventions.md, sandbox.json
         │         │
         │         └─► [NEM]  ──► (3) Figyelmeztetés + Interaktív felajánlás:
         │                           "[warning] No documentation files found in <path>.
         │                           Would you like a MEDIUM/HIGH AI model to perform
         │                           a deep codebase inspection and auto-generate configs? [Y/n]"
         │                                 │
         │                                 └─► [IGEN] ──► (4) Mélyelemzés (MEDIUM / HIGH modell)
         │                                                   Kódstruktúra, AST, manifestek beolvasása
         │                                                   Generálja a teljes .ai-os/ konfig csomagot
```

---

## 3. Generált konfigurációs fájlok formátuma

### 3.1. `.ai-os/instructions.json`
Tartalmazza a projekt gépi specifikációját, parancsait és dokumentációs hivatkozásait:

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
A projekt specifikus konvenciói, i18n szabályok, UI reszponzivitási szabályok (pl. 375px mobil nézet) és tesztelési elvárások.

### 3.3. `.ai-os/sandbox.json`
A Docker sandbox futtató parancsai és környezeti változói:

```json
{
  "setup_commands": ["pip install -r requirements.txt"],
  "test_command": "pytest -q"
}
```

---

## 4. Implementációs modulok

- **`ai_os/core/wizard.py`**: Interaktív környezetellenőrzés, login tesztelés, `.env` kezelés.
- **`ai_os/core/onboarding.py`**: Dokumentáció-pásztázó, LLM prompt-sablonok a konfig-generáláshoz, mélyelemző fallback.
- **`ai_os/cli.py`**: `@main.command("wizard")` és a kibővített `@project.command("add")`.
- **`tests/test_wizard.py`** & **`tests/test_onboarding.py`**: Unit és CLI tesztek `CliRunner` és `tmp_path` segítségével.

---

## Kapcsolódó dokumentumok

- `docs/01_ARCHITECTURE_OVERVIEW.md` — a rendszer általános architektúrája.
- `docs/20_STARTUP_GENERATOR.md` — a statikus demó generátor specifikációja.
- `docs/21_STATIC_SUBDOMAIN_DEPLOY.md` — az élő subdomain deploy script.
