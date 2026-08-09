"""Unit tests for ai_os.core.wizard module.

Tests interactive post-install wizard environment checks, credential checks,
sandbox image verification, and Rich output formatting without external commands
or real Docker daemon dependency.
"""

from __future__ import annotations

from pathlib import Path
import pytest
from rich.console import Console

from ai_os.core.wizard import (
    CheckResult,
    WizardResult,
    check_api_keys,
    check_agy_credentials,
    check_claude_credentials,
    check_docker_daemon,
    check_gh_cli,
    check_git_cli,
    check_python_version,
    check_sandbox_image,
    format_wizard_output,
    run_wizard,
)


def test_check_python_version_pass() -> None:
    result = check_python_version((3, 13, 1))
    assert result.status is True
    assert result.category == "environment"
    assert "3.13.1" in result.details
    assert ">= 3.13" in result.details


def test_check_python_version_fail() -> None:
    result = check_python_version((3, 12, 4))
    assert result.status is False
    assert result.category == "environment"
    assert "Required: >= 3.13" in result.details


def test_check_docker_daemon_pass() -> None:
    def mock_runner(cmd: list[str]) -> tuple[int, str]:
        assert cmd == ["docker", "info"]
        return 0, "Server Version: 24.0.5"

    result = check_docker_daemon(mock_runner)
    assert result.status is True
    assert result.category == "environment"
    assert "running and responsive" in result.details


def test_check_docker_daemon_fail() -> None:
    def mock_runner(cmd: list[str]) -> tuple[int, str]:
        return 1, "Cannot connect to the Docker daemon"

    result = check_docker_daemon(mock_runner)
    assert result.status is False
    assert result.category == "environment"
    assert "not running" in result.details


def test_check_git_cli_pass() -> None:
    def mock_runner(cmd: list[str]) -> tuple[int, str]:
        assert cmd == ["git", "--version"]
        return 0, "git version 2.43.0"

    result = check_git_cli(mock_runner)
    assert result.status is True
    assert result.details == "git version 2.43.0"


def test_check_git_cli_fail() -> None:
    def mock_runner(cmd: list[str]) -> tuple[int, str]:
        return 1, "git: command not found"

    result = check_git_cli(mock_runner)
    assert result.status is False
    assert "not found" in result.details


def test_check_gh_cli_pass() -> None:
    def mock_runner(cmd: list[str]) -> tuple[int, str]:
        assert cmd == ["gh", "--version"]
        return 0, "gh version 2.40.0 (2023-12-01)"

    result = check_gh_cli(mock_runner)
    assert result.status is True
    assert "gh version 2.40.0" in result.details


def test_check_gh_cli_fail() -> None:
    def mock_runner(cmd: list[str]) -> tuple[int, str]:
        return 1, "gh: command not found"

    result = check_gh_cli(mock_runner)
    assert result.status is False
    assert "not found" in result.details


def test_check_agy_credentials_env() -> None:
    env = {"AGY_API_KEY": "sk-agy-test-123"}
    result = check_agy_credentials(env=env)
    assert result.status is True
    assert result.category == "credentials"
    assert "AGY_API_KEY" in result.details


def test_check_agy_credentials_file() -> None:
    env: dict[str, str] = {}

    def mock_exists(p: Path) -> bool:
        return "agy" in str(p)

    result = check_agy_credentials(env=env, file_exists=mock_exists)
    assert result.status is True
    assert result.category == "credentials"
    assert "Found config file" in result.details


def test_check_agy_credentials_missing() -> None:
    env: dict[str, str] = {}

    def mock_exists(p: Path) -> bool:
        return False

    result = check_agy_credentials(env=env, file_exists=mock_exists)
    assert result.status is False
    assert "Missing AGY credentials" in result.details


def test_check_claude_credentials_env() -> None:
    env = {"ANTHROPIC_API_KEY": "sk-ant-test-456"}
    result = check_claude_credentials(env=env)
    assert result.status is True
    assert result.category == "credentials"
    assert "ANTHROPIC_API_KEY" in result.details


def test_check_claude_credentials_missing() -> None:
    env: dict[str, str] = {}

    def mock_exists(p: Path) -> bool:
        return False

    result = check_claude_credentials(env=env, file_exists=mock_exists)
    assert result.status is False
    assert "Missing Claude credentials" in result.details


def test_check_api_keys_present() -> None:
    env = {"OPENAI_API_KEY": "sk-openai", "GEMINI_API_KEY": "ai-gemini"}
    result = check_api_keys(env=env)
    assert result.status is True
    assert "OPENAI_API_KEY" in result.details
    assert "GEMINI_API_KEY" in result.details


def test_check_api_keys_missing() -> None:
    env: dict[str, str] = {}
    result = check_api_keys(env=env)
    assert result.status is False
    assert "No general API keys found" in result.details


def test_check_sandbox_image_pass() -> None:
    def mock_runner(cmd: list[str]) -> tuple[int, str]:
        assert cmd == ["docker", "image", "inspect", "ai-os-sandbox-python:3.12"]
        return 0, "[{}]"

    result = check_sandbox_image(mock_runner, "ai-os-sandbox-python:3.12")
    assert result.status is True
    assert result.category == "sandbox"
    assert "available locally" in result.details


def test_check_sandbox_image_fail() -> None:
    def mock_runner(cmd: list[str]) -> tuple[int, str]:
        return 1, "Error: No such image"

    result = check_sandbox_image(mock_runner, "ai-os-sandbox-python:3.12")
    assert result.status is False
    assert result.category == "sandbox"
    assert "not found locally" in result.details


def test_run_wizard_all_pass_and_rich_render() -> None:
    def mock_runner(cmd: list[str]) -> tuple[int, str]:
        return 0, "mock ok"

    env = {
        "AGY_API_KEY": "test-key",
        "CLAUDE_API_KEY": "test-key",
        "OPENAI_API_KEY": "test-key",
    }
    console = Console(record=True, width=100)

    res = run_wizard(
        console=console,
        sys_version_info=(3, 13, 0),
        env=env,
        command_runner=mock_runner,
        show_output=True,
    )

    assert res.all_passed is True
    assert len(res.checks) == 8
    rendered = console.export_text()
    assert "AI-OS Interactive Post-Install Setup Wizard" in rendered
    assert "Environment Checks" in rendered
    assert "Provider Credentials Checks" in rendered
    assert "Sandbox Image Checks" in rendered
    assert "All post-install checks passed successfully!" in rendered


def test_run_wizard_partial_failure() -> None:
    def mock_runner(cmd: list[str]) -> tuple[int, str]:
        return 1, "error"

    env: dict[str, str] = {}
    console = Console(record=True, width=100)

    res = run_wizard(
        console=console,
        sys_version_info=(3, 12, 0),
        env=env,
        command_runner=mock_runner,
        show_output=True,
    )

    assert res.all_passed is False
    assert "8 of 8 check(s) failed." in res.summary or "check(s) failed" in res.summary
    rendered = console.export_text()
    assert "Some checks failed or require attention" in rendered