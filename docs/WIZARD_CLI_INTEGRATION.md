# 22. CLI Wizard & Interactive Project Onboarding Integration — `ai-os wizard` & `ai-os project add`

> **Status: Implemented and Merged (Merged & Active).** This document describes the clean re-integration and operation of the `ai-os wizard` installation wizard, the interactive `ai-os project add` onboarding flow, and the Rich event printer in `cli.py`.

---

## 1. Background: What happened during Issue #10 / PR #30? (Root Cause Analysis)

### 1.1. Problem Background (Cause of Regression Error)
The goal of **Issue #10 / PR #30** was to introduce the post-install wizard (`ai-os wizard`), the interactive project registration and configuration generator flow (`ai-os project add`), and the Rich event printer.

During a previous override attempt, a full file overwrite occurred on `cli.py`. As a result, existing core CLI commands (such as `ai-os scan`, `ai-os watch`, `ai-os task run`, `ai-os epic run`, `ai-os init`, `ai-os clean`, `ai-os cost`, `ai-os startup`) were deleted or stubbed, breaking CLI regression tests and core capabilities. Consequently, the PR was reverted during code review.

### 1.2. Recovery and Integration Solution (Additive Integration)
On the current `fix/issue-10` branch, the regression was resolved using an additive code integration approach:
1. **Preserving Existing CLI Commands:** All pre-existing Click commands in `cli.py` (`main`, `clean`, `init`, `project`, `scan`, `watch`, `llm`, `task`, `epic`, `cost`, `startup`) remain 100% intact.
2. **Adding `ai-os wizard`:** Clean `@main.command("wizard")` entry point inserted, invoking `ai_os.core.wizard.run_wizard()`.
3. **Interactive Expansion of `ai-os project add`:** Extended the existing `project_add` command with the `--deep-scan` option, interactive confirmation prompting (`click.confirm`), and automatic configuration generation via `scan_and_generate_configs(...)`.
4. **Rich Event Printer Integration:** Inserted and attached `_make_event_printer` and `printer` under `epic run` and `epic resume` commands for transparent, live status feedback.

---

## 2. Architecture and Operational Guide

### 2.1. `ai-os wizard` — Post-install Installation & Diagnostic Wizard

The goal of this command is to let the user **verify and configure the AI-OS environment with a single command** after package installation or upgrade.

```bash
ai-os wizard
```

#### What does the wizard do?
1. **Environment Check:**
   - Checks Python version (Python 3.13+ expected).
   - Checks Docker daemon status (`docker info`). If Docker is not running, warns the user that sandbox execution is Docker-dependent.
   - Checks availability of Git and GitHub CLI (`gh auth status`).
2. **Provider & Authentication Check:**
   - Detects active CLI sessions (`agy` - Google Antigravity, `claude` - Anthropic).
   - Checks environment variables (`GEMINI_API_KEY`, `OPENROUTER_API_KEY`, `ANTHROPIC_API_KEY`).
   - Offers to prompt for missing keys or log in via existing active sessions.
   - Runs a test ping (`ai-os llm list`).
3. **Sandbox Docker Image Check:**
   - Checks for the presence of the `ai-os-sandbox-python:3.12` Docker image.
   - If missing, offers an automatic `docker build`.
4. **Risk Routing & Budget Settings:**
   - Displays default risk routing levels (LOW, MEDIUM, HIGH, CRITICAL) and offers budget (`AI_OS_EPIC_BUDGET_USD`) configuration.

---

## 2.2. Interactive `ai-os project add` — Project Registration & AI Configuration Generation

When registering a new project into `~/.ai-os/projects.json`:

```bash
ai-os project add <name> <path> [--force] [--deep-scan]
```

#### Workflow:
1. **Registration:** Project location and name are added to the registry (`registry.add`).
2. **Interactive / Flag-based Deep-Scan Decision:**
   - If `--deep-scan` flag is provided, a deep analysis is performed.
   - If no flag is supplied, CLI interactively prompts (`click.confirm("Perform deep scan of codebase?", default=False)`).
3. **Configuration Generation (`scan_and_generate_configs`):**
   - **Documentation Synthesis:** Scans existing documentation structures (`CLAUDE.md`, `README.md`, `docs/`, `package.json`, `pyproject.toml`, etc.).
   - **Generated `.ai-os/` Structure:**
     - `.ai-os/instructions.json`: Project specification, structure, default setup/test commands.
     - `.ai-os/conventions.md`: Coding and testing conventions.
     - `.ai-os/sandbox.json`: Docker sandbox environment and test commands.
     - `.ai-os/ui.json`: UI/UX configuration elements.

---

## 2.3. Glass-Box CLI Event Printer (Rich Stream)

During `ai-os epic run` and `ai-os task run`, terminal output is handled by the `_make_event_printer(verbose)` callback:
- **Event Types:** `attempt`, `agent_turn`, `validation`, `merge_conflict`, `agent_error`, `retry`, `merged`, `test_quality`, `test_critique`.
- **Formatting:** Color-coded status icons (▶ running, ✓ sandbox/merge success, ✗ sandbox failure, ⚠ warning, 🔐 safety file affected).
- **Failure Details:** On failure, the last 12 lines of sandbox output are shown inside a formatted Panel, or full output is rendered with `-v` / `--verbose`.

---

## 3. Verification & Tests

Correctness of changes is verified by comprehensive automated unit and integration tests:

1. **Wizard Tests (`tests/test_cli_wizard.py`):**
   - Tests execution of `ai-os wizard` via `CliRunner` under both healthy and missing Docker/tool environments.
2. **Project Add Tests (`tests/test_onboarding.py` & CLI unit tests):**
   - Tests interactive prompt confirmation in `project add`, `--deep-scan` option, and automatic `.ai-os/` config file generation in a temporary directory (`tmp_path`).
3. **Integration Regression Suite:**
   - Full pytest suite (550+ tests) passes cleanly without error (`pytest`).

---

## 4. Summary

Re-integrating the wizard and onboarding features guarantees complete preservation of legacy functionality while providing modern interactive setup and transparent event rendering capabilities for AI-OS users.
