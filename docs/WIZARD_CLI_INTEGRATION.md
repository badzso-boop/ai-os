# 22. CLI Wizard & Interaktív Projekt Onboarding Integration — `ai-os wizard` & `ai-os project add`

> **Státusz: Implementálva és visszaállítva (Merged & Active).** Ez a dokumentum az `ai-os wizard` telepítési varázsló, az interaktív `ai-os project add` onboarding flow és a Rich event printer `cli.py`-ba való tiszta reintegrációját és működését írja le. Kód: angol, leírás/próza: magyar.

---

## 1. Előzmények: Mi történt az Issue #10 / PR #30 során? (Hibaelemzés)

### 1.1. A probléma háttere (A regressziós hiba oka)
Az **Issue #10 / PR #30** feladata a post-install varázsló (`ai-os wizard`), az interaktív projekt regisztrációs és konfiguráció-generáló flow (`ai-os project add`), valamint a Rich-alapú esemény-nyomtató (Rich event printer) bevezetése volt. 

A korábbi felülbírálás során egy teljes fájlcsere (full file overwrite) történt a `cli.py` fájlon. Ennek következtében a meglévő core CLI parancsok (mint pl. az `ai-os scan`, `ai-os watch`, `ai-os task run`, `ai-os epic run`, `ai-os init`, `ai-os clean`, `ai-os cost`, `ai-os startup`) törlődtek vagy csonkolódtak (stubbed implementation), ami törte a CLI regressziós tesztjeit és a core funkciókat. Emiatt a PR felülvizsgálatkor visszavonásra (revert) került.

### 1.2. A helyreállítás és integráció megoldása (Additív bekötés)
A mostani `fix/issue-10` ágon a hibát additív (additív kódintegrációs) megközelítéssel javítottuk ki:
1. **Meglévő CLI parancsok megőrzése:** A `cli.py` összes korábbi Click parancsa (`main`, `clean`, `init`, `project`, `scan`, `watch`, `llm`, `task`, `epic`, `cost`, `startup`) 100%-ban épségben maradt.
2. **`ai-os wizard` hozzáadása:** Tiszta `@main.command("wizard")` belépési pont beillesztése, amely meghívja az `ai_os.core.wizard.run_wizard()` funkciót.
3. **`ai-os project add` interaktív bővítése:** A meglévő `project_add` parancs kiegészítése a `--deep-scan` opcióval és az interaktív promptolási logikával (`click.confirm`), majd a konfigurációk automatikus előállításával a `scan_and_generate_configs(...)` segítségével.
4. **Rich event printer integráció:** Az `_make_event_printer` és `printer` beillesztése és használata az `epic run` és `epic resume` parancsok alatt a transzparens, élő státuszkijelzéshez.

---

## 2. Architektúra és Működési Útmutató

### 2.1. `ai-os wizard` — Post-install telepítési és diagnosztikai varázsló

A parancs célja, hogy a csomag telepítése vagy frissítése után a felhasználó **egyetlen paranccsal ellenőrizhesse és beállíthassa az AI-OS környezetet**.

```bash
ai-os wizard
```

#### Mit csinál a varázsló?
1. **Environment Check (Környezet ellenőrzése):**
   - Ellenőrzi a Python verziót (3.13+ elvárt).
   - Ellenőrzi a Docker démon állapotát (`docker info`). Ha a Docker nem fut, figyelmezteti a felhasználót, hogy a sandbox tesztfuttatás Docker-függő.
   - Ellenőrzi a Git és a GitHub CLI (`gh auth status`) elérhetőségét.
2. **Provider & Authentication Check (Hitelesítés ellenőrzése):**
   - Detektálja az aktív CLI munkameneteket (`agy` - Google Antigravity, `claude` - Anthropic).
   - Ellenőrzi a környezeti változókat (`GEMINI_API_KEY`, `OPENROUTER_API_KEY`, `ANTHROPIC_API_KEY`).
   - Lehetőséget biztosít az hiányzó kulcsok megadására vagy munkamenet-alapú bejelentkezésre.
   - Futtat egy teszt lekérdezést (`ai-os llm list`).
3. **Sandbox Docker Image Check:**
   - Ellenőrzi az `ai-os-sandbox-python:3.12` Docker kép meglétét.
   - Hiány esetén felajánlja az automatikus `docker build` lefuttatását.
4. **Kockázat-routing és Budget beállítások:**
   - Megjeleníti az alapértelmezett kockázati szinteket (LOW, MEDIUM, HIGH, CRITICAL) és felajánlja a költségkeret (`AI_OS_EPIC_BUDGET_USD`) konfigurálását.

---

## 2.2. Interaktív `ai-os project add` — Projekt regisztráció és AI-konfiguráció generálás

Amikor a felhasználó regisztrál egy új projektet a `~/.ai-os/projects.json` regiszterbe:

```bash
ai-os project add <name> <path> [--force] [--deep-scan]
```

#### Munkafolyamat:
1. **Regisztráció:** A projekt helye és neve bekerül a regiszterbe (`registry.add`).
2. **Interaktív / Zászló alapú Deep-Scan döntés:**
   - Ha a `--deep-scan` flag meg van adva, a rendszer mély elemzést végez.
   - Ha nincs megadva flag, a CLI interaktív kérdést tesz fel (`click.confirm("Perform deep scan of codebase?", default=False)`).
3. **Konfiguráció generálás (`scan_and_generate_configs`):**
   - **Dokumentáció szintézis:** Beolvassa a meglévő projekt-doksis struktúrákat (`CLAUDE.md`, `README.md`, `docs/`, `package.json`, `pyproject.toml` stb.).
   - **Generált `.ai-os/` struktúra:**
     - `.ai-os/instructions.json`: Projekt specifikáció, felépítés, alapértelmezett setup/test parancsok.
     - `.ai-os/conventions.md`: Kódolási és tesztelési konvenciók.
     - `.ai-os/sandbox.json`: Docker sandbox futtatási környezet és teszt parancsok.
     - `.ai-os/ui.json`: UI/UX konfigurációs elemek.

---

## 2.3. Glass-Box CLI Event Printer (Rich Stream)

Az `ai-os epic run` és `ai-os task run` során a terminál kimenetéért az `_make_event_printer(verbose)` callback felel:
- **Eseménytípusok:** `attempt`, `agent_turn`, `validation`, `merge_conflict`, `agent_error`, `retry`, `merged`, `test_quality`, `test_critique`.
- **Formázás:** Színes ikonos státuszjelzés (▶ futás, ✓ sikeres sandbox/merge, ✗ sikertelen sandbox, ⚠ figyelmeztetés, 🔐 biztonsági fájl érintettség).
- **Hiba részletezés:** Hiba esetén a sandbox kimenet utolsó 12 sora keretben (Panel) jelenik meg, vagy `-v` / `--verbose` kapcsoló esetén a teljes kimenet kiíródik.

---

## 3. Ellenőrzés és Tesztelés (Verification & Tests)

A módosítások helyességét átfogó automatizált unit és integrációs tesztek igazolják:

1. **Wizard tesztek (`tests/test_cli_wizard.py`):**
   - Teszteli az `ai-os wizard` parancs futását `CliRunner` segítségével mind sikeres, mind hiányzó Docker/tool állapotok mellett.
2. **Project Add tesztek (`tests/test_onboarding.py` & CLI unit tesztek):**
   - Teszteli a `project add` interaktív megerősítését, a `--deep-scan` kapcsolót és az `.ai-os/` konfig fájlok automatikus kigenerálását egy temp könyvtárban (`tmp_path`).
3. **Integrációs regression suite:**
   - A teljes pytest tesztcsomag (550+ teszt) hiba nélkül lefut (`pytest`).

---

## 4. Összegzés

A wizard és onboarding funkciók újbóli integrációja garantálja a korábbi funkcionalitás teljes megőrzését, miközben biztosítja az új interaktív beállítási és átlátható eseménynyomtatási képességeket az AI-OS felhasználói számára.
