from __future__ import annotations

"""Tests for validating docs/SANDBOX_CONFIG.md documentation on risk-level overrides."""

from pathlib import Path


def test_sandbox_config_doc_risk_level_overrides() -> None:
    """Verify that docs/SANDBOX_CONFIG.md contains the Risk-level overrides section and details."""
    doc_path = Path("docs/SANDBOX_CONFIG.md")
    assert doc_path.exists(), "docs/SANDBOX_CONFIG.md file must exist"

    content = doc_path.read_text(encoding="utf-8")

    assert "Risk-level overrides" in content
    assert "risks.high" in content
    assert "risks.critical" in content
    assert "test_command" in content
    assert ".ai-os/sandbox.json" in content
    assert "risk" in content