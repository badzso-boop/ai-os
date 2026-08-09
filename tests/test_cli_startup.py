"""CLI Unit tests for ai-os startup command."""

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