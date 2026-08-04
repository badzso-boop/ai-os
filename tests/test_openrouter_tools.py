"""Tests for `OpenRouterAdapter.execute_with_tools` — the autonomous
OpenAI-compatible function-calling agentic loop.

Real httpx request-building/response-parsing runs throughout; only the socket
is swapped via `httpx.MockTransport` (httpx's own sanctioned test transport)
returning a *scripted sequence* keyed off a call counter — no mocking of our
own code's internals, no real network, no real API key. The tool `dispatch`
is a real async callable that records what it was invoked with.
"""
from __future__ import annotations

import json

import httpx
import pytest

from ai_os.mcp.adapters.base_adapter import LLMTaskRequest, ToolSpec
from ai_os.mcp.adapters.openrouter_adapter import (
    OPENROUTER_API_URL,
    OpenRouterAdapter,
    OpenRouterApiError,
)


# --------------------------------------------------------------------------- #
# Helpers                                                                      #
# --------------------------------------------------------------------------- #


def _tool_call(call_id: str, name: str, arguments: str) -> dict:
    """One entry of an assistant `tool_calls` array (arguments is a JSON
    STRING, per the OpenAI/OpenRouter wire format)."""
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": arguments},
    }


def _assistant_tool_calls_response(
    tool_calls: list[dict],
    prompt_tokens: int = 10,
    completion_tokens: int = 4,
    model: str = "openai/gpt-4o",
) -> dict:
    return {
        "id": "gen-tool",
        "object": "chat.completion",
        "model": model,
        "choices": [
            {
                "finish_reason": "tool_calls",
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": tool_calls,
                },
            }
        ],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
        },
    }


def _assistant_text_response(
    content: str = "final answer",
    prompt_tokens: int = 7,
    completion_tokens: int = 3,
    model: str = "openai/gpt-4o",
) -> dict:
    return {
        "id": "gen-final",
        "object": "chat.completion",
        "model": model,
        "choices": [
            {
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": content},
            }
        ],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
        },
    }


def _scripted_transport(captured: list[dict], responses: list[dict]) -> httpx.MockTransport:
    """MockTransport returning `responses[i]` for the i-th request, recording
    every parsed request body into `captured`."""
    counter = {"i": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        i = counter["i"]
        counter["i"] += 1
        return httpx.Response(200, json=responses[i])

    return httpx.MockTransport(handler)


def _adapter_with(responses: list[dict], captured: list[dict]) -> OpenRouterAdapter:
    client = httpx.AsyncClient(transport=_scripted_transport(captured, responses))
    return OpenRouterAdapter(api_key="or-test-key", model="openai/gpt-4o", client=client)


def _make_dispatch(calls: list[tuple[str, dict]], result: str = "tool-result"):
    """Real async dispatcher recording each (name, args) it receives."""

    async def dispatch(name: str, args: dict) -> str:
        calls.append((name, args))
        return result

    return dispatch


_WEATHER_TOOL = ToolSpec(
    name="get_weather",
    description="Get the weather for a city",
    json_schema={
        "type": "object",
        "properties": {"city": {"type": "string"}},
        "required": ["city"],
    },
)


# --------------------------------------------------------------------------- #
# 1. Happy path: one tool call, then final text                               #
# --------------------------------------------------------------------------- #


async def test_single_tool_call_dispatched_and_result_posted_back():
    captured: list[dict] = []
    responses = [
        _assistant_tool_calls_response(
            [_tool_call("call_1", "get_weather", '{"city": "Paris"}')]
        ),
        _assistant_text_response("It is sunny in Paris."),
    ]
    adapter = _adapter_with(responses, captured)

    dispatched: list[tuple[str, dict]] = []
    dispatch = _make_dispatch(dispatched, result="sunny, 21C")

    request = LLMTaskRequest(
        task_id="t1", system_prompt="You are helpful.", context_payload="Weather in Paris?"
    )
    result = await adapter.execute_with_tools(request, [_WEATHER_TOOL], dispatch)

    # dispatch called exactly once, with the JSON-string arguments parsed into a dict
    assert dispatched == [("get_weather", {"city": "Paris"})]
    assert isinstance(dispatched[0][1], dict)

    # final text returned
    assert result.generated_text == "It is sunny in Paris."
    assert result.task_id == "t1"
    assert result.provider == "openrouter"

    # two round-trips happened
    assert len(captured) == 2

    # second request carries the assistant tool_calls turn + the tool result
    second_messages = captured[1]["messages"]
    tool_messages = [m for m in second_messages if m.get("role") == "tool"]
    assert len(tool_messages) == 1
    assert tool_messages[0]["tool_call_id"] == "call_1"
    assert tool_messages[0]["content"] == "sunny, 21C"

    # the assistant tool_calls turn is present verbatim before the tool result
    assistant_turns = [
        m for m in second_messages if m.get("role") == "assistant" and m.get("tool_calls")
    ]
    assert len(assistant_turns) == 1
    assert assistant_turns[0]["tool_calls"][0]["id"] == "call_1"


# --------------------------------------------------------------------------- #
# 2. Multiple sequential tool round-trips, then final text                    #
# --------------------------------------------------------------------------- #


async def test_multiple_sequential_tool_roundtrips():
    captured: list[dict] = []
    responses = [
        _assistant_tool_calls_response(
            [_tool_call("c1", "get_weather", '{"city": "Paris"}')]
        ),
        _assistant_tool_calls_response(
            [_tool_call("c2", "get_weather", '{"city": "Berlin"}')]
        ),
        _assistant_text_response("Paris sunny, Berlin rainy."),
    ]
    adapter = _adapter_with(responses, captured)

    dispatched: list[tuple[str, dict]] = []
    dispatch = _make_dispatch(dispatched)

    request = LLMTaskRequest(task_id="t2", context_payload="Compare weather.")
    result = await adapter.execute_with_tools(request, [_WEATHER_TOOL], dispatch)

    assert dispatched == [
        ("get_weather", {"city": "Paris"}),
        ("get_weather", {"city": "Berlin"}),
    ]
    assert result.generated_text == "Paris sunny, Berlin rainy."
    assert len(captured) == 3  # 3 POSTs: two tool rounds + final


# --------------------------------------------------------------------------- #
# 3. Two tool_calls in one assistant message — both dispatched                #
# --------------------------------------------------------------------------- #


async def test_two_tool_calls_in_one_message():
    captured: list[dict] = []
    responses = [
        _assistant_tool_calls_response(
            [
                _tool_call("a1", "get_weather", '{"city": "Paris"}'),
                _tool_call("a2", "get_weather", '{"city": "Tokyo"}'),
            ]
        ),
        _assistant_text_response("Done."),
    ]
    adapter = _adapter_with(responses, captured)

    dispatched: list[tuple[str, dict]] = []
    dispatch = _make_dispatch(dispatched)

    request = LLMTaskRequest(task_id="t3", context_payload="Two cities.")
    result = await adapter.execute_with_tools(request, [_WEATHER_TOOL], dispatch)

    # both dispatched
    assert dispatched == [
        ("get_weather", {"city": "Paris"}),
        ("get_weather", {"city": "Tokyo"}),
    ]
    assert result.generated_text == "Done."

    # both tool result messages appended before the second POST, each with its
    # own tool_call_id
    second_messages = captured[1]["messages"]
    tool_messages = [m for m in second_messages if m.get("role") == "tool"]
    assert [m["tool_call_id"] for m in tool_messages] == ["a1", "a2"]
    assert len(captured) == 2


# --------------------------------------------------------------------------- #
# 4. Request shape: tools built from ToolSpecs; tool messages on follow-up    #
# --------------------------------------------------------------------------- #


async def test_request_shape_tools_and_followup_tool_messages():
    captured: list[dict] = []
    responses = [
        _assistant_tool_calls_response(
            [_tool_call("z1", "get_weather", '{"city": "Rome"}')]
        ),
        _assistant_text_response("ok"),
    ]
    adapter = _adapter_with(responses, captured)
    dispatch = _make_dispatch([])

    request = LLMTaskRequest(
        task_id="t4", system_prompt="sys", context_payload="Rome weather?"
    )
    await adapter.execute_with_tools(request, [_WEATHER_TOOL], dispatch)

    # first request body includes tools built from the ToolSpec
    first = captured[0]
    assert first["model"] == "openai/gpt-4o"
    assert first["messages"][0] == {"role": "system", "content": "sys"}
    assert first["messages"][1] == {"role": "user", "content": "Rome weather?"}
    assert len(first["tools"]) == 1
    entry = first["tools"][0]
    assert entry["type"] == "function"
    assert entry["function"]["name"] == "get_weather"
    assert entry["function"]["description"] == "Get the weather for a city"
    assert entry["function"]["parameters"] == _WEATHER_TOOL.json_schema

    # a later request includes the role:"tool" message with the right id, and
    # still carries the tools array
    second = captured[1]
    assert "tools" in second
    tool_messages = [m for m in second["messages"] if m.get("role") == "tool"]
    assert tool_messages[0]["tool_call_id"] == "z1"


# --------------------------------------------------------------------------- #
# 5. Usage accumulation across round-trips                                     #
# --------------------------------------------------------------------------- #


async def test_usage_accumulated_across_roundtrips():
    captured: list[dict] = []
    responses = [
        _assistant_tool_calls_response(
            [_tool_call("u1", "get_weather", '{"city": "Paris"}')],
            prompt_tokens=100,
            completion_tokens=10,
        ),
        _assistant_text_response(
            "answer", prompt_tokens=200, completion_tokens=20
        ),
    ]
    adapter = _adapter_with(responses, captured)
    dispatch = _make_dispatch([])

    request = LLMTaskRequest(task_id="t5", context_payload="hi")
    result = await adapter.execute_with_tools(request, [_WEATHER_TOOL], dispatch)

    assert result.usage.input_tokens == 300  # 100 + 200
    assert result.usage.output_tokens == 30  # 10 + 20


# --------------------------------------------------------------------------- #
# 6. Malformed tool-call arguments -> clear error, not raw JSONDecodeError     #
# --------------------------------------------------------------------------- #


async def test_malformed_tool_arguments_raises_openrouter_api_error():
    captured: list[dict] = []
    responses = [
        _assistant_tool_calls_response(
            [_tool_call("bad", "get_weather", "{not valid json")]
        ),
        _assistant_text_response("unreached"),
    ]
    adapter = _adapter_with(responses, captured)

    dispatched: list[tuple[str, dict]] = []
    dispatch = _make_dispatch(dispatched)

    request = LLMTaskRequest(task_id="t6", context_payload="hi")

    with pytest.raises(OpenRouterApiError) as exc_info:
        await adapter.execute_with_tools(request, [_WEATHER_TOOL], dispatch)

    # a clear, adapter-level error — not a raw json.JSONDecodeError bubbling up
    assert "invalid JSON" in str(exc_info.value)
    assert not isinstance(exc_info.value, json.JSONDecodeError)
    # dispatch never ran for the malformed call
    assert dispatched == []


# --------------------------------------------------------------------------- #
# 7. max_tool_iterations exceeded -> raises, no infinite loop                  #
# --------------------------------------------------------------------------- #


async def test_max_tool_iterations_exceeded_raises():
    captured: list[dict] = []
    # A transport that ALWAYS returns a tool_calls response, never a final text.
    call_counter = {"i": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        call_counter["i"] += 1
        return httpx.Response(
            200,
            json=_assistant_tool_calls_response(
                [_tool_call(f"c{call_counter['i']}", "get_weather", '{"city": "X"}')]
            ),
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = OpenRouterAdapter(api_key="k", model="openai/gpt-4o", client=client)
    dispatch = _make_dispatch([])

    request = LLMTaskRequest(task_id="t7", context_payload="hi")

    with pytest.raises(OpenRouterApiError) as exc_info:
        await adapter.execute_with_tools(
            request, [_WEATHER_TOOL], dispatch, max_tool_iterations=3
        )

    assert "max_tool_iterations" in str(exc_info.value)
    # bounded: exactly the cap number of POSTs, not an infinite loop
    assert len(captured) == 3


# --------------------------------------------------------------------------- #
# 8. No model available -> ValueError                                         #
# --------------------------------------------------------------------------- #


async def test_no_model_raises_value_error():
    transport = httpx.MockTransport(
        lambda r: httpx.Response(200, json=_assistant_text_response())
    )
    adapter = OpenRouterAdapter(api_key="k", client=httpx.AsyncClient(transport=transport))
    dispatch = _make_dispatch([])

    request = LLMTaskRequest(task_id="t8", context_payload="hi")

    with pytest.raises(ValueError):
        await adapter.execute_with_tools(request, [_WEATHER_TOOL], dispatch)


# --------------------------------------------------------------------------- #
# 9. supports_tool_calling() is True                                          #
# --------------------------------------------------------------------------- #


async def test_supports_tool_calling_true():
    adapter = OpenRouterAdapter(api_key="k", model="openai/gpt-4o")
    assert adapter.supports_tool_calling() is True
