"""Deterministic project scaffolding for `ai-os init` — start a project from
zero so AI-OS has a real git `main` + a working test baseline to build on.

Templates are written directly (no `npx create-*` / network / host toolchain at
init time), so scaffolding is deterministic and hermetic. Each preset lays down
a minimal-but-WORKING baseline: source + a passing test + a `.ai-os/sandbox.json`
wired for that stack, so the very first `ai-os epic run` already validates.

The `fastapi-react` monorepo is laid out so the existing sandbox validates it
without extra machinery: dependency manifests live at the repo ROOT (Python
installs to global site-packages; the Node copy-image's root `node_modules`
resolves from a subdir upward), and a per-language `.ai-os/sandbox.json`
(`languages` map) points each language's tests at its subdir.
"""
from __future__ import annotations

from pathlib import Path

PRESETS = ("fastapi", "react", "fastapi-react")

_GITIGNORE_PY = "__pycache__/\n*.pyc\n.venv/\nvenv/\n.pytest_cache/\n.ai-os/worktrees/\n"
_GITIGNORE_NODE = "node_modules/\ndist/\n.vite/\n"

_FASTAPI_MAIN = '''\
from fastapi import FastAPI

app = FastAPI(title="Scaffolded API")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
'''

_FASTAPI_TEST = '''\
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_health_ok():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
'''

_FASTAPI_REQS = "fastapi\nuvicorn\nhttpx\npytest\n"

_VITE_TSCONFIG = '''\
{
  "compilerOptions": {
    "target": "ES2020",
    "useDefineForClassFields": true,
    "lib": ["ES2020", "DOM", "DOM.Iterable"],
    "module": "ESNext",
    "skipLibCheck": true,
    "moduleResolution": "bundler",
    "jsx": "react-jsx",
    "strict": true,
    "noEmit": true
  },
  "include": ["src"]
}
'''

_VITE_APP = '''\
export function greeting(name: string): string {
  return `Hello, ${name}!`
}

export default function App() {
  return <h1>{greeting("world")}</h1>
}
'''

_VITE_APP_TEST = '''\
import { describe, it, expect } from "vitest"
import { greeting } from "./App"

describe("greeting", () => {
  it("greets by name", () => {
    expect(greeting("AI-OS")).toBe("Hello, AI-OS!")
  })
})
'''

_VITE_MAIN = '''\
import { StrictMode } from "react"
import { createRoot } from "react-dom/client"
import App from "./App"

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
  </StrictMode>,
)
'''

_VITE_INDEX_HTML = '''\
<!doctype html>
<html lang="en">
  <head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0" />
    <title>Scaffolded App</title>
  </head>
  <body>
    <div id="root"></div>
    <script type="module" src="/src/main.tsx"></script>
  </body>
</html>
'''

_VITE_CONFIG = '''\
import { defineConfig } from "vite"
import react from "@vitejs/plugin-react"

export default defineConfig({ plugins: [react()] })
'''


def _react_package_json(name: str, typecheck: str, tsx_dir: str) -> str:
    return f'''\
{{
  "name": "{name}",
  "private": true,
  "version": "0.0.0",
  "type": "module",
  "scripts": {{
    "dev": "vite",
    "build": "tsc && vite build",
    "typecheck": "{typecheck}",
    "test": "vitest run"
  }},
  "dependencies": {{
    "react": "^18.3.1",
    "react-dom": "^18.3.1"
  }},
  "devDependencies": {{
    "@types/react": "^18.3.3",
    "@types/react-dom": "^18.3.0",
    "@vitejs/plugin-react": "^4.3.1",
    "typescript": "^5.5.3",
    "vite": "^5.4.0",
    "vitest": "^2.1.0"
  }}
}}
'''


def _readme(title: str, body: str) -> str:
    return f"# {title}\n\nScaffolded by `ai-os init`.\n\n{body}\n"


def _fastapi_files(prefix: str = "") -> dict[str, str]:
    p = prefix
    return {
        f"{p}app/__init__.py": "",
        f"{p}app/main.py": _FASTAPI_MAIN,
        f"{p}tests/__init__.py": "",
        f"{p}tests/test_health.py": _FASTAPI_TEST,
    }


def _react_files(prefix: str = "") -> dict[str, str]:
    p = prefix
    return {
        f"{p}index.html": _VITE_INDEX_HTML,
        f"{p}vite.config.ts": _VITE_CONFIG,
        f"{p}tsconfig.json": _VITE_TSCONFIG,
        f"{p}src/main.tsx": _VITE_MAIN,
        f"{p}src/App.tsx": _VITE_APP,
        f"{p}src/App.test.tsx": _VITE_APP_TEST,
    }


def scaffold_files(preset: str) -> dict[str, str]:
    """Return `{relpath: content}` for a preset (does not touch disk)."""
    if preset == "fastapi":
        files = _fastapi_files()
        files["requirements.txt"] = _FASTAPI_REQS
        files[".gitignore"] = _GITIGNORE_PY
        files["README.md"] = _readme("FastAPI service", "Run tests: `pytest -q`.")
        # python profile already runs pytest + installs requirements.txt; explicit
        # for clarity / easy editing.
        files[".ai-os/sandbox.json"] = '{ "test_command": "pytest -q" }\n'
        return files

    if preset == "react":
        files = _react_files()
        files["package.json"] = _react_package_json("frontend", "tsc --noEmit", "src")
        files[".gitignore"] = _GITIGNORE_NODE
        files["README.md"] = _readme("React (Vite + TS) app", "Typecheck: `npm run typecheck`. Test: `npm test`.")
        files[".ai-os/sandbox.json"] = '{ "test_command": "npm run typecheck" }\n'
        return files

    if preset == "fastapi-react":
        files: dict[str, str] = {}
        files.update(_fastapi_files(prefix="backend/"))
        files.update(_react_files(prefix="frontend/"))
        # Manifests at ROOT so the sandbox validates without extra machinery:
        # Python installs globally; the Node copy-image installs a root
        # node_modules that resolves from frontend/ upward.
        files["requirements.txt"] = _FASTAPI_REQS
        files["package.json"] = _react_package_json(
            "app", "tsc -p frontend/tsconfig.json --noEmit", "frontend/src"
        )
        files[".gitignore"] = _GITIGNORE_PY + _GITIGNORE_NODE
        files["README.md"] = _readme(
            "FastAPI + React monorepo",
            "Backend in `backend/` (Python), frontend in `frontend/` (TypeScript). "
            "Run each epic per language:\n\n"
            "```bash\nai-os epic run <proj> --prompt \"...\" --language python\n"
            "ai-os epic run <proj> --prompt \"...\" --language typescript\n```",
        )
        files[".ai-os/conventions.md"] = (
            "# Project conventions\n\n"
            "- Backend code lives in `backend/` (Python/FastAPI); frontend in "
            "`frontend/` (React/TypeScript).\n"
            "- Keep dependency manifests at the repo root (`requirements.txt`, "
            "`package.json`) so the sandbox can install them.\n"
        )
        files[".ai-os/sandbox.json"] = (
            "{\n"
            '  "languages": {\n'
            '    "python": { "test_command": "cd backend && pytest -q" },\n'
            '    "typescript": { "test_command": "npm run typecheck" }\n'
            "  }\n"
            "}\n"
        )
        return files

    raise ValueError(f"Unknown preset {preset!r}. Available: {', '.join(PRESETS)}")


def write_scaffold(root: Path, preset: str) -> list[str]:
    """Write a preset's files under `root` (created if needed). Refuses to
    overwrite existing files. Returns the sorted list of created relpaths."""
    root = Path(root)
    files = scaffold_files(preset)
    root.mkdir(parents=True, exist_ok=True)
    written: list[str] = []
    for relpath, content in files.items():
        target = root / relpath
        if target.exists():
            raise FileExistsError(f"{target} already exists — refusing to overwrite")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        written.append(relpath)
    return sorted(written)
