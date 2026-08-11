"""Tests validating documentation and risk-level override representations in docs/SANDBOX_CONFIG.md."""

from __future__ import annotations

from pathlib import Path


def test_sandbox_config_doc_exists_and_contains_risk_overrides() -> None:
    """Verify docs/SANDBOX_CONFIG.md exists and contains the mandatory 'Risk-level overrides' section."""
    doc_path = Path("docs/SANDBOX_CONFIG.md")
    assert doc_path.is_file(), "docs/SANDBOX_CONFIG.md must exist."

    content = doc_path.read_text(encoding="utf-8")

    # Assert required title section and explanations exist
    assert "## Risk-level overrides" in content
    assert "risks.high" in content
    assert "risks.critical" in content
    assert "test_command" in content
    assert ".ai-os/sandbox.json" in content
    assert "risk" in content.lower()