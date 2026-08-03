"""Tests for `OpenRouterAdapter` (ai_os/mcp/adapters/openrouter_adapter.py).

Real httpx request-building/response-parsing runs throughout; only the
socket is swapped via `httpx.MockTransport` (httpx's own sanctioned test
transport) — no mocking of our own code's internals, no real network calls,
no real API key needed.
"""
from __future__ import annotations

import json

import httpx
import pytest

from ai_os.mcp.adapters.base_adapter import LLMTaskRequest
from ai_os.mcp.adapters.openrouter_adapter import (
    OPENROUTER_API_URL,
    OpenRouterAdapter,
    OpenRouterApiError,
)


def _openai_compatible_response(
    content: str = "hello from openrouter",
    prompt_tokens: int = 10,
    completion_tokens: int = 5,
    cost: float | None = None,
    model: str = "openai/gpt-4o",
) -> dict:
    usage = {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens}
    if cost is not None:
        usage["cost"] = cost
    return {
        "id": "gen-123",
        "object": "chat.completion",
        "model": model,
        "choices": [
            {
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": content},
            }
        ],
        "usage": usage,
    }


def _transport_capturing(captured: dict, response_json: dict, status_code: int = 200):
    def handler(request: httpx.Request) -> httpx.Response:
        captured["request"] = request
        captured["body"] = json.loads(request.content)
        return httpx.Response(status_code, json=response_json)

    return httpx.MockTransport(handler)


# -- 1. Happy path -----------------------------------------------------------


async def test_happy_path_returns_text_and_usage():
    response_json = _openai_compatible_response(
        content="42 is the answer", prompt_tokens=20, completion_tokens=8
    )
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, json=response_json)
    )
    client = httpx.AsyncClient(transport=transport)
    adapter = OpenRouterAdapter(api_key="or-test-key", model="openai/gpt-4o", client=client)

    request = LLMTaskRequest(
        task_id="t1",
        system_prompt="You are helpful.",
        context_payload="What is 40+2?",
    )
    result = await adapter.execute_task(request)

    assert result.task_id == "t1"
    assert result.provider == "openrouter"
    assert result.model_name == "openai/gpt-4o"
    assert result.generated_text == "42 is the answer"
    assert result.usage.input_tokens == 20
    assert result.usage.output_tokens == 8
    assert result.usage.estimated_usd_cost == 0.0  # no cost field in response


# -- 2. Correct request construction -----------------------------------------


async def test_request_construction_url_auth_and_messages():
    captured: dict = {}
    transport = _transport_capturing(captured, _openai_compatible_response())
    client = httpx.AsyncClient(transport=transport)
    adapter = OpenRouterAdapter(api_key="secret-key-123", model="anthropic/claude-3.5-sonnet", client=client)

    request = LLMTaskRequest(
        task_id="t2",
        system_prompt="Be terse.",
        context_payload="Say hi.",
    )
    await adapter.execute_task(request)

    sent_request = captured["request"]
    assert str(sent_request.url) == OPENROUTER_API_URL
    assert sent_request.headers["Authorization"] == "Bearer secret-key-123"

    body = captured["body"]
    assert body["model"] == "anthropic/claude-3.5-sonnet"
    assert body["messages"] == [
        {"role": "system", "content": "Be terse."},
        {"role": "user", "content": "Say hi."},
    ]


async def test_request_construction_omits_system_message_when_absent():
    captured: dict = {}
    transport = _transport_capturing(captured, _openai_compatible_response())
    client = httpx.AsyncClient(transport=transport)
    adapter = OpenRouterAdapter(api_key="k", model="openai/gpt-4o", client=client)

    request = LLMTaskRequest(task_id="t2b", context_payload="Just this.")
    await adapter.execute_task(request)

    body = captured["body"]
    assert body["messages"] == [{"role": "user", "content": "Just this."}]


# -- 3. Missing model raises a clear error -----------------------------------


async def test_missing_model_raises_value_error():
    # No model at construction, none on the request either.
    adapter = OpenRouterAdapter(api_key="k", client=httpx.AsyncClient(transport=httpx.MockTransport(
        lambda r: httpx.Response(200, json=_openai_compatible_response())
    )))
    request = LLMTaskRequest(task_id="t3", context_payload="hi")

    with pytest.raises(ValueError):
        await adapter.execute_task(request)


# -- 4. request.model overrides constructor default --------------------------


async def test_request_model_overrides_constructor_default():
    captured: dict = {}
    transport = _transport_capturing(
        captured, _openai_compatible_response(model="google/gemini-pro-1.5")
    )
    client = httpx.AsyncClient(transport=transport)
    adapter = OpenRouterAdapter(api_key="k", model="openai/gpt-4o", client=client)

    request = LLMTaskRequest(
        task_id="t4", context_payload="hi", model="google/gemini-pro-1.5"
    )
    result = await adapter.execute_task(request)

    assert captured["body"]["model"] == "google/gemini-pro-1.5"
    assert result.model_name == "google/gemini-pro-1.5"


# -- 5. Optional attribution headers -----------------------------------------


async def test_attribution_headers_included_when_configured():
    captured: dict = {}
    transport = _transport_capturing(captured, _openai_compatible_response())
    client = httpx.AsyncClient(transport=transport)
    adapter = OpenRouterAdapter(
        api_key="k",
        model="openai/gpt-4o",
        client=client,
        site_url="https://example.com",
        app_title="AI-OS",
    )
    request = LLMTaskRequest(task_id="t5", context_payload="hi")
    await adapter.execute_task(request)

    headers = captured["request"].headers
    assert headers["HTTP-Referer"] == "https://example.com"
    assert headers["X-Title"] == "AI-OS"


async def test_attribution_headers_absent_when_not_configured():
    captured: dict = {}
    transport = _transport_capturing(captured, _openai_compatible_response())
    client = httpx.AsyncClient(transport=transport)
    adapter = OpenRouterAdapter(api_key="k", model="openai/gpt-4o", client=client)

    request = LLMTaskRequest(task_id="t5b", context_payload="hi")
    await adapter.execute_task(request)

    headers = captured["request"].headers
    assert "HTTP-Referer" not in headers
    assert "X-Title" not in headers


# -- 6. Non-2xx response raises OpenRouterApiError ---------------------------


async def test_non_2xx_raises_openrouter_api_error():
    error_body = {
        "error": {
            "code": 404,
            "message": "Requested model not available",
            "metadata": {"error_type": "not_found"},
        }
    }
    transport = httpx.MockTransport(lambda request: httpx.Response(404, json=error_body))
    client = httpx.AsyncClient(transport=transport)
    adapter = OpenRouterAdapter(api_key="k", model="nonexistent/model", client=client)

    request = LLMTaskRequest(task_id="t6", context_payload="hi")

    with pytest.raises(OpenRouterApiError) as exc_info:
        await adapter.execute_task(request)

    assert exc_info.value.status_code == 404
    assert "not available" in str(exc_info.value) or "not available" in exc_info.value.body


async def test_401_unauthorized_raises_openrouter_api_error():
    error_body = {"error": {"code": 401, "message": "Invalid credentials"}}
    transport = httpx.MockTransport(lambda request: httpx.Response(401, json=error_body))
    client = httpx.AsyncClient(transport=transport)
    adapter = OpenRouterAdapter(api_key="bad-key", model="openai/gpt-4o", client=client)

    request = LLMTaskRequest(task_id="t7", context_payload="hi")

    with pytest.raises(OpenRouterApiError) as exc_info:
        await adapter.execute_task(request)

    assert exc_info.value.status_code == 401


async def test_cost_field_mapped_to_estimated_usd_cost_when_present():
    response_json = _openai_compatible_response(cost=0.00042)
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=response_json))
    client = httpx.AsyncClient(transport=transport)
    adapter = OpenRouterAdapter(api_key="k", model="openai/gpt-4o", client=client)

    request = LLMTaskRequest(task_id="t8", context_payload="hi")
    result = await adapter.execute_task(request)

    assert result.usage.estimated_usd_cost == 0.00042
