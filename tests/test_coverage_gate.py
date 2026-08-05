"""Tests for the coverage gate (Phase 6, feature 1b): parsing the `coverage`
block of `.ai-os/sandbox.json` and wrapping the pytest command with a fail-under
threshold. Deterministic — no Docker, no real coverage run."""
from __future__ import annotations

import json

import pytest

from ai_os.sandbox.container_runner import (
    SANDBOX_PROFILES,
    EphemeralSandboxRunner,
    build_coverage_command,
)
from ai_os.sandbox.sandbox_config import (
    CoverageConfig,
    SandboxConfigError,
    load_sandbox_config,
    parse_sandbox_config,
)


def test_parse_coverage_block():
    cfg = parse_sandbox_config({"coverage": {"min_percent": 80, "paths": ["src", "app"]}})
    assert cfg.coverage == CoverageConfig(min_percent=80.0, paths=("src", "app"))
    assert cfg.coverage.enabled is True


def test_parse_coverage_defaults_and_disabled():
    assert parse_sandbox_config({}).coverage is None
    cfg = parse_sandbox_config({"coverage": {"min_percent": 0}})
    assert cfg.coverage is not None and cfg.coverage.enabled is False


def test_parse_coverage_rejects_bad_types():
    with pytest.raises(SandboxConfigError):
        parse_sandbox_config({"coverage": {"min_percent": "lots"}})
    with pytest.raises(SandboxConfigError):
        parse_sandbox_config({"coverage": {"min_percent": 50, "paths": "src"}})
    with pytest.raises(SandboxConfigError):
        parse_sandbox_config({"coverage": [1, 2]})


def test_build_coverage_command_python_wraps_pytest():
    cov = CoverageConfig(min_percent=85, paths=("src",))
    cmd = build_coverage_command("python", "pytest -p no:cacheprovider", cov)
    assert cmd == (
        "pytest -p no:cacheprovider --cov=src --cov-report=term-missing --cov-fail-under=85"
    )


def test_build_coverage_command_defaults_to_whole_project():
    cov = CoverageConfig(min_percent=70)
    cmd = build_coverage_command("python", "pytest", cov)
    assert "--cov=." in cmd and "--cov-fail-under=70" in cmd


def test_build_coverage_command_skips_non_pytest_and_other_languages():
    cov = CoverageConfig(min_percent=80, paths=("src",))
    # Custom (non-pytest) python command: AI-OS won't guess how to instrument it.
    assert build_coverage_command("python", "python run_tests.py", cov) is None
    # Node/Java own their own coverage tooling.
    assert build_coverage_command("typescript", "npm test", cov) is None
    assert build_coverage_command("java", "mvn -o test", cov) is None
    # Disabled coverage -> no wrapping.
    assert build_coverage_command("python", "pytest", CoverageConfig(min_percent=0)) is None


def test_effective_command_applies_coverage(tmp_path):
    runner = EphemeralSandboxRunner()
    cfg = parse_sandbox_config({"coverage": {"min_percent": 90, "paths": ["pkg"]}})
    profile = SANDBOX_PROFILES["python"]
    # default_command None -> falls back to profile.command (pytest), then wrapped.
    cmd = runner._effective_command(profile, cfg, None, "python")
    assert cmd.startswith("pytest -p no:cacheprovider --cov=pkg")
    assert "--cov-fail-under=90" in cmd


def test_effective_command_no_coverage_is_passthrough():
    runner = EphemeralSandboxRunner()
    profile = SANDBOX_PROFILES["python"]
    # No config -> default_command (None here) returned unchanged.
    assert runner._effective_command(profile, None, None, "python") is None
    cfg = parse_sandbox_config({"test_command": "pytest -k fast"})
    assert runner._effective_command(profile, cfg, None, "python") == "pytest -k fast"


def test_load_coverage_from_file(tmp_path):
    ai_os_dir = tmp_path / ".ai-os"
    ai_os_dir.mkdir()
    (ai_os_dir / "sandbox.json").write_text(json.dumps({"coverage": {"min_percent": 75}}))
    cfg = load_sandbox_config(tmp_path)
    assert cfg is not None and cfg.coverage.min_percent == 75.0
