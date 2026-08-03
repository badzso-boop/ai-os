"""Tests for `ai_os.mcp.config.load_configured_adapters` — pure environment-
dict-driven logic, no real .env file, no real network, no real `claude`
binary lookup beyond whatever's genuinely on this machine's PATH.
"""
from __future__ import annotations

from ai_os.mcp.adapters.anthropic_adapter import AnthropicAdapter
from ai_os.mcp.adapters.gemini_adapter import GeminiAdapter
from ai_os.mcp.adapters.openrouter_adapter import OpenRouterAdapter
from ai_os.mcp.config import load_configured_adapters


def test_empty_environment_configures_nothing():
    adapters = load_configured_adapters(environ={})
    assert adapters == {}


def test_gemini_configured_when_api_key_present():
    adapters = load_configured_adapters(environ={"GEMINI_API_KEY": "test-key"})
    assert isinstance(adapters["gemini"], GeminiAdapter)
    assert adapters["gemini"].api_key == "test-key"


def test_openrouter_configured_with_optional_fields():
    adapters = load_configured_adapters(
        environ={
            "OPENROUTER_API_KEY": "sk-or-test",
            "OPENROUTER_DEFAULT_MODEL": "anthropic/claude-sonnet-4.5",
            "OPENROUTER_SITE_URL": "https://example.com",
            "OPENROUTER_APP_TITLE": "ai-os",
        }
    )
    adapter = adapters["openrouter"]
    assert isinstance(adapter, OpenRouterAdapter)
    assert adapter.api_key == "sk-or-test"
    assert adapter.model == "anthropic/claude-sonnet-4.5"
    assert adapter.site_url == "https://example.com"
    assert adapter.app_title == "ai-os"


def test_anthropic_api_key_mode_explicit():
    adapters = load_configured_adapters(
        environ={"ANTHROPIC_MODE": "api_key", "ANTHROPIC_API_KEY": "sk-ant-test"}
    )
    adapter = adapters["anthropic"]
    assert isinstance(adapter, AnthropicAdapter)
    assert adapter.api_key == "sk-ant-test"
    assert adapter.use_cli_session is False


def test_anthropic_session_mode_requires_a_real_cli_on_path():
    # Explicitly request session mode but point ANTHROPIC_CLI at a binary
    # name that certainly doesn't exist on PATH -> not configured (silently
    # omitted, matching "configured only on real evidence").
    adapters = load_configured_adapters(
        environ={"ANTHROPIC_MODE": "session", "ANTHROPIC_CLI": "definitely-not-a-real-binary-xyz"}
    )
    assert "anthropic" not in adapters


def test_anthropic_session_mode_found_via_fake_path_entry(tmp_path):
    fake_cli = tmp_path / "claude"
    fake_cli.write_text("#!/bin/sh\necho fake\n")
    fake_cli.chmod(0o755)

    adapters = load_configured_adapters(
        environ={"ANTHROPIC_MODE": "session", "ANTHROPIC_CLI": "claude", "PATH": str(tmp_path)}
    )
    adapter = adapters["anthropic"]
    assert isinstance(adapter, AnthropicAdapter)
    assert adapter.use_cli_session is True
    assert adapter.claude_cli == str(fake_cli)


def test_anthropic_api_key_mode_without_key_is_not_configured():
    adapters = load_configured_adapters(environ={"ANTHROPIC_MODE": "api_key"})
    assert "anthropic" not in adapters


def test_missing_gemini_and_openrouter_keys_omit_them():
    adapters = load_configured_adapters(environ={"ANTHROPIC_MODE": "api_key", "ANTHROPIC_API_KEY": "k"})
    assert "gemini" not in adapters
    assert "openrouter" not in adapters
