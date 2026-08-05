# AI-OS examples

Two worked examples. Neither hides a real LLM call behind a free-looking command
— the parts that cost usage are clearly marked.

## 1. Create a complete project from zero → `create_full_project.sh`

Scaffolds a full **FastAPI + React monorepo** with a Postgres-backed test setup,
turns on the coverage gate, analyzes it, and (optionally) has AI-OS build a
feature end-to-end and open a PR.

```bash
# Deterministic + free: scaffold, patch the coverage gate, scan.
./examples/create_full_project.sh

# Also run the epic (REAL LLM usage + Docker sandbox):
RUN_EPIC=1 ./examples/create_full_project.sh
```

What it demonstrates:
- `ai-os init <path> --stack fastapi-react --with-db` — a working project from
  zero (source + a passing test + `.ai-os/sandbox.json` + `.ai-os/conventions.md`
  + a git `main` commit + registry entry), no network/host toolchain needed.
- Adding a **coverage gate** (`.ai-os/sandbox.json` `"coverage"`) so the agent
  can't pass validation with untested code.
- `ai-os scan` — the deterministic Knowledge Graph.
- `ai-os epic run` — decompose → route by risk → edit → sandbox-validate → PR,
  for both a Python backend epic and a TypeScript frontend epic.
- `ai-os epic history` / `ai-os cost` — read back token/USD spend.

Other stacks `ai-os init` supports: `fastapi`, `react`, `spring`, `next-prisma`
(all accept `--with-db`).

## 2. Configure OpenRouter with multiple models → `openrouter-multimodel.env`

Routes each **risk level** to a different model through a single OpenRouter key
(cheap models for low-risk tasks, strong models for critical ones), with
optional fallback to your Claude subscription for the hardest work, plus 429
backoff and a per-epic cost cap.

```bash
# Copy the lines you want into your real `.env`, then confirm what was picked up:
ai-os llm list
```

The per-task routing (provider → model) is printed in the plan table before you
approve a run, so you can see exactly where each task will go.

Model slugs in that file are illustrative — check the live catalog and pricing at
<https://openrouter.ai/models>. Because the risk→model matrix is pure env config,
a model rename is a one-line change, never a code edit.
