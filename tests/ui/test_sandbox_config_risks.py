"""Tests for the optional ``risks`` field in ``SandboxConfig``."""

from __future__ import annotations

from ai_os.sandbox.sandbox_config import SandboxConfig, parse_sandbox_config, SandboxConfigError


def test_risks_field_parsed():
    data = {
        "risks": {
            "sql_injection": {
                "severity": "high",
                "description": "Potential SQL injection vector",
            },
            "xss": {"severity": "medium"},
        }
    }
    config = parse_sandbox_config(data)
    assert isinstance(config, SandboxConfig)
    assert config.risks == {
        "sql_injection": {
            "severity": "high",
            "description": "Potential SQL injection vector",
        },
        "xss": {"severity": "medium"},
    }


def test_risks_missing_is_none():
    config = parse_sandbox_config({})
    assert isinstance(config, SandboxConfig)
    assert config.risks is None