"""CLI tests for the `ai-os llm` command group — monkeypatches
`ai_os.cli.load_configured_adapters` with fake, no-network adapters so these
never make a real call to any provider (that's what `ai-os llm test` itself
is for, run manually by a human with real credentials)."""
from __future__ import annotations

from click.testing import CliRunner

import ai_os.cli as cli_module
from ai_os.mcp.adapters.base_adapter import BaseMCPAdapter, LLMTaskRequest, LLMTaskResponse, TokenUsage


class _FakeAdapter(BaseMCPAdapter):
    async def execute_task(self, request: LLMTaskRequest) -> LLMTaskResponse:
        return LLMTaskResponse(
            task_id=request.task_id,
            provider="fake",
            model_name=request.model or "fake-default-model",
            generated_text=f"echo: {request.context_payload}",
            usage=TokenUsage(input_tokens=3, output_tokens=5, estimated_usd_cost=0.001),
        )


def test_llm_list_reports_no_providers_when_none_configured(monkeypatch):
    monkeypatch.setattr(cli_module, "load_configured_adapters", lambda: {})
    result = CliRunner().invoke(cli_module.main, ["llm", "list"])
    assert result.exit_code == 0
    assert "No providers configured" in result.output


def test_llm_list_shows_configured_providers(monkeypatch):
    monkeypatch.setattr(cli_module, "load_configured_adapters", lambda: {"fake": _FakeAdapter()})
    result = CliRunner().invoke(cli_module.main, ["llm", "list"])
    assert result.exit_code == 0
    assert "fake" in result.output


def test_llm_test_rejects_unconfigured_provider(monkeypatch):
    monkeypatch.setattr(cli_module, "load_configured_adapters", lambda: {})
    result = CliRunner().invoke(cli_module.main, ["llm", "test", "anthropic", "--prompt", "hi"])
    assert result.exit_code != 0
    assert "not configured" in result.output


def test_llm_test_prints_response_for_configured_provider(monkeypatch):
    monkeypatch.setattr(cli_module, "load_configured_adapters", lambda: {"fake": _FakeAdapter()})
    result = CliRunner().invoke(
        cli_module.main, ["llm", "test", "fake", "--prompt", "hello there", "--model", "override-model"]
    )
    assert result.exit_code == 0, result.output
    assert "echo: hello there" in result.output
    assert "override-model" in result.output
    assert "tokens in=3 out=5" in result.output
