"""Bootstrap test for the UI-debug package.

Exists so `tests/ui/` is a real, non-empty test directory from the start — the
dogfooding sandbox validates with `python -m pytest tests/ui`, and an empty dir
would make pytest exit "no tests ran" (a false failure) before the first real
UI module + test lands. Real per-module tests (`test_ui_config.py`,
`test_ui_detectors.py`, …) join this directory as the feature is built.
"""
from __future__ import annotations

import ai_os.ui


def test_ui_package_imports():
    assert ai_os.ui.__doc__  # the package is importable and documented
