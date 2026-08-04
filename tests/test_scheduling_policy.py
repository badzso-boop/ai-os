"""Tests for `ai_os.core.scheduling_policy` (Phase 5 Stage 4) and the adapters'
`RateLimitedError` on HTTP 429 — no real network, no real waiting (sleep is
injected and just records the delays it was asked for)."""
from __future__ import annotations

import httpx
import pytest

from ai_os.core.scheduling_policy import BudgetExceededError, SchedulingPolicy
from ai_os.mcp.adapters.base_adapter import RateLimitedError, parse_retry_after


# -- parse_retry_after -------------------------------------------------------


def test_parse_retry_after_numeric():
    assert parse_retry_after("12") == 12.0


def test_parse_retry_after_none_and_http_date_return_none():
    assert parse_retry_after(None) is None
    assert parse_retry_after("Wed, 21 Oct 2025 07:28:00 GMT") is None


# -- SchedulingPolicy.with_backoff -------------------------------------------


class _Sleeps:
    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)


async def test_with_backoff_retries_then_succeeds():
    sleeps = _Sleeps()
    policy = SchedulingPolicy(max_rate_limit_retries=3, base_backoff_seconds=1.0, sleep=sleeps)
    calls = {"n": 0}

    async def attempt():
        calls["n"] += 1
        if calls["n"] < 3:
            raise RateLimitedError("gemini")  # no Retry-After -> exponential
        return "ok"

    assert await policy.with_backoff(attempt) == "ok"
    assert calls["n"] == 3
    # exponential base*2**0, base*2**1 for the two failed attempts
    assert sleeps.delays == [1.0, 2.0]


async def test_with_backoff_honors_retry_after():
    sleeps = _Sleeps()
    policy = SchedulingPolicy(max_rate_limit_retries=2, sleep=sleeps)

    async def attempt():
        raise RateLimitedError("openrouter", retry_after=7.0)

    with pytest.raises(RateLimitedError):
        await policy.with_backoff(attempt)
    # retry_after wins over exponential, once per allowed retry
    assert sleeps.delays == [7.0, 7.0]


async def test_with_backoff_reraises_after_exhaustion():
    sleeps = _Sleeps()
    policy = SchedulingPolicy(max_rate_limit_retries=1, sleep=sleeps)

    async def attempt():
        raise RateLimitedError("gemini")

    with pytest.raises(RateLimitedError):
        await policy.with_backoff(attempt)
    assert len(sleeps.delays) == 1  # one retry, then re-raise


async def test_with_backoff_propagates_other_errors_immediately():
    sleeps = _Sleeps()
    policy = SchedulingPolicy(max_rate_limit_retries=3, sleep=sleeps)

    async def attempt():
        raise ValueError("boom")

    with pytest.raises(ValueError):
        await policy.with_backoff(attempt)
    assert sleeps.delays == []  # no backoff for non-rate-limit errors


# -- run_over_providers (fallback) -------------------------------------------


async def test_run_over_providers_falls_back_to_next():
    sleeps = _Sleeps()
    policy = SchedulingPolicy(max_rate_limit_retries=1, sleep=sleeps)

    async def always_limited():
        raise RateLimitedError("gemini")

    async def succeeds():
        return "from-openrouter"

    result = await policy.run_over_providers(
        [("gemini", always_limited), ("openrouter", succeeds)]
    )
    assert result == "from-openrouter"


async def test_run_over_providers_reraises_when_all_limited():
    policy = SchedulingPolicy(max_rate_limit_retries=0, sleep=_Sleeps())

    async def limited():
        raise RateLimitedError("gemini")

    with pytest.raises(RateLimitedError):
        await policy.run_over_providers([("gemini", limited), ("openrouter", limited)])


# -- budget ------------------------------------------------------------------


def test_check_budget_raises_at_cap():
    policy = SchedulingPolicy(budget_usd=0.50)
    policy.check_budget(0.49)  # under cap -> fine
    with pytest.raises(BudgetExceededError):
        policy.check_budget(0.50)


def test_from_env_parses_budget_and_retries():
    policy = SchedulingPolicy.from_env(
        environ={"AI_OS_EPIC_BUDGET_USD": "1.25", "AI_OS_RATE_LIMIT_RETRIES": "5"}
    )
    assert policy.budget_usd == 1.25
    assert policy.max_rate_limit_retries == 5


def test_from_env_ignores_malformed_values():
    policy = SchedulingPolicy.from_env(environ={"AI_OS_EPIC_BUDGET_USD": "free"})
    assert policy.budget_usd is None


# -- adapters raise RateLimitedError on 429 ----------------------------------


async def test_gemini_raises_rate_limited_on_429():
    from ai_os.mcp.adapters.gemini_adapter import GeminiAdapter
    from ai_os.mcp.adapters.base_adapter import LLMTaskRequest

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"retry-after": "3"}, text="slow down")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = GeminiAdapter(api_key="k", client=client)
    with pytest.raises(RateLimitedError) as exc:
        await adapter.execute_task(LLMTaskRequest(task_id="T", context_payload="hi"))
    assert exc.value.provider == "gemini"
    assert exc.value.retry_after == 3.0


async def test_openrouter_raises_rate_limited_on_429():
    from ai_os.mcp.adapters.openrouter_adapter import OpenRouterAdapter
    from ai_os.mcp.adapters.base_adapter import LLMTaskRequest

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, text="too many requests")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = OpenRouterAdapter(api_key="k", model="x/y", client=client)
    with pytest.raises(RateLimitedError) as exc:
        await adapter.execute_task(LLMTaskRequest(task_id="T", context_payload="hi"))
    assert exc.value.provider == "openrouter"
    assert exc.value.retry_after is None  # no Retry-After header
