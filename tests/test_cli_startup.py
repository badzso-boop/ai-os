"""CLI Unit tests for ai-os startup command options and flags.

This module validates the command-line interface for `ai-os startup`, testing:
- Short flags (-p, -b, -o) and long options (--prompt, --brief, --out, --no-deploy).
- Inline brief strings vs brief file paths.
- Priority precedence between --brief and --prompt options.
- CLI output formatting, help text display, and default options.
- Generated scaffold directory structure and file contents.

Deterministic Execution:
- All CLI tests execute isolated via click.testing.CliRunner with no external subprocesses or network calls.
"""

from __future__ import annotations

from pathlib import Path
from click.testing import CliRunner
import pytest

from ai_os.cli import main, startup


def test_cli_startup_with_prompt(tmp_path: Path) -> None:
    """Test ai-os startup command with --prompt and --out options."""
    runner = CliRunner()
    out_dir = tmp_path / "startup_app"

    result = runner.invoke(
        main,
        ["startup", "--prompt", "TaskFlow - Automated workflow management", "--out", str(out_dir)],
    )

    assert result.exit_code == 0
    assert "Startup scaffold generated successfully" in result.output
    assert "TaskFlow" in result.output
    assert "Deployment: Ready" in result.output

    assert (out_dir / "index.html").exists()
    assert (out_dir / "styles" / "tokens.css").exists()
    assert (out_dir / "sim" / "seed.js").exists()

    html_content = (out_dir / "index.html").read_text(encoding="utf-8")
    assert "TaskFlow" in html_content


def test_cli_startup_short_flags_prompt_and_out(tmp_path: Path) -> None:
    """Test ai-os startup command using short flags -p and -o."""
    runner = CliRunner()
    out_dir = tmp_path / "short_flags_app"

    result = runner.invoke(
        main,
        ["startup", "-p", "QuickApp - Fast scaffold test", "-o", str(out_dir)],
    )

    assert result.exit_code == 0
    assert "Startup scaffold generated successfully" in result.output
    assert "QuickApp" in result.output
    assert (out_dir / "index.html").exists()

    html_content = (out_dir / "index.html").read_text(encoding="utf-8")
    assert "QuickApp" in html_content


def test_cli_startup_with_brief_file(tmp_path: Path) -> None:
    """Test ai-os startup command with --brief option pointing to a file."""
    brief_file = tmp_path / "brief.md"
    brief_file.write_text(
        "# FreshBox\n\n## Név + egymondatos value prop\nFreshBox — Helyi zöldség előfizetés\n\n## Oldalak\nLanding, Demo\n",
        encoding="utf-8",
    )
    out_dir = tmp_path / "freshbox_app"

    runner = CliRunner()
    result = runner.invoke(
        main,
        ["startup", "--brief", str(brief_file), "--out", str(out_dir)],
    )

    assert result.exit_code == 0
    assert "FreshBox" in result.output
    assert (out_dir / "index.html").exists()

    html_content = (out_dir / "index.html").read_text(encoding="utf-8")
    assert "FreshBox" in html_content


def test_cli_startup_short_flag_brief_and_out(tmp_path: Path) -> None:
    """Test ai-os startup command using short flags -b for brief file and -o for output directory."""
    brief_file = tmp_path / "short_brief.md"
    brief_file.write_text(
        "# ShortFlagApp\n\n## Név + egymondatos value prop\nShortFlagApp - Tested with short flag\n",
        encoding="utf-8",
    )
    out_dir = tmp_path / "short_brief_out"

    runner = CliRunner()
    result = runner.invoke(
        main,
        ["startup", "-b", str(brief_file), "-o", str(out_dir)],
    )

    assert result.exit_code == 0
    assert "ShortFlagApp" in result.output
    assert (out_dir / "index.html").exists()


def test_cli_startup_brief_inline_text(tmp_path: Path) -> None:
    """Test ai-os startup command with --brief option using raw text input."""
    runner = CliRunner()
    out_dir = tmp_path / "inline_brief_app"

    result = runner.invoke(
        main,
        ["startup", "--brief", "InlineApp - Direct brief prompt string", "--out", str(out_dir)],
    )

    assert result.exit_code == 0
    assert "InlineApp" in result.output
    assert (out_dir / "index.html").exists()


def test_cli_startup_brief_overrides_prompt(tmp_path: Path) -> None:
    """Test that --brief option takes precedence over --prompt option when both are provided."""
    runner = CliRunner()
    out_dir = tmp_path / "override_app"

    result = runner.invoke(
        main,
        [
            "startup",
            "--brief",
            "PriorityBrief - Takes precedence",
            "--prompt",
            "IgnoredPrompt - Secondary option",
            "--out",
            str(out_dir),
        ],
    )

    assert result.exit_code == 0
    assert "PriorityBrief" in result.output
    assert "IgnoredPrompt" not in result.output
    assert (out_dir / "index.html").exists()


def test_cli_startup_no_deploy_flag(tmp_path: Path) -> None:
    """Test ai-os startup command with --no-deploy flag."""
    runner = CliRunner()
    out_dir = tmp_path / "nodeploy_app"

    result = runner.invoke(
        main,
        ["startup", "--prompt", "NoDeployApp - Fast startup", "--out", str(out_dir), "--no-deploy"],
    )

    assert result.exit_code == 0
    assert "Deployment: Skipped (--no-deploy)" in result.output
    assert (out_dir / "index.html").exists()


def test_cli_startup_default_options(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Test ai-os startup command with default options in a temp working directory."""
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()

    result = runner.invoke(main, ["startup"])

    assert result.exit_code == 0
    assert "Untitled Startup" in result.output
    assert (tmp_path / "out" / "index.html").exists()


def test_cli_startup_help_flag() -> None:
    """Test ai-os startup command --help option outputs option descriptions."""
    runner = CliRunner()
    result = runner.invoke(main, ["startup", "--help"])

    assert result.exit_code == 0
    assert "--prompt" in result.output or "-p" in result.output
    assert "--brief" in result.output or "-b" in result.output
    assert "--out" in result.output or "-o" in result.output
    assert "--no-deploy" in result.output


def test_cli_startup_detailed_stdout_summary(tmp_path: Path) -> None:
    """Test that startup command prints all brief metadata sections to stdout."""
    runner = CliRunner()
    out_dir = tmp_path / "summary_app"

    result = runner.invoke(
        main,
        [
            "startup",
            "--prompt",
            "DetailedApp - Complete startup description with full metadata",
            "--out",
            str(out_dir),
        ],
    )

    assert result.exit_code == 0
    assert "Title:" in result.output
    assert "Value Proposition:" in result.output
    assert "Target Audience:" in result.output
    assert "Pages:" in result.output
    assert "Core Flow:" in result.output
    assert "Brand / Tone:" in result.output