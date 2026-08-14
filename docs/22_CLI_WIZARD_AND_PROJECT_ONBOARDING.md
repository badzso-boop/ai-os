# 22. CLI Wizard & Interactive Project Onboarding — `ai-os wizard` & AI-driven `ai-os project add`

> **Status: Implemented (Active).** This document describes the operational specification and design of the `ai-os wizard` post-install wizard and the enhanced, AI-driven `ai-os project add` automatic project configuration generator.

---

## 1. Summary in One Sentence

`ai-os wizard` is an **interactive post-installation wizard** that guides the user through checking dependencies (Docker, Git, gh CLI), setting up LLM providers/authentications, and testing the sandbox execution environment. The enhanced **`ai-os project add`** automatically scans project documentation (`CLAUDE.md`, `README.md`, `docs/`, `package.json`, etc.) and uses a **LOW/MEDIUM AI model to automatically generate** the project's `.ai-os/` configuration files (`instructions.json`, `conventions.md`, `sandbox.json`, `ui.json`) — or, if documentation is missing, offers a **MEDIUM/HIGH AI deep codebase analysis** based on the code structure.

---

## 2. Detailed Architecture and Workflow

### 2.1. `ai-os wizard` — Interactive Installation Wizard

The goal of this command is to enable the user to **activate the AI-OS system with a single command** immediately after `pip install`, without manually copying and editing `.env` files.

```
ai-os wizard
```

#### Wizard Steps:
1. **Environment Check:**
   - Python version (Python 3.13+ expected)
   - Docker daemon status (`docker info`) — if not running, warns about the sandbox dependency
   - Git & GitHub CLI (`gh auth status`) presence
2. **Provider Authentication & Login Wizard:**
   - Detects existing CLI session logins:
     - Google Antigravity CLI (`agy`) account
     - Anthropic CLI (`claude`) account
   - Checks environment variables (`GEMINI_API_KEY`, `OPENROUTER_API_KEY`, `ANTHROPIC_API_KEY`)
   - Offers to prompt for missing API keys or use session-based account login
   - Executes a test ping (`ai-os llm list`)
3. **Sandbox Docker Image Check:**
   - Checks for the presence of the `ai-os-sandbox-python:3.12` Docker image
   - If missing, automatically prompts to run `docker build -t ai-os-sandbox-python:3.12 -f docker/python-sandbox.Dockerfile .`
4. **Risk Routing & Budget Settings:**
   - Interactive prompt to assign provider models for risk levels (LOW, MEDIUM, HIGH, CRITICAL)
   - Optional setting for `AI_OS_EPIC_BUDGET_USD` to prevent budget overruns

---

### 2.2. Enhanced `ai-os project add` — AI-driven Configuration Generation

When a user adds a project to the AI-OS register:

```bash
ai-os project add <name> <path> [--deep]
```

#### Workflow Steps:

```
[ai-os project add <name> <path>]
         │
         ▼
(1) Static Documentation Search ── (CLAUDE.md, README.md, docs/, build manifests)
         │
         ├──► ARE THERE documentation files?
         │         │
         │         ├─► [YES] ──► (2) Fast AI Synthesis (LOW / MEDIUM model)
         │         │                Generates: instructions.json, conventions.md, sandbox.json
         │         │
         │         └─► [NO]  ──► (3) Warning + Interactive Prompt:
         │                           "[warning] No documentation files found in <path>.
         │                           Would you like a MEDIUM/HIGH AI model to perform
         │                           a deep codebase inspection and auto-generate configs? [Y/n]"
         │                                 │
         │                                 └─► [YES] ──► (4) Deep Inspection (MEDIUM / HIGH model)
         │                                                   Read code structure, AST, manifests
         │                                                   Generates complete .ai-os/ config suite
```

---

### 2.3. CLI Readability and Lifecycle Rendering (Enhanced Glass-Box CLI Stream)

During `ai-os epic run` and `ai-os task run`, terminal output is displayed using **rich formatting** with clear step statuses:
- **Rich Stream Panel**: Color-coded status icons (▶ running, ✓ merged successfully, ⚠ warning, ✗ sandbox error), elapsed execution timer, and real-time token/USD cost counters.
- **Clear Details**: Framed, clean preview of sandbox output tail and test critic observations.

---

### 2.4. Safe Git MCP Tools

The `ai_os/mcp/mcp_server.py` server is extended with safety-gated Git MCP tools:
- `git_status`: Returns current git branch, modified files, and untracked files.
- `git_pull_main`: Safely pulls the latest `main` branch (`git pull origin main`), guaranteeing no file overwrites if there are uncommitted changes.
- `git_create_branch`: Creates a new feature branch matching the specified naming pattern.
- `git_diff_summary`: Returns differences from the trunk for testing and code review.

---

## 3. Generated Configuration File Format

### 3.1. `.ai-os/instructions.json`
Contains machine-readable project specifications, commands, and documentation references:

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
Project-specific coding conventions, i18n rules, UI responsiveness guidelines (e.g., 375px mobile viewport), and testing expectations.

### 3.3. `.ai-os/sandbox.json`
Execution commands and environment variables for the Docker sandbox:

```json
{
  "setup_commands": ["pip install -r requirements.txt"],
  "test_command": "pytest -q"
}
```

---

## 4. Implementation Modules

- **`ai_os/core/wizard.py`**: Interactive environment checking, login testing, `.env` management.
- **`ai_os/core/onboarding.py`**: Documentation scanner, LLM prompt templates for config generation, deep analysis fallback.
- **`ai_os/cli.py`**: `@main.command("wizard")` and expanded `@project.command("add")`.
- **`tests/test_wizard.py`** & **`tests/test_onboarding.py`**: Unit and CLI tests using `CliRunner` and `tmp_path`.

---

## Related Documents

- `docs/01_ARCHITECTURE_OVERVIEW.md` — General system architecture.
- `docs/20_STARTUP_GENERATOR.md` — Static demo generator specification.
- `docs/21_STATIC_SUBDOMAIN_DEPLOY.md` — Live subdomain deploy script specification.
