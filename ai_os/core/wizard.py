"""Interactive Post-Install Diagnostic Wizard for AI-OS.

This module provides the `run_wizard` function and associated check functions
to verify system environment requirements (Python >= 3.13, Docker daemon,
Git CLI, gh CLI), provider credentials (AGY, Claude, general API keys), and
the presence of the sandbox Docker image (`ai-os-sandbox-python:3.12`).

Architecture & Design Principles:
- Pure & Deterministic: System checks are wrapped in pure functions accepting
  injectable dependencies (version tuples, environment dictionaries, command
  runners, and file existence checkers) to allow 100% deterministic unit testing
  without external network access or starting actual Docker containers.
- Rich Output Formatting: Diagnostic findings are rendered into interactive
  terminal output using Rich `Panel` and `Table` components for enhanced UX.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path
import subprocess
import sys
from typing import Callable, Sequence

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text


@dataclass
class CheckResult:
    """Represents the outcome of an individual wizard setup check.

    Attributes:
        name: Short display title of the component or resource being checked.
        status: True if the check passed, False otherwise.
        details: Human-readable description or diagnostic detail.
        category: Check category ('environment', 'credentials', or 'sandbox').
    """

    name: str
    status: bool
    details: str
    category: str = "environment"


@dataclass
class WizardResult:
    """Summary result of a full wizard execution run.

    Attributes:
        checks: Sequence of individual CheckResult objects.
        all_passed: True if every check passed, False if any check failed.
        summary: Text summary of the check outcomes.
    """

    checks: list[CheckResult] = field(default_factory=list)
    all_passed: bool = True
    summary: str = ""


def _default_command_runner(cmd: list[str]) -> tuple[int, str]:
    """Execute a system command and return (exit_code, output_text)."""
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
        out = (proc.stdout or "").strip() or (proc.stderr or "").strip()
        return proc.returncode, out
    except (FileNotFoundError, subprocess.TimeoutExpired, PermissionError) as exc:
        return 1, str(exc)


def _default_file_exists(path: Path) -> bool:
    """Check if a filepath exists on disk."""
    try:
        return path.exists()
    except Exception:
        return False


def check_python_version(
    sys_version_info: tuple[int, int, int] | None = None,
) -> CheckResult:
    """Check that Python version is >= 3.13."""
    v = sys_version_info or sys.version_info[:3]
    version_str = f"{v[0]}.{v[1]}.{v[2]}"
    if v >= (3, 13, 0):
        return CheckResult(
            name="Python Version",
            status=True,
            details=f"Python {version_str} (>= 3.13)",
            category="environment",
        )
    return CheckResult(
        name="Python Version",
        status=False,
        details=f"Python {version_str} (Required: >= 3.13)",
        category="environment",
    )


def check_docker_daemon(
    command_runner: Callable[[list[str]], tuple[int, str]] | None = None,
) -> CheckResult:
    """Check if Docker CLI is present and the Docker daemon is responsive."""
    runner = command_runner or _default_command_runner
    code, out = runner(["docker", "info"])
    if code == 0:
        return CheckResult(
            name="Docker Daemon",
            status=True,
            details="Docker daemon is running and responsive",
            category="environment",
        )
    return CheckResult(
        name="Docker Daemon",
        status=False,
        details="Docker daemon is not running or docker CLI is missing",
        category="environment",
    )


def check_git_cli(
    command_runner: Callable[[list[str]], tuple[int, str]] | None = None,
) -> CheckResult:
    """Check if Git CLI is installed and available in PATH."""
    runner = command_runner or _default_command_runner
    code, out = runner(["git", "--version"])
    if code == 0:
        first_line = out.splitlines()[0] if out else "git installed"
        return CheckResult(
            name="Git CLI",
            status=True,
            details=first_line,
            category="environment",
        )
    return CheckResult(
        name="Git CLI",
        status=False,
        details="Git CLI not found in PATH",
        category="environment",
    )


def check_gh_cli(
    command_runner: Callable[[list[str]], tuple[int, str]] | None = None,
) -> CheckResult:
    """Check if GitHub CLI (gh) is installed and available in PATH."""
    runner = command_runner or _default_command_runner
    code, out = runner(["gh", "--version"])
    if code == 0:
        first_line = out.splitlines()[0] if out else "gh installed"
        return CheckResult(
            name="GitHub CLI (gh)",
            status=True,
            details=first_line,
            category="environment",
        )
    return CheckResult(
        name="GitHub CLI (gh)",
        status=False,
        details="GitHub CLI (gh) not found in PATH",
        category="environment",
    )


def check_agy_credentials(
    env: dict[str, str] | None = None,
    file_exists: Callable[[Path], bool] | None = None,
) -> CheckResult:
    """Check for AGY provider credentials in environment variables or config files."""
    environ = env if env is not None else dict(os.environ)
    exists = file_exists or _default_file_exists

    env_keys = ["AGY_API_KEY", "AGY_TOKEN", "AGY_CREDENTIALS"]
    for key in env_keys:
        if environ.get(key):
            return CheckResult(
                name="AGY Credentials",
                status=True,
                details=f"Found via environment variable '{key}'",
                category="credentials",
            )

    home = Path.home()
    config_paths = [
        home / ".config" / "agy" / "config.json",
        home / ".agy" / "credentials",
    ]
    for cfg in config_paths:
        if exists(cfg):
            return CheckResult(
                name="AGY Credentials",
                status=True,
                details=f"Found config file at '{cfg}'",
                category="credentials",
            )

    return CheckResult(
        name="AGY Credentials",
        status=False,
        details="Missing AGY credentials (set AGY_API_KEY)",
        category="credentials",
    )


def check_claude_credentials(
    env: dict[str, str] | None = None,
    file_exists: Callable[[Path], bool] | None = None,
) -> CheckResult:
    """Check for Claude / Anthropic credentials in environment variables or config files."""
    environ = env if env is not None else dict(os.environ)
    exists = file_exists or _default_file_exists

    env_keys = ["ANTHROPIC_API_KEY", "CLAUDE_API_KEY"]
    for key in env_keys:
        if environ.get(key):
            return CheckResult(
                name="Claude Credentials",
                status=True,
                details=f"Found via environment variable '{key}'",
                category="credentials",
            )

    home = Path.home()
    config_paths = [
        home / ".config" / "claude" / "config.json",
        home / ".anthropic" / "credentials",
    ]
    for cfg in config_paths:
        if exists(cfg):
            return CheckResult(
                name="Claude Credentials",
                status=True,
                details=f"Found config file at '{cfg}'",
                category="credentials",
            )

    return CheckResult(
        name="Claude Credentials",
        status=False,
        details="Missing Claude credentials (set ANTHROPIC_API_KEY or CLAUDE_API_KEY)",
        category="credentials",
    )


def check_api_keys(env: dict[str, str] | None = None) -> CheckResult:
    """Check for general LLM provider API keys in environment variables."""
    environ = env if env is not None else dict(os.environ)
    key_names = [
        "OPENAI_API_KEY",
        "GEMINI_API_KEY",
        "DEEPSEEK_API_KEY",
        "MISTRAL_API_KEY",
        "LLM_API_KEY",
    ]
    found = [k for k in key_names if environ.get(k)]

    if found:
        return CheckResult(
            name="General API Keys",
            status=True,
            details=f"Configured keys: {', '.join(found)}",
            category="credentials",
        )
    return CheckResult(
        name="General API Keys",
        status=False,
        details="No general API keys found (e.g. OPENAI_API_KEY, GEMINI_API_KEY)",
        category="credentials",
    )


def check_sandbox_image(
    command_runner: Callable[[list[str]], tuple[int, str]] | None = None,
    image_name: str = "ai-os-sandbox-python:3.12",
) -> CheckResult:
    """Check if the target sandbox Docker image is present locally."""
    runner = command_runner or _default_command_runner
    code, _ = runner(["docker", "image", "inspect", image_name])
    if code == 0:
        return CheckResult(
            name="Sandbox Image",
            status=True,
            details=f"Docker image '{image_name}' is available locally",
            category="sandbox",
        )
    return CheckResult(
        name="Sandbox Image",
        status=False,
        details=f"Docker image '{image_name}' not found locally",
        category="sandbox",
    )


def format_wizard_output(results: WizardResult, console: Console | None = None) -> None:
    """Render the wizard results using Rich Panels and Tables.

    Args:
        results: The WizardResult object containing check outcomes.
        console: Optional Rich Console instance. Defaults to a new Console.
    """
    con = console or Console()

    # Header Panel
    title = Text("AI-OS Interactive Post-Install Setup Wizard", style="bold cyan")
    subtitle = "System Environment, Provider Credentials & Sandbox Image Verification"
    con.print(Panel(f"{title}\n{subtitle}", border_style="cyan", expand=False))

    # Tables by category
    categories = [
        ("environment", "Environment Checks"),
        ("credentials", "Provider Credentials Checks"),
        ("sandbox", "Sandbox Image Checks"),
    ]

    for cat_key, cat_title in categories:
        cat_checks = [c for c in results.checks if c.category == cat_key]
        if not cat_checks:
            continue

        table = Table(title=cat_title, show_header=True, header_style="bold magenta")
        table.add_column("Component", style="cyan", min_width=20)
        table.add_column("Status", min_width=12)
        table.add_column("Details", style="white")

        for c in cat_checks:
            status_text = "[bold green]PASS[/bold green]" if c.status else "[bold red]FAIL[/bold red]"
            table.add_row(c.name, status_text, c.details)

        con.print(table)

    # Summary Panel
    if results.all_passed:
        summary_panel = Panel(
            "[bold green]✓ All post-install checks passed successfully! AI-OS is ready.[/bold green]",
            title="Wizard Summary",
            border_style="green",
        )
    else:
        summary_panel = Panel(
            "[bold red]✗ Some checks failed or require attention. Please review details above.[/bold red]",
            title="Wizard Summary",
            border_style="red",
        )
    con.print(summary_panel)


def run_wizard(
    *,
    console: Console | None = None,
    sys_version_info: tuple[int, int, int] | None = None,
    env: dict[str, str] | None = None,
    command_runner: Callable[[list[str]], tuple[int, str]] | None = None,
    file_exists: Callable[[Path], bool] | None = None,
    show_output: bool = True,
) -> WizardResult:
    """Run the complete post-install diagnostic wizard.

    Args:
        console: Optional Rich Console for formatting output.
        sys_version_info: Optional tuple override for Python version check.
        env: Optional environment dictionary override.
        command_runner: Optional command runner function override.
        file_exists: Optional file existence check function override.
        show_output: If True, renders Rich formatted panels and tables.

    Returns:
        WizardResult containing all individual checks and overall status.
    """
    checks: list[CheckResult] = []

    # 1. Environment Checks
    checks.append(check_python_version(sys_version_info))
    checks.append(check_docker_daemon(command_runner))
    checks.append(check_git_cli(command_runner))
    checks.append(check_gh_cli(command_runner))

    # 2. Provider Credentials Checks
    checks.append(check_agy_credentials(env, file_exists))
    checks.append(check_claude_credentials(env, file_exists))
    checks.append(check_api_keys(env))

    # 3. Sandbox Docker Image Check
    checks.append(check_sandbox_image(command_runner))

    all_passed = all(c.status for c in checks)
    summary_text = (
        "All checks passed successfully."
        if all_passed
        else f"{sum(1 for c in checks if not c.status)} of {len(checks)} check(s) failed."
    )

    result = WizardResult(checks=checks, all_passed=all_passed, summary=summary_text)

    if show_output:
        format_wizard_output(result, console)

    return result