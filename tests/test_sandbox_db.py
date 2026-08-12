"""Opt-in real-Postgres integration test for the DB sidecar flow.

Gated behind AI_OS_TEST_DB_SANDBOX=1 (and Docker) because it pulls postgres:16
and builds a psycopg2 dep image — too heavy for a routine `pytest`, same policy
as the opt-in Java test. Proves end-to-end that: a declared database comes up on
an isolated `--internal` network, the repo-side seed command populates it, the
tests reach it — AND outbound internet is still blocked (no exfiltration).

Run it deliberately:  AI_OS_TEST_DB_SANDBOX=1 .venv/bin/pytest tests/test_sandbox_db.py -q
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess

import pytest

from ai_os.sandbox import container_runner
from ai_os.sandbox.container_runner import EphemeralSandboxRunner, SandboxProfile

pytestmark = pytest.mark.skipif(
    os.environ.get("AI_OS_TEST_DB_SANDBOX") != "1" or shutil.which("docker") is None,
    reason="opt-in only (AI_OS_TEST_DB_SANDBOX=1 with docker; pulls postgres:16)",
)

_TEST_PYTHON_IMAGE = "ai-os-sandbox-test-python:3.12-slim-pytest"


@pytest.fixture(scope="session", autouse=True)
def _build_test_python_image():
    dockerfile = "FROM python:3.12-slim\nRUN pip install --no-cache-dir pytest\n"
    subprocess.run(
        ["docker", "build", "-t", _TEST_PYTHON_IMAGE, "-"],
        input=dockerfile, text=True, capture_output=True, check=True,
    )


@pytest.fixture(autouse=True)
def _two_phase_python_profile(monkeypatch):
    monkeypatch.setitem(
        container_runner.SANDBOX_PROFILES, "python",
        SandboxProfile(
            image=_TEST_PYTHON_IMAGE,
            command="pytest -p no:cacheprovider",
            dependency_manifests=("requirements.txt",),
            install_command="pip install --no-cache-dir -r requirements.txt",
        ),
    )


def _write_project(root):
    (root / "requirements.txt").write_text("psycopg2-binary\n")
    (root / ".ai-os").mkdir()
    (root / ".ai-os" / "sandbox.json").write_text(json.dumps({
        "database": {
            "image": "postgres:16",
            "hostname": "db",
            "env": {"POSTGRES_USER": "app", "POSTGRES_PASSWORD": "app", "POSTGRES_DB": "app"},
        },
        # libpq env vars psycopg2 picks up automatically -> point it at the sidecar.
        "env": {"PGHOST": "db", "PGUSER": "app", "PGPASSWORD": "app", "PGDATABASE": "app"},
        "setup_commands": ["python seed.py"],
    }))
    (root / "seed.py").write_text(
        "import psycopg2\n"
        "conn = psycopg2.connect(); conn.autocommit = True\n"
        "cur = conn.cursor()\n"
        "cur.execute('CREATE TABLE IF NOT EXISTS widgets (id serial primary key, name text)')\n"
        "cur.execute(\"INSERT INTO widgets (name) VALUES ('reference-widget')\")\n"
        "print('seeded')\n"
    )


async def test_db_backed_tests_run_and_internet_stays_blocked(tmp_path):
    _write_project(tmp_path)
    (tmp_path / "test_db.py").write_text(
        "import socket\n"
        "import psycopg2\n"
        "import pytest\n"
        "\n"
        "def test_seeded_reference_row_is_present():\n"
        "    conn = psycopg2.connect()\n"
        "    cur = conn.cursor()\n"
        "    cur.execute(\"SELECT count(*) FROM widgets WHERE name = 'reference-widget'\")\n"
        "    assert cur.fetchone()[0] >= 1\n"
        "\n"
        "def test_outbound_internet_is_blocked():\n"
        "    # --internal network: DB reachable, internet NOT.\n"
        "    with pytest.raises(OSError):\n"
        "        socket.create_connection(('8.8.8.8', 53), timeout=3)\n"
    )

    runner = EphemeralSandboxRunner(timeout_seconds=120.0, build_timeout_seconds=600.0)
    result = await runner.run_validation(tmp_path, "python")

    assert result.success is True, f"expected pass, got:\n{result.output}"
    assert result.exit_code == 0


async def test_node_setup_command_local_file_write_visible_with_db_backing(tmp_path):
    """Same "setup_commands writes a local file the test then reads" proof as
    test_sandbox_node_copy.py, but through the DB-backed flow
    (_run_with_database) specifically — that copy-isolation branch has its
    own combined-command code path, separate from _run_isolated's, and
    wasn't covered by any existing test."""
    (tmp_path / "package.json").write_text(json.dumps({
        "name": "nodedbtest", "version": "1.0.0",
        "scripts": {"test": "vitest run"},
        "devDependencies": {"vitest": "^2.1.0"},
    }))
    (tmp_path / "pnpm-lock.yaml").write_text("")
    (tmp_path / "consumer.test.ts").write_text(
        "import { test, expect } from 'vitest';\n"
        "import { generated } from './generated';\n"
        "test('sees the setup-generated file with DB backing too', () => { "
        "expect(generated).toBe(42); });\n"
    )
    ai_os_dir = tmp_path / ".ai-os"
    ai_os_dir.mkdir()
    (ai_os_dir / "sandbox.json").write_text(json.dumps({
        "database": {
            "image": "postgres:16", "hostname": "db",
            "env": {"POSTGRES_USER": "app", "POSTGRES_PASSWORD": "app", "POSTGRES_DB": "app"},
        },
        "env": {"PGHOST": "db", "PGUSER": "app", "PGPASSWORD": "app", "PGDATABASE": "app"},
        "setup_commands": ["echo 'export const generated = 42;' > generated.ts"],
    }))

    runner = EphemeralSandboxRunner(timeout_seconds=180.0, build_timeout_seconds=600.0)
    result = await runner.run_validation(tmp_path, "typescript")
    assert result.success is True, f"expected pass, got:\n{result.output}"


async def test_failing_seed_is_reported_as_setup_failure(tmp_path):
    _write_project(tmp_path)
    # Break the seed so it errors out -> validation must report a setup failure,
    # not run the tests and not crash.
    (tmp_path / "seed.py").write_text("import psycopg2\nraise SystemExit('seed boom')\n")
    (tmp_path / "test_db.py").write_text("def test_never_runs():\n    assert True\n")

    runner = EphemeralSandboxRunner(timeout_seconds=120.0, build_timeout_seconds=600.0)
    result = await runner.run_validation(tmp_path, "python")

    assert result.success is False
    assert "setup/seed failed" in result.output.lower()
