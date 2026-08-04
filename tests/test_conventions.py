"""Tests for repo-side `.ai-os/conventions.md` injection into planner + prompts."""
from __future__ import annotations

from ai_os.core.conventions import conventions_block, load_project_conventions
from ai_os.core.epic_planner import build_planning_prompt
from ai_os.sandbox.sandbox_config import parse_sandbox_config


def test_absent_conventions_is_empty(tmp_path):
    assert load_project_conventions(tmp_path) == ""


def test_loads_conventions_file(tmp_path):
    (tmp_path / ".ai-os").mkdir()
    (tmp_path / ".ai-os" / "conventions.md").write_text("- use i18n for all strings\n")
    assert "use i18n" in load_project_conventions(tmp_path)


def test_conventions_block_empty_when_no_conventions():
    assert conventions_block("") == ""
    assert conventions_block("   ") == ""


def test_conventions_block_labels_the_section():
    block = conventions_block("- use i18n")
    assert "Project conventions (MUST follow)" in block
    assert "- use i18n" in block


def test_planning_prompt_includes_conventions():
    prompt = build_planning_prompt("add a page", "repo summary", conventions="- use i18n and translate")
    assert "Project conventions" in prompt
    assert "use i18n and translate" in prompt


def test_planning_prompt_omits_conventions_when_empty():
    prompt = build_planning_prompt("add a page", "repo summary")
    assert "Project conventions" not in prompt


def test_sandbox_config_image_override(tmp_path):
    config = parse_sandbox_config({"image": "mcr.microsoft.com/playwright:v1.50.0-jammy"})
    assert config.image == "mcr.microsoft.com/playwright:v1.50.0-jammy"
