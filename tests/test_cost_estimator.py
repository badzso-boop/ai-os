"""Deterministic tests for the pre-flight token/cost estimator."""
from __future__ import annotations

from ai_os.core.cost_estimator import (
    EstimatorConfig,
    estimate_epic,
    price_for,
)
from ai_os.core.models import TaskNode
from ai_os.core.scheduler import DynamicScheduler
from ai_os.knowledge.graph_engine import KnowledgeEngine
from ai_os.mcp.adapters.base_adapter import BaseMCPAdapter, LLMTaskRequest, LLMTaskResponse, TokenUsage
from ai_os.mcp.protocol_router import ProtocolRouter


class _Adapter(BaseMCPAdapter):
    def __init__(self, use_cli_session=False):
        self.use_cli_session = use_cli_session

    async def execute_task(self, request: LLMTaskRequest) -> LLMTaskResponse:  # pragma: no cover
        return LLMTaskResponse(task_id=request.task_id, provider="x", model_name="m", generated_text="", usage=TokenUsage())


def _task(tid, risk="LOW", files=None):
    files = files or [f"{tid}.py"]
    return TaskNode(id=tid, title=tid, description="do it", risk_level=risk, target_files=files, write_set=set(files))


PRICES = {"sonnet": (3.0, 15.0), "haiku": (0.8, 4.0)}


def test_price_for_matches_by_substring():
    assert price_for("anthropic", "claude-sonnet-4-5", _Adapter(), PRICES) == (3.0, 15.0)
    assert price_for("anthropic", "haiku", _Adapter(), PRICES) == (0.8, 4.0)


def test_price_for_subscription_session_is_none():
    # Anthropic in CLI-session mode -> subscription, no per-token price.
    assert price_for("anthropic", "sonnet", _Adapter(use_cli_session=True), PRICES) is None


def test_price_for_unknown_model_is_none():
    assert price_for("openrouter", "some/unknown-model", _Adapter(), PRICES) is None


def test_estimate_epic_metered_has_cost():
    engine = KnowledgeEngine()
    adapters = {"openrouter": _Adapter()}
    router = ProtocolRouter(adapters, risk_provider_order={lvl: ["openrouter"] for lvl in ("LOW", "MEDIUM", "HIGH", "CRITICAL")})
    scheduler = DynamicScheduler(router, environ={"AI_OS_MODEL_OPENROUTER_LOW": "anthropic/claude-sonnet-4.5"})

    est = estimate_epic([_task("A"), _task("B")], engine, scheduler, adapters)
    assert len(est.per_task) == 2
    assert est.total_input_tokens > 0 and est.total_output_tokens > 0
    assert est.total_usd > 0  # sonnet is priced
    assert est.per_task[0].usd is not None


def test_estimate_epic_subscription_no_cost():
    engine = KnowledgeEngine()
    adapters = {"anthropic": _Adapter(use_cli_session=True)}
    router = ProtocolRouter(adapters)  # default order routes to anthropic
    scheduler = DynamicScheduler(router, environ={})

    est = estimate_epic([_task("A", risk="CRITICAL")], engine, scheduler, adapters)
    assert est.total_usd == 0.0
    assert est.has_subscription is True
    assert est.per_task[0].usd is None


def test_estimator_config_from_env():
    cfg = EstimatorConfig.from_env(environ={"AI_OS_EST_TURNS_PER_TASK": "6", "AI_OS_EST_OUTPUT_TOKENS_PER_TURN": "1000"})
    assert cfg.turns_per_task == 6.0
    assert cfg.output_tokens_per_turn == 1000
