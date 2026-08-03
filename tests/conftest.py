from pathlib import Path

import pytest

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "sample_project"


@pytest.fixture()
def fixture_root() -> Path:
    return FIXTURE_ROOT


@pytest.fixture()
def isolated_home(tmp_path, monkeypatch):
    """Points the project registry at a throwaway directory so tests never touch ~/.ai-os."""
    home = tmp_path / "ai-os-home"
    monkeypatch.setenv("AI_OS_HOME", str(home))
    return home
