"""Tests for Onboarding Engine and .ai-os Config Synthesizer."""

from __future__ import annotations

import json
from pathlib import Path
import pytest

from ai_os.core.onboarding import scan_and_generate_configs, ConfigDict


def test_scan_and_generate_configs_nonexistent_dir(tmp_path: Path) -> None:
    """Test that scan_and_generate_configs raises FileNotFoundError for non-existent path."""
    non_existent = tmp_path / "does_not_exist"
    with pytest.raises(FileNotFoundError):
        scan_and_generate_configs(non_existent)


def test_scan_and_generate_configs_missing_docs_no_deep_scan(tmp_path: Path) -> None:
    """Test that projects without docs or manifests return missing_docs status when use_deep_scan is False."""
    empty_proj = tmp_path / "empty_proj"
    empty_proj.mkdir()
    
    # Add a plain python file, but no documentation or manifest
    (empty_proj / "app.py").write_text("print('hello')", encoding="utf-8")

    result = scan_and_generate_configs(empty_proj, use_deep_scan=False)
    assert isinstance(result, dict)
    assert result["status"] == "missing_docs"
    assert result["config_dir"] is None
    assert not (empty_proj / ".ai-os").exists()


def test_scan_and_generate_configs_missing_docs_with_deep_scan(tmp_path: Path) -> None:
    """Test that projects without docs or manifests generate configs when use_deep_scan is True."""
    code_proj = tmp_path / "code_proj"
    code_proj.mkdir()

    (code_proj / "main.py").write_text("""
def compute_metrics(data):
    return len(data)

class AnalyticsEngine:
    pass
""", encoding="utf-8")

    result = scan_and_generate_configs(code_proj, use_deep_scan=True)
    assert isinstance(result, dict)
    assert result["status"] == "success"
    assert result["config_dir"] == code_proj / ".ai-os"
    assert (code_proj / ".ai-os").is_dir()

    inst_data = json.loads((code_proj / ".ai-os" / "instructions.json").read_text(encoding="utf-8"))
    assert "main.py:compute_metrics" in inst_data["code_stubs"]
    assert "main.py:AnalyticsEngine" in inst_data["code_stubs"]


def test_scan_and_generate_configs_claude_md_and_contributing(tmp_path: Path) -> None:
    """Test scanning documentation from CLAUDE.md, CONTRIBUTING.md, and docs directory."""
    proj_dir = tmp_path / "doc_proj"
    proj_dir.mkdir()

    claude_md = proj_dir / "CLAUDE.md"
    claude_md.write_text("# AI Assistant Guide\nProject guidelines for assistant.\n", encoding="utf-8")

    contrib_md = proj_dir / "CONTRIBUTING.md"
    contrib_md.write_text("# Contributing Guide\nHow to contribute.\n", encoding="utf-8")

    docs_dir = proj_dir / "docs"
    docs_dir.mkdir()
    (docs_dir / "architecture.md").write_text("# Architecture\nDetails here.\n", encoding="utf-8")

    result = scan_and_generate_configs(proj_dir, use_deep_scan=False)
    assert result["status"] == "success"
    assert result["config_dir"] == proj_dir / ".ai-os"

    conv_text = (proj_dir / ".ai-os" / "conventions.md").read_text(encoding="utf-8")
    assert "# Project Conventions: AI Assistant Guide" in conv_text


def test_scan_and_generate_configs_light_scan(tmp_path: Path) -> None:
    """Test light scan onboarding on a simple project directory with README."""
    proj_dir = tmp_path / "sample_proj"
    proj_dir.mkdir()

    readme = proj_dir / "README.md"
    readme.write_text("# Sample Project\nThis is a sample project for testing onboarding.\n", encoding="utf-8")

    reqs = proj_dir / "requirements.txt"
    reqs.write_text("fastapi>=0.100.0\npytest>=8.0.0\n", encoding="utf-8")

    config_dir = scan_and_generate_configs(proj_dir, use_deep_scan=False, model="test-model")

    assert isinstance(config_dir, dict)
    assert config_dir["status"] == "success"
    assert config_dir["config_dir"] == proj_dir / ".ai-os"
    assert config_dir == proj_dir / ".ai-os"
    assert config_dir.is_dir()

    # Check generated files exist
    instructions_file = config_dir / "instructions.json"
    conventions_file = config_dir / "conventions.md"
    sandbox_file = config_dir / "sandbox.json"
    ui_file = config_dir / "ui.json"

    assert instructions_file.is_file()
    assert conventions_file.is_file()
    assert sandbox_file.is_file()
    assert ui_file.is_file()

    # Verify instructions.json content
    inst_data = json.loads(instructions_file.read_text(encoding="utf-8"))
    assert inst_data["project_name"] == "Sample Project"
    assert "sample project" in inst_data["description"].lower()
    assert inst_data["language"] == "python"
    assert inst_data["framework"] == "fastapi"
    assert inst_data["deep_scan"] is False
    assert inst_data["model"] == "test-model"

    # Verify conventions.md content
    conv_text = conventions_file.read_text(encoding="utf-8")
    assert "# Project Conventions: Sample Project" in conv_text
    assert "fastapi" in conv_text

    # Verify sandbox.json content
    sandbox_data = json.loads(sandbox_file.read_text(encoding="utf-8"))
    assert sandbox_data["environment"] == "python"
    assert sandbox_data["setup_commands"] == ["pip install -r requirements.txt"]
    assert sandbox_data["test_command"] == "python -m pytest"

    # Verify ui.json content
    ui_data = json.loads(ui_file.read_text(encoding="utf-8"))
    assert isinstance(ui_data["has_ui"], bool)


def test_scan_and_generate_configs_deep_scan(tmp_path: Path) -> None:
    """Test deep scan onboarding extracting code stubs, UI entry points, routes, and components."""
    proj_dir = tmp_path / "deep_proj"
    proj_dir.mkdir()

    pkg_json = proj_dir / "package.json"
    pkg_json.write_text(
        json.dumps({
            "name": "deep-web-app",
            "description": "A complex React web application.",
            "scripts": {"build": "vite build", "test": "vitest"},
            "dependencies": {"react": "^18.0.0", "next": "^14.0.0"}
        }),
        encoding="utf-8"
    )

    index_html = proj_dir / "index.html"
    index_html.write_text("<!DOCTYPE html><html><head><title>App</title></head><body><div id='root'></div></body></html>", encoding="utf-8")

    app_py = proj_dir / "app.py"
    app_py.write_text("""
def calculate_score(items):
    return sum(items)

class UserProfile:
    pass

@app.get("/api/v1/users")
def get_users():
    return []
""", encoding="utf-8")

    src_dir = proj_dir / "src"
    src_dir.mkdir()
    component_file = src_dir / "Header.tsx"
    component_file.write_text("export function HeaderComponent() { return <header>Header</header>; }", encoding="utf-8")

    config_dir = scan_and_generate_configs(proj_dir, use_deep_scan=True)

    assert config_dir == proj_dir / ".ai-os"

    inst_data = json.loads((config_dir / "instructions.json").read_text(encoding="utf-8"))
    assert inst_data["project_name"] == "deep-web-app"
    assert inst_data["deep_scan"] is True
    assert "app.py:calculate_score" in inst_data["code_stubs"]
    assert "app.py:UserProfile" in inst_data["code_stubs"]

    ui_data = json.loads((config_dir / "ui.json").read_text(encoding="utf-8"))
    assert ui_data["has_ui"] is True
    assert ui_data["framework"] == "nextjs"
    assert "index.html" in ui_data["entry_points"]
    assert "/api/v1/users" in ui_data["routes"]
    assert "HeaderComponent" in ui_data["components"]

    sandbox_data = json.loads((config_dir / "sandbox.json").read_text(encoding="utf-8"))
    assert sandbox_data["setup_commands"] == ["npm install"]
    assert sandbox_data["build_command"] == "npm run build"