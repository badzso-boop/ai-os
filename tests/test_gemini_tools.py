"""Tests for GeminiAdapter.execute_with_tools (native function-calling loop).

Uses httpx.MockTransport so the real request-building / response-parsing code
runs, but no socket is opened and no real API key is needed. The mock handler
returns a SCRIPTED SEQUENCE of generateContent responses across successive
calls (tracked by a call counter), simulating Gemini's function-calling turns.
"""
from __future__ import annotations

import json

import httpx
import pytest

from ai_os.mcp.adapters.base_adapter import LLMTaskRequest, ToolSpec
from ai_os.mcp.adapters.gemini_adapter import GeminiApiError, GeminiAdapter


def _adapter(handler, **kwargs) -> GeminiAdapter:
    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport)
    return GeminiAdapter(api_key="test-api-key", client=client, **kwargs)


def _tool(name: str = "get_weather") -> ToolSpec:
    return ToolSpec(
        name=name,
        description=f"call {name}",
        json_schema={
            "type": "object",
            "properties": {"city": {"type": "string"}},
            "required": ["city"],
        },
    )


def _function_call_response(
    calls: list[tuple[str, dict]],
    prompt_tokens: int = 10,
    candidate_tokens: int = 5,
) -> dict:
    """A generateContent response whose single candidate emits functionCall parts."""
    return {
        "candidates": [
            {
                "content": {
                    "role": "model",
                    "parts": [
                        {"functionCall": {"name": name, "args": args}}
                        for name, args in calls
                    ],
                },
                "finishReason": "STOP",
            }
        ],
        "usageMetadata": {
            "promptTokenCount": prompt_tokens,
            "candidatesTokenCount": candidate_tokens,
        },
    }


def _text_response(
    text: str, prompt_tokens: int = 7, candidate_tokens: int = 3
) -> dict:
    return {
        "candidates": [
            {
                "content": {"role": "model", "parts": [{"text": text}]},
                "finishReason": "STOP",
            }
        ],
        "usageMetadata": {
            "promptTokenCount": prompt_tokens,
            "candidatesTokenCount": candidate_tokens,
        },
    }


class _RecordingDispatch:
    """Async dispatch that records (name, args) calls and returns canned results."""

    def __init__(self, result: str = "sunny, 25C") -> None:
        self.result = result
        self.calls: list[tuple[str, dict]] = []

    async def __call__(self, name: str, args: dict) -> str:
        self.calls.append((name, args))
        return self.result


async def test_happy_path_single_tool_call():
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        if len(bodies) == 1:
            return httpx.Response(
                200, json=_function_call_response([("get_weather", {"city": "Paris"})])
            )
        return httpx.Response(200, json=_text_response("It is sunny in Paris."))

    adapter = _adapter(handler)
    dispatch = _RecordingDispatch("sunny, 25C")
    request = LLMTaskRequest(task_id="t1", context_payload="weather in Paris?")

    response = await adapter.execute_with_tools(request, [_tool()], dispatch)

    assert len(dispatch.calls) == 1
    assert dispatch.calls[0] == ("get_weather", {"city": "Paris"})
    assert response.generated_text == "It is sunny in Paris."
    assert response.provider == "gemini"
    assert response.task_id == "t1"
    assert len(bodies) == 2


async def test_multiple_round_trips_of_tool_use():
    def handler(request: httpx.Request) -> httpx.Response:
        n = handler.count = getattr(handler, "count", 0) + 1
        if n == 1:
            return httpx.Response(
                200, json=_function_call_response([("get_weather", {"city": "Paris"})])
            )
        if n == 2:
            return httpx.Response(
                200, json=_function_call_response([("get_weather", {"city": "Rome"})])
            )
        return httpx.Response(200, json=_text_response("Both cities are sunny."))

    adapter = _adapter(handler)
    dispatch = _RecordingDispatch()
    request = LLMTaskRequest(task_id="t2", context_payload="weather in Paris and Rome?")

    response = await adapter.execute_with_tools(request, [_tool()], dispatch)

    assert len(dispatch.calls) == 2
    assert dispatch.calls[0] == ("get_weather", {"city": "Paris"})
    assert dispatch.calls[1] == ("get_weather", {"city": "Rome"})
    assert response.generated_text == "Both cities are sunny."


async def test_two_function_calls_in_one_turn():
    def handler(request: httpx.Request) -> httpx.Response:
        n = handler.count = getattr(handler, "count", 0) + 1
        if n == 1:
            return httpx.Response(
                200,
                json=_function_call_response(
                    [
                        ("get_weather", {"city": "Paris"}),
                        ("get_weather", {"city": "Rome"}),
                    ]
                ),
            )
        return httpx.Response(200, json=_text_response("Paris and Rome are both sunny."))

    adapter = _adapter(handler)
    dispatch = _RecordingDispatch()
    request = LLMTaskRequest(task_id="t3", context_payload="weather in Paris and Rome?")

    response = await adapter.execute_with_tools(request, [_tool()], dispatch)

    # Both parallel calls dispatched in the single turn, then loop finished.
    assert len(dispatch.calls) == 2
    assert dispatch.calls[0] == ("get_weather", {"city": "Paris"})
    assert dispatch.calls[1] == ("get_weather", {"city": "Rome"})
    assert response.generated_text == "Paris and Rome are both sunny."


async def test_request_shape_tools_and_function_response():
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        if len(bodies) == 1:
            return httpx.Response(
                200, json=_function_call_response([("get_weather", {"city": "Paris"})])
            )
        return httpx.Response(200, json=_text_response("done"))

    adapter = _adapter(handler)
    dispatch = _RecordingDispatch("sunny, 25C")
    request = LLMTaskRequest(
        task_id="t4",
        system_prompt="You are a weather bot.",
        context_payload="weather in Paris?",
    )

    await adapter.execute_with_tools(request, [_tool("get_weather")], dispatch)

    # First request carries the functionDeclarations built from the ToolSpec.
    first = bodies[0]
    decls = first["tools"][0]["functionDeclarations"]
    assert decls[0]["name"] == "get_weather"
    assert decls[0]["description"] == "call get_weather"
    assert decls[0]["parameters"] == {
        "type": "object",
        "properties": {"city": {"type": "string"}},
        "required": ["city"],
    }
    assert first["systemInstruction"] == {"parts": [{"text": "You are a weather bot."}]}
    # First turn is just the user prompt.
    assert first["contents"][0] == {
        "role": "user",
        "parts": [{"text": "weather in Paris?"}],
    }

    # Second request echoes the model's functionCall turn + the functionResponse
    # carrying the dispatched result.
    second = bodies[1]
    assert second["tools"] == first["tools"]  # tools re-sent every round-trip
    contents = second["contents"]
    # user prompt, model functionCall turn, user functionResponse turn
    assert contents[1]["role"] == "model"
    assert contents[1]["parts"][0]["functionCall"]["name"] == "get_weather"
    assert contents[2]["role"] == "user"
    fr = contents[2]["parts"][0]["functionResponse"]
    assert fr["name"] == "get_weather"
    assert fr["response"] == {"result": "sunny, 25C"}


async def test_usage_accumulates_across_round_trips():
    def handler(request: httpx.Request) -> httpx.Response:
        n = handler.count = getattr(handler, "count", 0) + 1
        if n == 1:
            return httpx.Response(
                200,
                json=_function_call_response(
                    [("get_weather", {"city": "Paris"})],
                    prompt_tokens=100,
                    candidate_tokens=10,
                ),
            )
        if n == 2:
            return httpx.Response(
                200,
                json=_function_call_response(
                    [("get_weather", {"city": "Rome"})],
                    prompt_tokens=200,
                    candidate_tokens=20,
                ),
            )
        return httpx.Response(
            200,
            json=_text_response("done", prompt_tokens=300, candidate_tokens=30),
        )

    adapter = _adapter(handler)
    dispatch = _RecordingDispatch()
    request = LLMTaskRequest(task_id="t5", context_payload="weather?")

    response = await adapter.execute_with_tools(request, [_tool()], dispatch)

    assert response.usage.input_tokens == 100 + 200 + 300
    assert response.usage.output_tokens == 10 + 20 + 30
    assert response.usage.estimated_usd_cost == 0.0


async def test_max_tool_iterations_exceeded_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        handler.count = getattr(handler, "count", 0) + 1
        # Always ask for another tool call — never finish.
        return httpx.Response(
            200, json=_function_call_response([("get_weather", {"city": "Paris"})])
        )

    adapter = _adapter(handler)
    dispatch = _RecordingDispatch()
    request = LLMTaskRequest(task_id="t6", context_payload="loop forever")

    with pytest.raises(GeminiApiError, match="max_tool_iterations"):
        await adapter.execute_with_tools(
            request, [_tool()], dispatch, max_tool_iterations=3
        )

    # Bounded: exactly 3 generateContent round-trips, no infinite loop.
    assert handler.count == 3


async def test_supports_tool_calling_true():
    adapter = _adapter(lambda request: httpx.Response(200, json=_text_response("x")))
    assert adapter.supports_tool_calling() is True
