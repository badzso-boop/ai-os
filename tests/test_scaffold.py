"""Tests for project scaffolding + per-task language resolution + per-language
sandbox config (the "start from zero" / multi-language epic feature)."""
from __future__ import annotations

import json

import pytest

from ai_os.core.epic_runner import resolve_task_language
from ai_os.core.models import TaskNode
from ai_os.core.scaffold import DB_CAPABLE, PRESETS, scaffold_files, write_scaffold
from ai_os.sandbox.sandbox_config import load_sandbox_config


# -- scaffolding -------------------------------------------------------------


@pytest.mark.parametrize("preset", PRESETS)
def test_scaffold_files_include_sandbox_config(preset):
    files = scaffold_files(preset)
    assert ".ai-os/sandbox.json" in files
    # every declared sandbox.json is valid JSON
    json.loads(files[".ai-os/sandbox.json"])


def test_scaffold_fastapi_has_app_and_test():
    files = scaffold_files("fastapi")
    assert "app/main.py" in files and "tests/test_health.py" in files
    assert "requirements.txt" in files and "fastapi" in files["requirements.txt"]


def test_scaffold_react_has_package_and_tsconfig():
    files = scaffold_files("react")
    assert "package.json" in files and "tsconfig.json" in files
    assert "src/App.tsx" in files and "src/App.test.tsx" in files
    json.loads(files["package.json"])
    cfg = json.loads(files[".ai-os/sandbox.json"])
    assert cfg["test_command"] == "npm run typecheck && npm test"


def test_scaffold_monorepo_has_both_and_per_language_config():
    files = scaffold_files("fastapi-react")
    assert "backend/app/main.py" in files
    assert "frontend/src/App.tsx" in files
    # manifests at root so the sandbox can install them
    assert "requirements.txt" in files and "package.json" in files
    cfg = json.loads(files[".ai-os/sandbox.json"])
    assert set(cfg["languages"]) == {"python", "typescript"}
    assert cfg["languages"]["typescript"]["test_command"] == "npm run typecheck && npm test"


def test_write_scaffold_creates_files_and_refuses_overwrite(tmp_path):
    written = write_scaffold(tmp_path / "proj", "fastapi")
    assert "app/main.py" in written
    assert (tmp_path / "proj" / "app" / "main.py").is_file()
    # refuses to clobber an existing project
    with pytest.raises(FileExistsError):
        write_scaffold(tmp_path / "proj", "fastapi")


def test_unknown_preset_raises():
    with pytest.raises(ValueError):
        scaffold_files("cobol-mainframe")


def test_scaffold_spring_has_pom_and_test():
    files = scaffold_files("spring")
    assert "pom.xml" in files and "spring-boot-starter-web" in files["pom.xml"]
    assert "src/main/java/com/example/demo/HealthController.java" in files
    assert "src/test/java/com/example/demo/HealthControllerTest.java" in files


def test_scaffold_next_prisma_has_schema_and_config():
    files = scaffold_files("next-prisma")
    assert "prisma/schema.prisma" in files and "model Widget" in files["prisma/schema.prisma"]
    assert "package.json" in files and "@prisma/client" in files["package.json"]
    cfg = json.loads(files[".ai-os/sandbox.json"])
    assert cfg["test_command"] == "npm run typecheck && npm test"


@pytest.mark.parametrize("preset", DB_CAPABLE)
def test_with_db_adds_a_database_and_seed(preset):
    files = scaffold_files(preset, with_db=True)
    cfg = json.loads(files[".ai-os/sandbox.json"])
    # a database is declared (flat, or under a language for the monorepo)
    has_db = "database" in cfg or any("database" in v for v in cfg.get("languages", {}).values())
    assert has_db
    # a seed exists somewhere
    assert any("seed" in rel.lower() or "V900" in rel for rel in files)


def test_with_db_rejected_for_react():
    with pytest.raises(ValueError):
        scaffold_files("react", with_db=True)


def test_with_db_prisma_uses_db_push_and_seed():
    files = scaffold_files("next-prisma", with_db=True)
    assert "prisma/seed.ts" in files
    cfg = json.loads(files[".ai-os/sandbox.json"])
    assert cfg["database"]["image"].startswith("postgres")
    assert any("db push" in c for c in cfg["setup_commands"])


def test_with_db_monorepo_scopes_db_to_python(tmp_path):
    files = scaffold_files("fastapi-react", with_db=True)
    cfg = json.loads(files[".ai-os/sandbox.json"])
    assert "database" in cfg["languages"]["python"]      # backend gets the DB
    assert "database" not in cfg["languages"]["typescript"]  # frontend does not
    assert "backend/scripts/seed.py" in files


# -- per-task language resolution (multi-language epic) ----------------------


def _t(tid, files, language=None):
    return TaskNode(id=tid, title=tid, description="d", risk_level="LOW",
                    target_files=files, write_set=set(files), language=language)


def test_language_derived_from_python_files():
    assert resolve_task_language(_t("A", ["backend/app/main.py"]), "typescript") == "python"


def test_language_derived_from_tsx_files():
    assert resolve_task_language(_t("B", ["frontend/src/App.tsx"]), "python") == "typescript"


def test_explicit_task_language_wins():
    assert resolve_task_language(_t("C", ["a.py"], language="java"), "python") == "java"


def test_falls_back_to_default_when_undeterminable():
    assert resolve_task_language(_t("D", ["notes.txt"]), "python") == "python"


def test_sandbox_unsupported_extension_votes_are_dropped():
    # A Prisma schema change task whose only recognized-extension file is a
    # raw .sql migration: the analyzer recognizes "sql" as a language, but
    # the sandbox has no profile for it. The vote must be dropped rather than
    # "winning" and later crashing SandboxLanguageNotSupportedError deep in
    # validation — this should fall back to the epic's default language.
    task = _t("E", ["prisma/migrations/0001/migration.sql", "prisma/schema.prisma"])
    assert resolve_task_language(task, "typescript") == "typescript"


def test_explicit_unsupported_task_language_falls_back_too():
    # Same story if the epic planner itself set an unsupported language
    # explicitly rather than it being inferred from file extensions.
    task = _t("F", ["schema.sql"], language="sql")
    assert resolve_task_language(task, "typescript") == "typescript"


def test_sandbox_supported_language_still_wins_over_unsupported_sibling():
    # A task touching both a .ts file and a .sql migration should still
    # resolve to typescript (the supported one), not fall through to default.
    task = _t(
        "G",
        ["src/db/migrate.sql", "src/server/domains/foo/foo.service.ts"],
    )
    assert resolve_task_language(task, "python") == "typescript"


# -- per-language sandbox config ---------------------------------------------


def test_per_language_sandbox_config_picks_sublang(tmp_path):
    (tmp_path / ".ai-os").mkdir()
    (tmp_path / ".ai-os" / "sandbox.json").write_text(json.dumps({
        "languages": {
            "python": {"test_command": "cd backend && pytest -q"},
            "typescript": {"test_command": "npm run typecheck"},
        }
    }))
    py = load_sandbox_config(tmp_path, language="python")
    ts = load_sandbox_config(tmp_path, language="typescript")
    assert py.test_command == "cd backend && pytest -q"
    assert ts.test_command == "npm run typecheck"
    # a language not in the map + no shared defaults -> None (plain isolated run)
    assert load_sandbox_config(tmp_path, language="java") is None


def test_per_language_merges_shared_defaults(tmp_path):
    (tmp_path / ".ai-os").mkdir()
    (tmp_path / ".ai-os" / "sandbox.json").write_text(json.dumps({
        "env": {"CI": "1"},
        "languages": {"python": {"test_command": "pytest"}},
    }))
    py = load_sandbox_config(tmp_path, language="python")
    assert py.test_command == "pytest"
    assert py.env == {"CI": "1"}  # shared default merged in
