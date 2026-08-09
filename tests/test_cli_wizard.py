"""CLI Unit tests for ai-os wizard, project add, and event printer.

This module validates:
- `ai-os wizard` command execution calling `run_wizard()`.
- `ai-os project add` command invoking `scan_and_generate_configs()` with interactive prompt.
- Event printer formatting execution events into Rich Panels with elapsed timer and cost tracking.
"""

from __future__ import annotations

import json
from pathlib import Path
from click.testing import CliRunner
import pytest
from rich.console import Console

from ai_os.cli import main, _make_event_printer, printer
from ai_os.core.wizard import WizardResult, CheckResult


def test_cli_wizard_command_runs_wizard(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test that ai-os wizard CLI command invokes run_wizard successfully."""
    wizard_called = False

    def mock_run_wizard(**kwargs):
        nonlocal wizard_called
        wizard_called = True
        return WizardResult(
            checks=[
                CheckResult(name="Python Version", status=True, details="Python 3.13", category="environment")
            ],
            all_passed=True,
            summary="All checks passed.",
        )

    monkeypatch.setattr("ai_os.cli.run_wizard", mock_run_wizard)

    runner = CliRunner()
    result = runner.invoke(main, ["wizard"])

    assert result.exit_code == 0
    assert wizard_called is True


def test_cli_wizard_command_real_execution() -> None:
    """Test ai-os wizard command without monkeypatching run_wizard."""
    runner = CliRunner()
    result = runner.invoke(main, ["wizard"])

    assert result.exit_code == 0
    assert "AI-OS Interactive Post-Install Setup Wizard" in result.output
    assert "Environment Checks" in result.output


def test_cli_project_add_interactive_prompt_deep_scan(tmp_path: Path) -> None:
    """Test ai-os project add with interactive prompt accepting deep scan (input='y')."""
    proj_dir = tmp_path / "interactive_proj"
    proj_dir.mkdir()
    (proj_dir / "main.py").write_text("def run_app(): pass\n", encoding="utf-8")

    runner = CliRunner()
    result = runner.invoke(main, ["project", "add", "my_app", str(proj_dir)], input="y\n")

    assert result.exit_code == 0
    assert "Perform deep scan of codebase?" in result.output
    assert f"Project 'my_app' added at {proj_dir.resolve()}." in result.output
    assert (proj_dir / ".ai-os" / "instructions.json").exists()

    inst_data = json.loads((proj_dir / ".ai-os" / "instructions.json").read_text(encoding="utf-8"))
    assert inst_data["deep_scan"] is True
    assert "main.py:run_app" in inst_data["code_stubs"]


def test_cli_project_add_interactive_prompt_no_deep_scan(tmp_path: Path) -> None:
    """Test ai-os project add with interactive prompt declining deep scan (input='n')."""
    proj_dir = tmp_path / "light_proj"
    proj_dir.mkdir()
    (proj_dir / "README.md").write_text("# Light Proj\nSimple description.\n", encoding="utf-8")

    runner = CliRunner()
    result = runner.invoke(main, ["project", "add", "light_app", str(proj_dir)], input="n\n")

    assert result.exit_code == 0
    assert "Perform deep scan of codebase?" in result.output
    assert (proj_dir / ".ai-os" / "instructions.json").exists()

    inst_data = json.loads((proj_dir / ".ai-os" / "instructions.json").read_text(encoding="utf-8"))
    assert inst_data["deep_scan"] is False


def test_cli_project_add_flag_override(tmp_path: Path) -> None:
    """Test ai-os project add with --deep-scan flag skipping interactive prompt."""
    proj_dir = tmp_path / "flag_proj"
    proj_dir.mkdir()
    (proj_dir / "app.py").write_text("class CoreApp: pass\n", encoding="utf-8")

    runner = CliRunner()
    result = runner.invoke(main, ["project", "add", "flag_app", str(proj_dir), "--deep-scan"])

    assert result.exit_code == 0
    assert (proj_dir / ".ai-os" / "instructions.json").exists()

    inst_data = json.loads((proj_dir / ".ai-os" / "instructions.json").read_text(encoding="utf-8"))
    assert inst_data["deep_scan"] is True
    assert "app.py:CoreApp" in inst_data["code_stubs"]


def test_make_event_printer_rich_panel_formatting() -> None:
    """Test _make_event_printer formats live execution events into Rich Panels."""
    console = Console(record=True, width=100)
    event_printer = _make_event_printer(verbose=True, console=console)

    event_data = {
        "type": "task_execution",
        "task_id": "TASK-42",
        "status": "SUCCESS",
        "message": "Task processed successfully",
        "cost": 0.0025,
        "elapsed": 1.45,
        "extra_info": "test-val",
    }

    event_printer(event_data)

    output = console.export_text()
    assert "Task [TASK-42] - TASK_EXECUTION" in output
    assert "SUCCESS" in output
    assert "Task processed successfully" in output
    assert "1.45s" in output
    assert "$0.0025" in output
    assert "extra_info" in output


def test_printer_function_executes() -> None:
    """Test printer wrapper function formats events without raising errors."""
    printer({"type": "ping", "status": "OK"})