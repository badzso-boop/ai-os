"""Optional, repo-side sandbox configuration (`.ai-os/sandbox.json`).

This is how a project declares what its test suite needs beyond the language
profile's defaults — most importantly a **database**. It's committed in the
project repo (so it lands in every git worktree AI-OS validates), and the
project owns it: AI-OS never needs to understand the project's schema, it just
starts the declared services on an isolated network and runs the declared
setup/seed + test commands the project provides.

Shape (every field optional; absent file → `None` → current single-container,
`--network none` behavior):

    {
      "database": {
        "image": "postgres:16",              // or pgvector/pgvector:pg16, etc.
        "hostname": "db",                     // DNS name the app connects to
        "env": { "POSTGRES_USER": "app", "POSTGRES_PASSWORD": "app", "POSTGRES_DB": "app" },
        "ready_command": "pg_isready -U app -d app"
      },
      "env": { "DATABASE_URL": "postgres://app:app@db:5432/app" },  // injected into setup+test
      "setup_commands": [                     // run (DB reachable, internet NOT) before tests
        "prisma migrate deploy",
        "tsx scripts/seed-reference.ts"       // the repo-maintained reference-data seed
      ],
      "test_command": "pnpm test:unit"        // optional; overrides the profile's default
    }

Security note: when a database is declared, the setup + test containers run on a
Docker `--internal` network (no route to the outside), so the untrusted test
code can reach the DB but still cannot exfiltrate to the internet.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

CONFIG_RELPATH = Path(".ai-os") / "sandbox.json"


class SandboxConfigError(ValueError):
    """`.ai-os/sandbox.json` exists but is malformed."""


@dataclass(frozen=True)
class CoverageConfig:
    """A code-coverage gate for the validation run (Phase 6, feature 1b). When
    present, AI-OS enforces a minimum coverage threshold so an agent can't pass
    validation with weak/absent tests — a change with no exercising test drops
    coverage and fails the gate, feeding the agent a "add tests" signal on retry.

    `min_percent` is the fail-under threshold. `paths` are the packages/dirs to
    measure (default: the whole project). For Python (the out-of-the-box case)
    AI-OS wraps the default `pytest` command with `pytest-cov`
    (`--cov=<path> --cov-fail-under=<min>`); for Node/Java, set your own
    coverage-producing `test_command` (see the README) — the threshold is
    advisory metadata there."""

    min_percent: float = 0.0
    paths: tuple[str, ...] = ()

    @property
    def enabled(self) -> bool:
        return self.min_percent > 0.0


@dataclass(frozen=True)
class DatabaseService:
    image: str = "postgres:16"
    hostname: str = "db"
    env: dict[str, str] = field(default_factory=dict)
    ready_command: str = "pg_isready"
    memory: str = "1g"


@dataclass(frozen=True)
class SandboxConfig:
    database: DatabaseService | None = None
    env: dict[str, str] = field(default_factory=dict)
    setup_commands: tuple[str, ...] = ()
    test_command: str | None = None
    # Optional coverage gate (Phase 6). `None` = no coverage enforcement.
    coverage: CoverageConfig | None = None
    # Override the language profile's base image — e.g. point at a Playwright
    # image (`mcr.microsoft.com/playwright:...`) so `setup_commands` can install
    # browsers and `test_command` can run e2e tests. `None` = the profile default.
    image: str | None = None

    @property
    def needs_database(self) -> bool:
        return self.database is not None


def _as_str_map(value: object, where: str) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, dict) or not all(
        isinstance(k, str) and isinstance(v, (str, int, float, bool)) for k, v in value.items()
    ):
        raise SandboxConfigError(f"{where} must be an object of string values")
    return {k: str(v) for k, v in value.items()}


def parse_sandbox_config(data: object) -> SandboxConfig:
    """Parse an already-loaded JSON object into a `SandboxConfig`, applying
    defaults and validating types (raising `SandboxConfigError` on anything
    malformed)."""
    if not isinstance(data, dict):
        raise SandboxConfigError("sandbox config root must be a JSON object")

    database = None
    raw_db = data.get("database")
    if raw_db is not None:
        if not isinstance(raw_db, dict):
            raise SandboxConfigError("'database' must be an object")
        db_env = _as_str_map(raw_db.get("env"), "database.env")
        # A sensible default readiness probe honoring the declared user/db.
        user = db_env.get("POSTGRES_USER", "postgres")
        db_name = db_env.get("POSTGRES_DB", user)
        default_ready = f"pg_isready -U {user} -d {db_name}"
        database = DatabaseService(
            image=str(raw_db.get("image", "postgres:16")),
            hostname=str(raw_db.get("hostname", "db")),
            env=db_env,
            ready_command=str(raw_db.get("ready_command", default_ready)),
            memory=str(raw_db.get("memory", "1g")),
        )

    setup = data.get("setup_commands") or []
    if not isinstance(setup, list) or not all(isinstance(c, str) for c in setup):
        raise SandboxConfigError("'setup_commands' must be a list of strings")

    test_command = data.get("test_command")
    if test_command is not None and not isinstance(test_command, str):
        raise SandboxConfigError("'test_command' must be a string")

    image = data.get("image")
    if image is not None and not isinstance(image, str):
        raise SandboxConfigError("'image' must be a string")

    coverage = None
    raw_cov = data.get("coverage")
    if raw_cov is not None:
        if not isinstance(raw_cov, dict):
            raise SandboxConfigError("'coverage' must be an object")
        min_pct = raw_cov.get("min_percent", 0)
        if not isinstance(min_pct, (int, float)) or isinstance(min_pct, bool):
            raise SandboxConfigError("coverage.min_percent must be a number")
        raw_paths = raw_cov.get("paths") or []
        if not isinstance(raw_paths, list) or not all(isinstance(p, str) for p in raw_paths):
            raise SandboxConfigError("coverage.paths must be a list of strings")
        coverage = CoverageConfig(min_percent=float(min_pct), paths=tuple(raw_paths))

    return SandboxConfig(
        database=database,
        env=_as_str_map(data.get("env"), "env"),
        setup_commands=tuple(setup),
        test_command=test_command,
        image=image,
        coverage=coverage,
    )


def load_sandbox_config(worktree_path: Path, language: str | None = None) -> SandboxConfig | None:
    """Load `<worktree>/.ai-os/sandbox.json` if present, else `None`. Raises
    `SandboxConfigError` if the file exists but is malformed.

    Multi-language monorepos can key config per language under a top-level
    `languages` map, e.g.::

        {
          "languages": {
            "python":     { "test_command": "cd backend && pytest" },
            "typescript": { "setup_commands": ["cd frontend && pnpm prisma generate"],
                            "test_command": "cd frontend && pnpm typecheck" }
          }
        }

    When `language` is given and present under `languages`, its sub-config is
    merged over the top-level fields (top-level acts as shared defaults). If
    there's no `languages` map, the flat config applies to every task."""
    path = Path(worktree_path) / CONFIG_RELPATH
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SandboxConfigError(f"{path} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise SandboxConfigError("sandbox config root must be a JSON object")

    per_language = data.get("languages")
    if per_language is not None:
        if not isinstance(per_language, dict):
            raise SandboxConfigError("'languages' must be an object of language -> config")
        shared = {k: v for k, v in data.items() if k != "languages"}
        if language is not None and language in per_language:
            sub = per_language[language]
            if not isinstance(sub, dict):
                raise SandboxConfigError(f"languages.{language} must be an object")
            return parse_sandbox_config({**shared, **sub})
        # A task in a language the map doesn't cover falls back to shared defaults
        # (or None when there are none) — so it isn't accidentally DB-validated.
        return parse_sandbox_config(shared) if shared else None
    return parse_sandbox_config(data)
