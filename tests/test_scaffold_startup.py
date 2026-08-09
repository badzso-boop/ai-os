"""Tests for the 'startup' scaffolding preset."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from ai_os.core.scaffold import scaffold_files, write_scaffold


def test_scaffold_files_startup_returns_expected_dictionary():
    files = scaffold_files("startup")
    expected_keys = {
        "index.html",
        "styles/reset.css",
        "styles/tokens.css",
        "styles/layout.css",
        "sim/sim.js",
        "sim/seed.js",
        "app.js",
        ".ai-os/sandbox.json",
    }
    assert expected_keys.issubset(set(files.keys()))
    sandbox_cfg = json.loads(files[".ai-os/sandbox.json"])
    assert isinstance(sandbox_cfg, dict)


def test_write_scaffold_startup_creates_all_expected_files(tmp_path: Path):
    target_dir = tmp_path / "startup_proj"
    written_files = write_scaffold(target_dir, "startup")

    expected_files = [
        "index.html",
        "styles/reset.css",
        "styles/tokens.css",
        "styles/layout.css",
        "sim/sim.js",
        "sim/seed.js",
        "app.js",
        ".ai-os/sandbox.json",
    ]

    for relpath in expected_files:
        assert relpath in written_files
        file_path = target_dir / relpath
        assert file_path.is_file()
        assert file_path.stat().st_size > 0

    index_content = (target_dir / "index.html").read_text(encoding="utf-8")
    assert "styles/reset.css" in index_content
    assert "styles/tokens.css" in index_content
    assert "styles/layout.css" in index_content
    assert "sim/sim.js" in index_content
    assert "sim/seed.js" in index_content
    assert "app.js" in index_content