"""Tests for `ai_os.core.scheduler.DynamicScheduler` — pure resolution logic
against a real `ProtocolRouter` with fake adapters (no real providers)."""
from __future__ import annotations

import pytest

from ai_os.core.scheduler import DynamicScheduler
from ai_os.mcp.adapters.base_adapter import BaseMCPAdapter, LLMTaskRequest, LLMTaskResponse
from ai_os.mcp.protocol_router import NoAdapterConfiguredError, ProtocolRouter


class _FakeAdapter(BaseMCPAdapter):
    async def execute_task(self, request: LLMTaskRequest) -> LLMTaskResponse:  # pragma: no cover
        raise NotImplementedError


def _router(*providers: str) -> ProtocolRouter:
    return ProtocolRouter({p: _FakeAdapter() for p in providers})


def test_anthropic_only_maps_risk_to_model_tiers():
    sched = DynamicScheduler(_router("anthropic"), environ={})
    assert sched.assign("LOW").model == "haiku"
    assert sched.assign("MEDIUM").model == "sonnet"
    assert sched.assign("HIGH").model == "sonnet"
    assert sched.assign("CRITICAL").model == "opus"
    assert sched.assign("LOW").provider == "anthropic"


def test_planning_assignment_is_critical_tier():
    sched = DynamicScheduler(_router("anthropic"), environ={})
    assert sched.planning_assignment().model == "opus"


def test_gemini_model_is_none_meaning_adapter_default():
    # With only gemini configured, LOW routes to gemini (per DEFAULT order),
    # and its model is None (use the adapter's own default).
    sched = DynamicScheduler(_router("gemini"), environ={})
    a = sched.assign("LOW")
    assert a.provider == "gemini"
    assert a.model is None


def test_env_override_changes_model():
    sched = DynamicScheduler(
        _router("anthropic"),
        environ={"AI_OS_MODEL_ANTHROPIC_LOW": "claude-haiku-4-5", "AI_OS_MODEL_ANTHROPIC_CRITICAL": "opus-custom"},
    )
    assert sched.assign("LOW").model == "claude-haiku-4-5"
    assert sched.assign("CRITICAL").model == "opus-custom"
    # untouched levels keep their defaults
    assert sched.assign("MEDIUM").model == "sonnet"


def test_env_override_for_openrouter_gives_it_a_model():
    sched = DynamicScheduler(
        _router("openrouter"),
        environ={"AI_OS_MODEL_OPENROUTER_MEDIUM": "anthropic/claude-sonnet-4.5"},
    )
    a = sched.assign("MEDIUM")
    assert a.provider == "openrouter"
    assert a.model == "anthropic/claude-sonnet-4.5"


def test_unconfigured_provider_raises():
    sched = DynamicScheduler(_router(), environ={})  # no adapters at all
    with pytest.raises(NoAdapterConfiguredError):
        sched.assign("HIGH")
