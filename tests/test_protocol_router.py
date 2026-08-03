"""Tests for `ai_os.mcp.protocol_router.ProtocolRouter` — pure routing logic
against fake adapter stand-ins, no real providers involved.
"""
from __future__ import annotations

import pytest

from ai_os.mcp.adapters.base_adapter import BaseMCPAdapter, LLMTaskRequest, LLMTaskResponse, TokenUsage
from ai_os.mcp.protocol_router import (
    DEFAULT_RISK_PROVIDER_ORDER,
    NoAdapterConfiguredError,
    ProtocolRouter,
    risk_provider_order_from_env,
)


class _FakeAdapter(BaseMCPAdapter):
    def __init__(self, provider_name: str) -> None:
        self.provider_name = provider_name
        self.calls: list[LLMTaskRequest] = []

    async def execute_task(self, request: LLMTaskRequest) -> LLMTaskResponse:
        self.calls.append(request)
        return LLMTaskResponse(
            task_id=request.task_id,
            provider=self.provider_name,
            model_name="fake-model",
            generated_text=f"response from {self.provider_name}",
            usage=TokenUsage(input_tokens=1, output_tokens=1),
        )


def _request(task_id: str = "T1") -> LLMTaskRequest:
    return LLMTaskRequest(task_id=task_id, context_payload="do the thing")


async def test_execute_for_risk_picks_first_configured_provider_in_order():
    router = ProtocolRouter({"anthropic": _FakeAdapter("anthropic")})
    response = await router.execute_for_risk("CRITICAL", _request())
    assert response.provider == "anthropic"


async def test_execute_for_risk_skips_unconfigured_providers_in_preference_order():
    # LOW's preference order is gemini, openrouter, anthropic — only
    # anthropic is configured, so it must still be picked.
    router = ProtocolRouter({"anthropic": _FakeAdapter("anthropic")})
    response = await router.execute_for_risk("LOW", _request())
    assert response.provider == "anthropic"


async def test_execute_for_risk_raises_when_nothing_configured():
    router = ProtocolRouter({})
    with pytest.raises(NoAdapterConfiguredError):
        await router.execute_for_risk("HIGH", _request())


async def test_execute_explicit_provider_bypasses_risk_order():
    router = ProtocolRouter({"gemini": _FakeAdapter("gemini"), "anthropic": _FakeAdapter("anthropic")})
    response = await router.execute("gemini", _request())
    assert response.provider == "gemini"


async def test_execute_raises_for_unconfigured_explicit_provider():
    router = ProtocolRouter({"gemini": _FakeAdapter("gemini")})
    with pytest.raises(NoAdapterConfiguredError):
        await router.execute("openrouter", _request())


def test_default_risk_provider_order_covers_all_four_risk_levels():
    assert set(DEFAULT_RISK_PROVIDER_ORDER) == {"LOW", "MEDIUM", "HIGH", "CRITICAL"}
    for order in DEFAULT_RISK_PROVIDER_ORDER.values():
        assert order  # never an empty preference list


async def test_custom_risk_provider_order_is_respected():
    router = ProtocolRouter(
        {"openrouter": _FakeAdapter("openrouter"), "anthropic": _FakeAdapter("anthropic")},
        risk_provider_order={"LOW": ["anthropic", "openrouter"]},
    )
    response = await router.execute_for_risk("LOW", _request())
    assert response.provider == "anthropic"


def test_env_provider_order_defaults_when_unset():
    order = risk_provider_order_from_env(environ={})
    assert order == DEFAULT_RISK_PROVIDER_ORDER
    # a copy, not the shared module-level dict (mutating it must not leak)
    order["LOW"].append("mutation")
    assert "mutation" not in DEFAULT_RISK_PROVIDER_ORDER["LOW"]


def test_env_provider_order_override_single_level():
    order = risk_provider_order_from_env(environ={"AI_OS_PROVIDER_ORDER_MEDIUM": "anthropic, openrouter"})
    assert order["MEDIUM"] == ["anthropic", "openrouter"]
    # other levels keep their defaults
    assert order["LOW"] == DEFAULT_RISK_PROVIDER_ORDER["LOW"]


async def test_env_provider_order_actually_changes_routing():
    order = risk_provider_order_from_env(environ={"AI_OS_PROVIDER_ORDER_LOW": "anthropic,openrouter"})
    router = ProtocolRouter(
        {"gemini": _FakeAdapter("gemini"), "anthropic": _FakeAdapter("anthropic")},
        risk_provider_order=order,
    )
    # Default LOW order would pick gemini first; the env override puts anthropic first.
    response = await router.execute_for_risk("LOW", _request())
    assert response.provider == "anthropic"
