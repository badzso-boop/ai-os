#!/usr/bin/env bash
#
# Example: create a COMPLETE project from zero with AI-OS, then let it build a
# feature end-to-end (decompose -> route to models -> edit -> sandbox-validate
# -> open a PR).
#
# The `ai-os init` + `ai-os scan` steps are DETERMINISTIC and free (no LLM, no
# network) — run them freely. The `ai-os epic run` steps make REAL LLM calls and
# consume real usage/quota, so they are gated behind RUN_EPIC=1 (see the bottom).
#
# Usage:
#   ./examples/create_full_project.sh              # scaffold + scan only (free)
#   RUN_EPIC=1 ./examples/create_full_project.sh   # also run the epic (real usage)
#
# Prereqs: `pip install -e .` (so `ai-os` is on PATH), Docker running (for the
# sandbox), and at least one provider configured in `.env` (see
# `ai-os llm list`). For the OpenRouter multi-model setup, see
# `examples/openrouter-multimodel.env`.

set -euo pipefail

PROJECT_DIR="${PROJECT_DIR:-./demo-shop}"
PROJECT_NAME="${PROJECT_NAME:-demo-shop}"

echo "==> 1. Scaffold a full FastAPI + React monorepo WITH a Postgres-backed"
echo "       test setup, from zero. Creates source + a passing test + a wired"
echo "       .ai-os/sandbox.json + .ai-os/conventions.md, git-inits it with a"
echo "       'main' commit, and registers it — all deterministic, no LLM."
#   --stack: fastapi | react | fastapi-react | spring | next-prisma
#   --with-db: adds a Postgres sidecar to the sandbox + a migration/seed + a DB test
ai-os init "$PROJECT_DIR" --stack fastapi-react --with-db --name "$PROJECT_NAME"

echo
echo "==> 2. (Optional) Turn on the Phase-6 coverage gate so the agent can't pass"
echo "       validation with untested code. We add a coverage block to the"
echo "       python language config in .ai-os/sandbox.json."
python3 - "$PROJECT_DIR" <<'PY'
import json, sys
from pathlib import Path
cfg_path = Path(sys.argv[1]) / ".ai-os" / "sandbox.json"
cfg = json.loads(cfg_path.read_text())
# The fastapi-react scaffold keys config per language under "languages".
cfg["languages"]["python"]["coverage"] = {"min_percent": 80, "paths": ["backend/app"]}
cfg_path.write_text(json.dumps(cfg, indent=2) + "\n")
print(f"  patched {cfg_path} — python coverage gate: min 80% on backend/app")
PY

echo
echo "==> 3. Analyze the scaffold (deterministic Knowledge Graph — no LLM)."
ai-os scan "$PROJECT_NAME"

echo
echo "==> 4. Build a feature end-to-end. THIS MAKES REAL LLM CALLS."
echo "       AI-OS decomposes the request into a task DAG, shows the plan +"
echo "       a rough token/USD estimate for approval (HITL Stage 1), routes each"
echo "       task to a model by risk, lets the model edit code + call tools,"
echo "       validates every change in the Docker sandbox, and opens a PR whose"
echo "       body includes the DAG + a 'Validator quality' readout."
echo
BACKEND_PROMPT='add a /products CRUD API (list, get, create) backed by the DB, with tests'
FRONTEND_PROMPT='add a products list page that calls GET /products'

if [[ "${RUN_EPIC:-0}" == "1" ]]; then
  # A Python (backend) epic. Drop --yes to review the plan interactively first.
  ai-os epic run "$PROJECT_NAME" --prompt "$BACKEND_PROMPT" --language python

  # A TypeScript (frontend) epic — same project, its own sandbox profile.
  ai-os epic run "$PROJECT_NAME" --prompt "$FRONTEND_PROMPT" --language typescript

  # NOTE: a single epic can also span BOTH — if the DAG's tasks touch a mix of
  # .py and .tsx files, each task validates with its own language profile
  # (majority-vote from its file extensions). Just pick the majority language
  # as --language and let per-task resolution handle the rest.
else
  echo "  [skipped — real usage] To actually run it:"
  echo "    ai-os epic run $PROJECT_NAME --prompt \"$BACKEND_PROMPT\" --language python"
  echo "    ai-os epic run $PROJECT_NAME --prompt \"$FRONTEND_PROMPT\" --language typescript"
fi

echo
echo "==> 5. After a run, read back what it cost:"
echo "    ai-os epic history            # every epic: status, task counts, tokens, USD"
echo "    ai-os cost --epic <epic-id>   # spend grouped by provider+model"
echo
echo "Done. The project lives at: $PROJECT_DIR"
