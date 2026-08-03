"""Tests for ai_os.mcp.adapters.gemini_adapter.

Uses httpx.MockTransport so real httpx request-building/response-parsing code
runs, but no socket is ever opened and no real API key is needed.
"""
from __future__ import annotations

import json

import httpx
import pytest

from ai_os.mcp.adapters.base_adapter import LLMTaskRequest
from ai_os.mcp.adapters.gemini_adapter import GeminiApiError, GeminiAdapter


def _adapter(handler, **kwargs) -> GeminiAdapter:
    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport)
    return GeminiAdapter(api_key="test-api-key", client=client, **kwargs)


def _happy_response_json(text: str = "hello from gemini") -> dict:
    return {
        "candidates": [
            {
                "content": {"parts": [{"text": text}], "role": "model"},
                "finishReason": "STOP",
            }
        ],
        "usageMetadata": {
            "promptTokenCount": 12,
            "candidatesTokenCount": 34,
            "totalTokenCount": 46,
        },
        "modelVersion": "gemini-3.5-flash-lite",
    }


async def test_happy_path_returns_generated_text_and_usage():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_happy_response_json("42 is the answer"))

    adapter = _adapter(handler)
    request = LLMTaskRequest(task_id="t1", system_prompt="", context_payload="what is the answer?")

    response = await adapter.execute_task(request)

    assert response.task_id == "t1"
    assert response.provider == "gemini"
    assert response.model_name == "gemini-3.5-flash-lite"
    assert response.generated_text == "42 is the answer"
    assert response.usage.input_tokens == 12
    assert response.usage.output_tokens == 34
    assert response.usage.estimated_usd_cost == 0.0


async def test_request_construction_uses_correct_url_and_body_shape():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json=_happy_response_json())

    adapter = _adapter(handler)
    request = LLMTaskRequest(
        task_id="t2",
        system_prompt="You are a helpful assistant.",
        context_payload="say hi",
    )

    await adapter.execute_task(request)

    url = captured["url"]
    assert "gemini-3.5-flash-lite:generateContent" in url
    assert "key=test-api-key" in url

    body = captured["body"]
    assert body["contents"] == [{"parts": [{"text": "say hi"}]}]
    assert body["systemInstruction"] == {"parts": [{"text": "You are a helpful assistant."}]}


async def test_no_system_instruction_field_when_system_prompt_empty():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json=_happy_response_json())

    adapter = _adapter(handler)
    request = LLMTaskRequest(task_id="t3", system_prompt="", context_payload="say hi")

    await adapter.execute_task(request)

    assert "systemInstruction" not in captured["body"]


async def test_request_model_override_changes_url_model():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(200, json=_happy_response_json())

    adapter = _adapter(handler, model="gemini-3.5-flash-lite")
    request = LLMTaskRequest(
        task_id="t4",
        context_payload="say hi",
        model="gemini-2.5-pro",
    )

    response = await adapter.execute_task(request)

    assert "gemini-2.5-pro:generateContent" in captured["url"]
    assert response.model_name == "gemini-2.5-pro"


async def test_non_2xx_response_raises_gemini_api_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": {"message": "API key not valid"}})

    adapter = _adapter(handler)
    request = LLMTaskRequest(task_id="t5", context_payload="say hi")

    with pytest.raises(GeminiApiError):
        await adapter.execute_task(request)


async def test_forbidden_response_raises_gemini_api_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="Forbidden")

    adapter = _adapter(handler)
    request = LLMTaskRequest(task_id="t6", context_payload="say hi")

    with pytest.raises(GeminiApiError):
        await adapter.execute_task(request)


async def test_empty_candidates_raises_gemini_api_error_with_clear_message():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "candidates": [],
                "promptFeedback": {"blockReason": "SAFETY"},
            },
        )

    adapter = _adapter(handler)
    request = LLMTaskRequest(task_id="t7", context_payload="something unsafe")

    with pytest.raises(GeminiApiError, match="SAFETY"):
        await adapter.execute_task(request)


async def test_missing_candidates_key_raises_gemini_api_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"promptFeedback": {"blockReason": "OTHER"}})

    adapter = _adapter(handler)
    request = LLMTaskRequest(task_id="t8", context_payload="say hi")

    with pytest.raises(GeminiApiError):
        await adapter.execute_task(request)


async def test_malformed_candidate_shape_raises_gemini_api_error_not_keyerror():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"candidates": [{"content": {"parts": []}}]})

    adapter = _adapter(handler)
    request = LLMTaskRequest(task_id="t9", context_payload="say hi")

    with pytest.raises(GeminiApiError):
        await adapter.execute_task(request)
