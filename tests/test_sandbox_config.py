"""Deterministic tests for `.ai-os/sandbox.json` parsing + the DB-aware argv
(no Docker needed)."""
from __future__ import annotations

import json

import pytest

from ai_os.sandbox.container_runner import SANDBOX_PROFILES, build_docker_argv
from ai_os.sandbox.sandbox_config import (
    SandboxConfigError,
    load_sandbox_config,
    parse_sandbox_config,
)


def test_absent_config_is_none(tmp_path):
    assert load_sandbox_config(tmp_path) is None


def test_full_config_parses_with_database(tmp_path):
    (tmp_path / ".ai-os").mkdir()
    (tmp_path / ".ai-os" / "sandbox.json").write_text(json.dumps({
        "database": {
            "image": "pgvector/pgvector:pg16",
            "hostname": "db",
            "env": {"POSTGRES_USER": "app", "POSTGRES_PASSWORD": "app", "POSTGRES_DB": "app"},
        },
        "env": {"DATABASE_URL": "postgres://app:app@db:5432/app"},
        "setup_commands": ["prisma migrate deploy", "tsx scripts/seed.ts"],
        "test_command": "pnpm test:unit",
    }))
    config = load_sandbox_config(tmp_path)
    assert config is not None and config.needs_database
    assert config.database.image == "pgvector/pgvector:pg16"
    assert config.database.hostname == "db"
    # readiness probe defaults to the declared user/db
    assert config.database.ready_command == "pg_isready -U app -d app"
    assert config.env["DATABASE_URL"].startswith("postgres://")
    assert config.setup_commands == ("prisma migrate deploy", "tsx scripts/seed.ts")
    assert config.test_command == "pnpm test:unit"


def test_config_without_database_is_isolated(tmp_path):
    config = parse_sandbox_config({"env": {"X": "1"}, "setup_commands": ["echo hi"]})
    assert config.needs_database is False
    assert config.env == {"X": "1"}
    assert config.setup_commands == ("echo hi",)
    assert config.test_command is None


def test_database_defaults():
    config = parse_sandbox_config({"database": {}})
    assert config.database.image == "postgres:16"
    assert config.database.hostname == "db"
    assert config.database.ready_command == "pg_isready -U postgres -d postgres"


def test_env_values_coerced_to_strings():
    config = parse_sandbox_config({"env": {"PORT": 5432, "DEBUG": True}})
    assert config.env == {"PORT": "5432", "DEBUG": "True"}


@pytest.mark.parametrize("bad", [
    [],                                          # root not an object
    {"setup_commands": "not-a-list"},
    {"setup_commands": [1, 2]},
    {"test_command": 123},
    {"database": "postgres"},
    {"env": {"K": {"nested": "obj"}}},
])
def test_malformed_config_raises(bad):
    with pytest.raises(SandboxConfigError):
        parse_sandbox_config(bad)


def test_invalid_json_file_raises(tmp_path):
    (tmp_path / ".ai-os").mkdir()
    (tmp_path / ".ai-os" / "sandbox.json").write_text("{ not json ")
    with pytest.raises(SandboxConfigError):
        load_sandbox_config(tmp_path)


def test_per_language_deep_merge(tmp_path):
    (tmp_path / ".ai-os").mkdir()
    (tmp_path / ".ai-os" / "sandbox.json").write_text(json.dumps({
        "env": {"COMMON_ENV": "1", "OVERRIDDEN_ENV": "base"},
        "setup_commands": ["echo base_setup"],
        "languages": {
            "python": {
                "env": {"PYTHON_ENV": "2", "OVERRIDDEN_ENV": "py_override"},
                "setup_commands": ["echo py_setup"],
                "test_command": "pytest",
            }
        }
    }))
    config = load_sandbox_config(tmp_path, language="python")
    assert config is not None
    assert config.env == {
        "COMMON_ENV": "1",
        "OVERRIDDEN_ENV": "py_override",
        "PYTHON_ENV": "2",
    }
    assert config.setup_commands == ("echo base_setup", "echo py_setup")
    assert config.test_command == "pytest"



# -- DB-aware argv (internal network + injected env) -------------------------


def test_argv_uses_internal_network_and_injects_env(tmp_path):
    argv = build_docker_argv(
        "docker", tmp_path, "ai-os-setup-x", SANDBOX_PROFILES["python"],
        network="ai-os-sbx-net-abc",
        extra_env=(("DATABASE_URL", "postgres://app:app@db:5432/app"),),
        command="python seed.py",
    )
    assert argv[argv.index("--network") + 1] == "ai-os-sbx-net-abc"
    assert "DATABASE_URL=postgres://app:app@db:5432/app" in argv
    # command override lands in the sh -c slot
    assert argv[-3:] == ["sh", "-c", "python seed.py"]
    # hardening is still fully applied even with a network + env
    assert "--cap-drop=ALL" in argv
    assert argv[argv.index("--user") + 1] == "1000:1000"


def test_default_argv_still_network_none(tmp_path):
    # Regression: the default (no DB) path stays fully isolated.
    argv = build_docker_argv("docker", tmp_path, "n", SANDBOX_PROFILES["python"])
    assert argv[argv.index("--network") + 1] == "none"
