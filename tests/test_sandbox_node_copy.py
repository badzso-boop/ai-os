"""Opt-in real-Docker test for Node copy-isolation + pnpm.

Gated behind AI_OS_TEST_NODE_SANDBOX=1 (and Docker): pulls node:22-alpine and
runs a real `pnpm install` + Vitest. This is the proof that copy-isolation fixes
the exact thing the out-of-tree NODE_PATH approach couldn't — a bundler-based
runner (Vitest) resolving node_modules — and that pnpm is detected/used.

Run it deliberately:  AI_OS_TEST_NODE_SANDBOX=1 .venv/bin/pytest tests/test_sandbox_node_copy.py -q
"""
from __future__ import annotations

import json
import os
import shutil

import pytest

from ai_os.sandbox.container_runner import EphemeralSandboxRunner

pytestmark = pytest.mark.skipif(
    os.environ.get("AI_OS_TEST_NODE_SANDBOX") != "1" or shutil.which("docker") is None,
    reason="opt-in only (AI_OS_TEST_NODE_SANDBOX=1 with docker; pulls node:22-alpine + Vitest)",
)


def _vitest_project(root, *, package_manager: str):
    (root / "package.json").write_text(json.dumps({
        "name": "copytest",
        "version": "1.0.0",
        "scripts": {"test": "vitest run"},
        "devDependencies": {"vitest": "^2.1.0"},
    }))
    if package_manager == "pnpm":
        # An empty pnpm-lock.yaml triggers pnpm detection; `--frozen-lockfile`
        # then fails on it and falls back to a plain `pnpm install`.
        (root / "pnpm-lock.yaml").write_text("")
    (root / "math.ts").write_text("export const add = (a: number, b: number) => a + b;\n")
    (root / "math.test.ts").write_text(
        "import { test, expect } from 'vitest';\n"
        "import { add } from './math';\n"
        "test('adds', () => { expect(add(2, 3)).toBe(5); });\n"
    )


async def test_pnpm_vitest_passes_under_copy_isolation(tmp_path):
    _vitest_project(tmp_path, package_manager="pnpm")
    runner = EphemeralSandboxRunner(timeout_seconds=180.0, build_timeout_seconds=600.0)
    result = await runner.run_validation(tmp_path, "typescript")
    assert result.success is True, f"expected pass, got:\n{result.output}"
    assert result.exit_code == 0


async def test_npm_vitest_passes_under_copy_isolation(tmp_path):
    _vitest_project(tmp_path, package_manager="npm")
    runner = EphemeralSandboxRunner(timeout_seconds=180.0, build_timeout_seconds=600.0)
    result = await runner.run_validation(tmp_path, "typescript")
    assert result.success is True, f"expected pass, got:\n{result.output}"
    assert result.exit_code == 0


async def test_failing_vitest_reports_failure(tmp_path):
    _vitest_project(tmp_path, package_manager="npm")
    (tmp_path / "math.test.ts").write_text(
        "import { test, expect } from 'vitest';\n"
        "import { add } from './math';\n"
        "test('wrong', () => { expect(add(2, 3)).toBe(999); });\n"
    )
    runner = EphemeralSandboxRunner(timeout_seconds=180.0, build_timeout_seconds=600.0)
    result = await runner.run_validation(tmp_path, "typescript")
    assert result.success is False


async def test_setup_command_local_file_write_is_visible_to_test_command(tmp_path):
    """A `.ai-os/sandbox.json` `setup_commands` step that writes a local file
    (the Prisma-generate-into-node_modules pattern) must be visible to the
    test command that runs afterward - proving they share ONE container's
    filesystem under copy-isolation, not two separate `--rm` containers
    where the setup step's writes would be silently discarded."""
    (tmp_path / "package.json").write_text(json.dumps({
        "name": "setuptest",
        "version": "1.0.0",
        "scripts": {"test": "vitest run"},
        "devDependencies": {"vitest": "^2.1.0"},
    }))
    (tmp_path / "pnpm-lock.yaml").write_text("")
    (tmp_path / "consumer.test.ts").write_text(
        "import { test, expect } from 'vitest';\n"
        "import { generated } from './generated';\n"
        "test('sees the setup-generated file', () => { expect(generated).toBe(42); });\n"
    )
    ai_os_dir = tmp_path / ".ai-os"
    ai_os_dir.mkdir()
    (ai_os_dir / "sandbox.json").write_text(json.dumps({
        "setup_commands": ["echo 'export const generated = 42;' > generated.ts"],
    }))

    runner = EphemeralSandboxRunner(timeout_seconds=180.0, build_timeout_seconds=600.0)
    result = await runner.run_validation(tmp_path, "typescript")
    assert result.success is True, f"expected pass, got:\n{result.output}"


async def test_setup_command_failure_still_reported_as_setup_failure(tmp_path):
    """The setup-vs-test failure distinction must survive the combined
    single-container run (via the echoed marker), not just plain success."""
    _vitest_project(tmp_path, package_manager="npm")
    ai_os_dir = tmp_path / ".ai-os"
    ai_os_dir.mkdir()
    (ai_os_dir / "sandbox.json").write_text(json.dumps({
        "setup_commands": ["exit 1"],
    }))

    runner = EphemeralSandboxRunner(timeout_seconds=180.0, build_timeout_seconds=600.0)
    result = await runner.run_validation(tmp_path, "typescript")
    assert result.success is False
    assert "setup/seed failed" in result.output.lower()
