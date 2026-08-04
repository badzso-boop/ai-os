"""Tests for `AnthropicAdapter.execute_with_tools` — the API-key-mode
autonomous tool-calling loop against Anthropic's Messages API.

Follows the project's "real behavior over mocks" philosophy: the adapter's
real request-building and response-parsing code runs unmodified; only the
socket is swapped via `httpx.MockTransport` (httpx's own sanctioned test
transport), driven by a SCRIPTED SEQUENCE of responses keyed on a call
counter. No test here makes a real network call, uses a real API key, or
invokes the real `claude` binary.

The mock-transport fixture mirrors `tests/test_anthropic_adapter.py`: it
monkeypatches the module's `httpx.AsyncClient` so the adapter's own
`httpx.AsyncClient()` construction routes through the mock transport.
"""
from __future__ import annotations

import json

import httpx
import pytest

import ai_os.mcp.adapters.anthropic_adapter as adapter_module
from ai_os.mcp.adapters.anthropic_adapter import (
    AnthropicAdapter,
    AnthropicApiError,
)
from ai_os.mcp.adapters.base_adapter import (
    LLMTaskRequest,
    ToolCallingNotSupported,
    ToolSpec,
)


@pytest.fixture()
def mock_httpx_transport(monkeypatch):
    """Route `httpx.AsyncClient()` (as constructed inside the adapter module)
    through a caller-supplied `httpx.MockTransport` handler. Returns a setter
    to install the handler for the test."""
    original_async_client = adapter_module.httpx.AsyncClient

    def _install(handler):
        transport = httpx.MockTransport(handler)

        def _client_factory(*args, **kwargs):
            kwargs["transport"] = transport
            return original_async_client(*args, **kwargs)

        monkeypatch.setattr(adapter_module.httpx, "AsyncClient", _client_factory)

    yield _install
    # monkeypatch reverts automatically on teardown


# -- response builders (verified Messages-API tool-use shape) -----------------


def _tool_use_response(
    *,
    tool_use_id: str,
    name: str,
    tool_input: dict,
    text: str | None = None,
    input_tokens: int = 10,
    output_tokens: int = 5,
) -> dict:
    content: list[dict] = []
    if text is not None:
        content.append({"type": "text", "text": text})
    content.append(
        {"type": "tool_use", "id": tool_use_id, "name": name, "input": tool_input}
    )
    return {
        "stop_reason": "tool_use",
        "content": content,
        "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens},
    }


def _multi_tool_use_response(
    *,
    calls: list[tuple[str, str, dict]],
    input_tokens: int = 10,
    output_tokens: int = 5,
) -> dict:
    """`calls` is a list of (tool_use_id, name, input) — all in one turn."""
    content = [
        {"type": "tool_use", "id": tid, "name": name, "input": inp}
        for tid, name, inp in calls
    ]
    return {
        "stop_reason": "tool_use",
        "content": content,
        "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens},
    }


def _end_turn_response(
    text: str, *, input_tokens: int = 7, output_tokens: int = 3
) -> dict:
    return {
        "stop_reason": "end_turn",
        "content": [{"type": "text", "text": text}],
        "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens},
    }


def _scripted_handler(responses: list[dict], captured: list[httpx.Request]):
    """Return a handler that plays `responses` in order, keyed on a counter,
    recording each incoming request into `captured`."""
    counter = {"i": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        i = counter["i"]
        counter["i"] += 1
        return httpx.Response(200, json=responses[i])

    return handler


SAMPLE_TOOLS = [
    ToolSpec(
        name="propose_file_patch",
        description="Write a patch into the worktree.",
        json_schema={
            "type": "object",
            "properties": {"path": {"type": "string"}, "contents": {"type": "string"}},
            "required": ["path", "contents"],
        },
    ),
    ToolSpec(
        name="fetch_symbol_definition",
        description="Look up a symbol's skeleton stub by FQN.",
        json_schema={
            "type": "object",
            "properties": {"fqn": {"type": "string"}},
            "required": ["fqn"],
        },
    ),
]


# -- 1. happy path: one tool call ---------------------------------------------


async def test_single_tool_call_round_trip(mock_httpx_transport):
    captured: list[httpx.Request] = []
    responses = [
        _tool_use_response(
            tool_use_id="toolu_1",
            name="fetch_symbol_definition",
            tool_input={"fqn": "src/foo.py::Foo.bar"},
        ),
        _end_turn_response("Done: patched Foo.bar."),
    ]
    mock_httpx_transport(_scripted_handler(responses, captured))

    dispatched: list[tuple[str, dict]] = []

    async def dispatch(name: str, args: dict) -> str:
        dispatched.append((name, args))
        return "stub: def bar(self): ..."

    adapter = AnthropicAdapter(api_key="test-key")
    request = LLMTaskRequest(task_id="T-1", context_payload="Fix Foo.bar")
    response = await adapter.execute_with_tools(request, SAMPLE_TOOLS, dispatch)

    # dispatch called exactly once, with the right (name, input)
    assert dispatched == [("fetch_symbol_definition", {"fqn": "src/foo.py::Foo.bar"})]

    # final text returned
    assert response.generated_text == "Done: patched Foo.bar."
    assert response.task_id == "T-1"
    assert response.provider == "anthropic"

    # two round-trips
    assert len(captured) == 2

    # the follow-up request carries the assistant turn (with the tool_use
    # block) AND a user turn with the matching tool_result
    follow_up = json.loads(captured[1].content)
    messages = follow_up["messages"]
    assert messages[0] == {"role": "user", "content": "Fix Foo.bar"}

    assistant_turn = messages[1]
    assert assistant_turn["role"] == "assistant"
    tool_use_blocks = [
        b for b in assistant_turn["content"] if b.get("type") == "tool_use"
    ]
    assert tool_use_blocks[0]["id"] == "toolu_1"
    assert tool_use_blocks[0]["name"] == "fetch_symbol_definition"

    user_turn = messages[2]
    assert user_turn["role"] == "user"
    assert user_turn["content"] == [
        {
            "type": "tool_result",
            "tool_use_id": "toolu_1",
            "content": "stub: def bar(self): ...",
        }
    ]


# -- 2. multiple sequential tool round-trips ----------------------------------


async def test_multiple_sequential_tool_round_trips(mock_httpx_transport):
    captured: list[httpx.Request] = []
    responses = [
        _tool_use_response(
            tool_use_id="toolu_1", name="fetch_symbol_definition", tool_input={"fqn": "a"}
        ),
        _tool_use_response(
            tool_use_id="toolu_2",
            name="propose_file_patch",
            tool_input={"path": "a.py", "contents": "x"},
        ),
        _end_turn_response("All set."),
    ]
    mock_httpx_transport(_scripted_handler(responses, captured))

    dispatched: list[tuple[str, dict]] = []

    async def dispatch(name: str, args: dict) -> str:
        dispatched.append((name, args))
        return f"result-for-{name}"

    adapter = AnthropicAdapter(api_key="test-key")
    request = LLMTaskRequest(task_id="T-2", context_payload="Do a then b")
    response = await adapter.execute_with_tools(request, SAMPLE_TOOLS, dispatch)

    assert dispatched == [
        ("fetch_symbol_definition", {"fqn": "a"}),
        ("propose_file_patch", {"path": "a.py", "contents": "x"}),
    ]
    assert response.generated_text == "All set."
    assert len(captured) == 3


# -- 3. two tool_use blocks in one assistant turn -----------------------------


async def test_two_tool_use_blocks_in_one_turn(mock_httpx_transport):
    captured: list[httpx.Request] = []
    responses = [
        _multi_tool_use_response(
            calls=[
                ("toolu_a", "fetch_symbol_definition", {"fqn": "a"}),
                ("toolu_b", "fetch_symbol_definition", {"fqn": "b"}),
            ]
        ),
        _end_turn_response("Both fetched."),
    ]
    mock_httpx_transport(_scripted_handler(responses, captured))

    dispatched: list[tuple[str, dict]] = []

    async def dispatch(name: str, args: dict) -> str:
        dispatched.append((name, args))
        return f"stub-{args['fqn']}"

    adapter = AnthropicAdapter(api_key="test-key")
    request = LLMTaskRequest(task_id="T-3", context_payload="Fetch a and b")
    response = await adapter.execute_with_tools(request, SAMPLE_TOOLS, dispatch)

    # both dispatched
    assert dispatched == [
        ("fetch_symbol_definition", {"fqn": "a"}),
        ("fetch_symbol_definition", {"fqn": "b"}),
    ]
    assert response.generated_text == "Both fetched."

    # both tool_result blocks in the single following user turn
    follow_up = json.loads(captured[1].content)
    user_turn = follow_up["messages"][2]
    assert user_turn["role"] == "user"
    assert user_turn["content"] == [
        {"type": "tool_result", "tool_use_id": "toolu_a", "content": "stub-a"},
        {"type": "tool_result", "tool_use_id": "toolu_b", "content": "stub-b"},
    ]


# -- 4. request shape: first request includes tools with input_schema ---------


async def test_first_request_includes_tools_schema(mock_httpx_transport):
    captured: list[httpx.Request] = []
    responses = [_end_turn_response("no tools needed")]
    mock_httpx_transport(_scripted_handler(responses, captured))

    async def dispatch(name: str, args: dict) -> str:  # pragma: no cover - unused
        raise AssertionError("dispatch should not be called")

    adapter = AnthropicAdapter(api_key="test-key")
    request = LLMTaskRequest(
        task_id="T-4", context_payload="hi", system_prompt="Be terse."
    )
    await adapter.execute_with_tools(request, SAMPLE_TOOLS, dispatch)

    body = json.loads(captured[0].content)
    assert body["system"] == "Be terse."
    assert body["max_tokens"] == 4096
    assert body["tools"] == [
        {
            "name": "propose_file_patch",
            "description": "Write a patch into the worktree.",
            "input_schema": SAMPLE_TOOLS[0].json_schema,
        },
        {
            "name": "fetch_symbol_definition",
            "description": "Look up a symbol's skeleton stub by FQN.",
            "input_schema": SAMPLE_TOOLS[1].json_schema,
        },
    ]
    # headers match the API-key mode contract
    assert captured[0].headers["x-api-key"] == "test-key"
    assert captured[0].headers["anthropic-version"] == "2023-06-01"


# -- 5. usage accumulation across round-trips ---------------------------------


async def test_usage_accumulated_across_round_trips(mock_httpx_transport):
    captured: list[httpx.Request] = []
    responses = [
        _tool_use_response(
            tool_use_id="toolu_1",
            name="fetch_symbol_definition",
            tool_input={"fqn": "a"},
            input_tokens=100,
            output_tokens=20,
        ),
        _end_turn_response("done", input_tokens=50, output_tokens=8),
    ]
    mock_httpx_transport(_scripted_handler(responses, captured))

    async def dispatch(name: str, args: dict) -> str:
        return "stub"

    adapter = AnthropicAdapter(api_key="test-key")
    request = LLMTaskRequest(task_id="T-5", context_payload="go")
    response = await adapter.execute_with_tools(request, SAMPLE_TOOLS, dispatch)

    assert response.usage.input_tokens == 150
    assert response.usage.output_tokens == 28
    # API path never sets a cost estimate
    assert response.usage.estimated_usd_cost == 0.0


# -- 6. max_tool_iterations exceeded -> AnthropicApiError, no infinite loop ----


async def test_max_tool_iterations_exceeded_raises(mock_httpx_transport):
    captured: list[httpx.Request] = []

    # A handler that NEVER stops asking for a tool.
    counter = {"i": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        counter["i"] += 1
        return httpx.Response(
            200,
            json=_tool_use_response(
                tool_use_id=f"toolu_{counter['i']}",
                name="fetch_symbol_definition",
                tool_input={"fqn": "loop"},
            ),
        )

    mock_httpx_transport(handler)

    dispatched: list[tuple[str, dict]] = []

    async def dispatch(name: str, args: dict) -> str:
        dispatched.append((name, args))
        return "stub"

    adapter = AnthropicAdapter(api_key="test-key")
    request = LLMTaskRequest(task_id="T-6", context_payload="loop forever")
    with pytest.raises(AnthropicApiError):
        await adapter.execute_with_tools(
            request, SAMPLE_TOOLS, dispatch, max_tool_iterations=3
        )

    # bounded: exactly 3 POSTs and 3 dispatches, then it gave up
    assert len(captured) == 3
    assert len(dispatched) == 3


# -- 7. CLI-session-mode instance raises a clear ValueError -------------------


async def test_execute_with_tools_on_cli_session_mode_raises_value_error():
    adapter = AnthropicAdapter(use_cli_session=True)

    async def dispatch(name: str, args: dict) -> str:  # pragma: no cover - unused
        raise AssertionError("dispatch should not be called")

    request = LLMTaskRequest(task_id="T-7", context_payload="hi")
    with pytest.raises(ValueError) as excinfo:
        await adapter.execute_with_tools(request, SAMPLE_TOOLS, dispatch)

    # A plain, clear ValueError — NOT the tool-calling-not-supported sentinel,
    # and it must not have tried to spawn the CLI.
    assert not isinstance(excinfo.value, ToolCallingNotSupported)
    assert "api" in str(excinfo.value).lower()


async def test_execute_with_tools_unconfigured_raises_value_error():
    adapter = AnthropicAdapter()  # neither api_key nor CLI

    async def dispatch(name: str, args: dict) -> str:  # pragma: no cover - unused
        raise AssertionError("dispatch should not be called")

    request = LLMTaskRequest(task_id="T-7b", context_payload="hi")
    with pytest.raises(ValueError):
        await adapter.execute_with_tools(request, SAMPLE_TOOLS, dispatch)


# -- 8. supports_tool_calling reflects mode -----------------------------------


def test_supports_tool_calling_true_in_api_key_mode():
    assert AnthropicAdapter(api_key="test-key").supports_tool_calling() is True


def test_supports_tool_calling_false_in_cli_session_mode():
    assert AnthropicAdapter(use_cli_session=True).supports_tool_calling() is False
    # Even if both are set, CLI-session takes priority and has no API loop.
    assert (
        AnthropicAdapter(api_key="test-key", use_cli_session=True).supports_tool_calling()
        is False
    )


def test_supports_tool_calling_false_when_unconfigured():
    assert AnthropicAdapter().supports_tool_calling() is False
