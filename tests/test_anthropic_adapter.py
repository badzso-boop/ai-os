"""Tests for `ai_os.mcp.adapters.anthropic_adapter.AnthropicAdapter`.

Follows the project's "real behavior over mocks" testing philosophy: the
CLI-session path is exercised against a real (fake, but really executed)
subprocess — a small Python script written to `tmp_path` and invoked via
`asyncio.create_subprocess_exec` exactly as the adapter would invoke the
real `claude` binary — never a mock of the adapter's own subprocess-calling
code. The API-key path is exercised via `httpx.MockTransport`, httpx's own
sanctioned test transport: real request-building/response-parsing code
runs, only the socket is swapped.

No test in this file makes a real network call or invokes the real `claude`
CLI.
"""
from __future__ import annotations

import json
import stat
from pathlib import Path

import httpx
import pytest

import ai_os.mcp.adapters.anthropic_adapter as adapter_module
from ai_os.mcp.adapters.anthropic_adapter import (
    AnthropicAdapter,
    AnthropicApiError,
    AnthropicCliError,
    AnthropicCliOutputError,
    DISALLOWED_TOOLS,
)
from ai_os.mcp.adapters.base_adapter import LLMTaskRequest

VERIFIED_SHAPE = {
    "is_error": False,
    "duration_api_ms": 4135,
    "num_turns": 1,
    "stop_reason": "end_turn",
    "session_id": "6ce65c6c-test-session",
    "total_cost_usd": 0.0326199,
    "usage": {
        "input_tokens": 10,
        "cache_creation_input_tokens": 15334,
        "cache_read_input_tokens": 7179,
        "output_tokens": 127,
    },
    "result": "pong",
    "type": "result",
}


@pytest.fixture()
def mock_httpx_transport(monkeypatch):
    """Redirect `httpx.AsyncClient()` (as constructed inside the adapter
    module) through a caller-supplied `httpx.MockTransport` handler.

    Returns a setter function: call it with the handler to install for the
    test. Using `httpx.MockTransport` — httpx's own sanctioned test
    transport — means the adapter's real request-building and
    response-parsing code runs unmodified; only the socket is swapped.
    """
    original_async_client = adapter_module.httpx.AsyncClient

    def _install(handler):
        transport = httpx.MockTransport(handler)

        def _client_factory(*args, **kwargs):
            kwargs["transport"] = transport
            return original_async_client(*args, **kwargs)

        monkeypatch.setattr(adapter_module.httpx, "AsyncClient", _client_factory)

    yield _install
    # monkeypatch fixture reverts automatically on teardown


def _write_fake_cli(tmp_path: Path, script_body: str) -> Path:
    """Write an executable fake `claude` CLI stub to `tmp_path`."""
    script_path = tmp_path / "fake_claude.py"
    script_path.write_text(script_body)
    script_path.chmod(script_path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return script_path


def _argv_capturing_stub(tmp_path: Path, response_json: dict, exit_code: int = 0) -> Path:
    """A fake CLI that records its argv to `tmp_path/argv.json` and prints
    canned JSON to stdout, matching the verified real-CLI output shape.
    """
    argv_log = tmp_path / "argv.json"
    response_text = json.dumps(response_json)
    script = f"""#!/usr/bin/env python3
import json
import sys

with open({str(argv_log)!r}, "w") as f:
    json.dump(sys.argv, f)

print({response_text!r})
sys.exit({exit_code})
"""
    return _write_fake_cli(tmp_path, script)


def _read_argv_log(tmp_path: Path) -> list[str]:
    return json.loads((tmp_path / "argv.json").read_text())


# -- CLI-session mode: happy path --------------------------------------------


async def test_cli_session_parses_successful_response(tmp_path):
    stub = _argv_capturing_stub(tmp_path, VERIFIED_SHAPE)
    adapter = AnthropicAdapter(claude_cli=str(stub), use_cli_session=True)

    request = LLMTaskRequest(task_id="t1", context_payload="Reply with exactly the word: pong")
    response = await adapter.execute_task(request)

    assert response.task_id == "t1"
    assert response.provider == "anthropic"
    assert response.generated_text == "pong"
    assert response.usage.input_tokens == 10
    assert response.usage.output_tokens == 127
    assert response.usage.estimated_usd_cost == pytest.approx(0.0326199)


async def test_cli_session_builds_locked_down_argv(tmp_path):
    stub = _argv_capturing_stub(tmp_path, VERIFIED_SHAPE)
    adapter = AnthropicAdapter(claude_cli=str(stub), use_cli_session=True)

    request = LLMTaskRequest(task_id="t1", context_payload="hello")
    await adapter.execute_task(request)

    argv = _read_argv_log(tmp_path)
    assert "--permission-mode" in argv
    assert argv[argv.index("--permission-mode") + 1] == "plan"
    assert "--disallowedTools" in argv
    assert argv[argv.index("--disallowedTools") + 1] == DISALLOWED_TOOLS
    assert "-p" in argv
    assert argv[argv.index("-p") + 1] == "hello"
    assert "--output-format" in argv
    assert argv[argv.index("--output-format") + 1] == "json"


async def test_cli_session_passes_system_prompt_when_present(tmp_path):
    stub = _argv_capturing_stub(tmp_path, VERIFIED_SHAPE)
    adapter = AnthropicAdapter(claude_cli=str(stub), use_cli_session=True)

    request = LLMTaskRequest(
        task_id="t1", context_payload="hello", system_prompt="You are terse."
    )
    await adapter.execute_task(request)

    argv = _read_argv_log(tmp_path)
    assert "--system-prompt" in argv
    assert argv[argv.index("--system-prompt") + 1] == "You are terse."


async def test_cli_session_omits_system_prompt_flag_when_empty(tmp_path):
    stub = _argv_capturing_stub(tmp_path, VERIFIED_SHAPE)
    adapter = AnthropicAdapter(claude_cli=str(stub), use_cli_session=True)

    request = LLMTaskRequest(task_id="t1", context_payload="hello")
    await adapter.execute_task(request)

    argv = _read_argv_log(tmp_path)
    assert "--system-prompt" not in argv


async def test_cli_session_uses_request_model_override(tmp_path):
    stub = _argv_capturing_stub(tmp_path, VERIFIED_SHAPE)
    adapter = AnthropicAdapter(
        claude_cli=str(stub), use_cli_session=True, model="claude-sonnet-4-5"
    )

    request = LLMTaskRequest(task_id="t1", context_payload="hello", model="claude-haiku-4-5")
    response = await adapter.execute_task(request)

    argv = _read_argv_log(tmp_path)
    assert argv[argv.index("--model") + 1] == "claude-haiku-4-5"
    assert response.model_name == "claude-haiku-4-5"


async def test_cli_session_defaults_to_adapter_model(tmp_path):
    stub = _argv_capturing_stub(tmp_path, VERIFIED_SHAPE)
    adapter = AnthropicAdapter(
        claude_cli=str(stub), use_cli_session=True, model="claude-sonnet-4-5"
    )

    request = LLMTaskRequest(task_id="t1", context_payload="hello")
    response = await adapter.execute_task(request)

    argv = _read_argv_log(tmp_path)
    assert argv[argv.index("--model") + 1] == "claude-sonnet-4-5"
    assert response.model_name == "claude-sonnet-4-5"


# -- CLI-session mode: error paths --------------------------------------------


async def test_cli_session_nonzero_exit_raises(tmp_path):
    script = """#!/usr/bin/env python3
import sys
sys.stderr.write("boom: something broke\\n")
sys.exit(1)
"""
    stub = _write_fake_cli(tmp_path, script)
    adapter = AnthropicAdapter(claude_cli=str(stub), use_cli_session=True)

    request = LLMTaskRequest(task_id="t1", context_payload="hello")
    with pytest.raises(AnthropicCliError) as excinfo:
        await adapter.execute_task(request)
    assert excinfo.value.returncode == 1
    assert "boom" in excinfo.value.stderr


async def test_cli_session_is_error_true_raises(tmp_path):
    error_payload = dict(VERIFIED_SHAPE, is_error=True, result="")
    stub = _argv_capturing_stub(tmp_path, error_payload)
    adapter = AnthropicAdapter(claude_cli=str(stub), use_cli_session=True)

    request = LLMTaskRequest(task_id="t1", context_payload="hello")
    with pytest.raises(AnthropicCliError):
        await adapter.execute_task(request)


async def test_cli_session_malformed_stdout_raises_clear_error(tmp_path):
    script = """#!/usr/bin/env python3
import sys
print("not json at all {{{")
sys.exit(0)
"""
    stub = _write_fake_cli(tmp_path, script)
    adapter = AnthropicAdapter(claude_cli=str(stub), use_cli_session=True)

    request = LLMTaskRequest(task_id="t1", context_payload="hello")
    with pytest.raises(AnthropicCliOutputError) as excinfo:
        await adapter.execute_task(request)
    assert "not json at all" in excinfo.value.stdout


# -- API-key mode: happy path --------------------------------------------------


async def test_api_key_mode_sends_correct_request_and_parses_response(mock_httpx_transport):
    captured_requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured_requests.append(request)
        return httpx.Response(
            200,
            json={
                "content": [{"type": "text", "text": "hello from claude"}],
                "usage": {"input_tokens": 12, "output_tokens": 34},
            },
        )

    mock_httpx_transport(handler)
    adapter = AnthropicAdapter(api_key="sk-ant-test-key")

    request = LLMTaskRequest(task_id="t2", context_payload="Hi", system_prompt="Be terse.")
    response = await adapter.execute_task(request)

    assert response.task_id == "t2"
    assert response.provider == "anthropic"
    assert response.model_name == "claude-sonnet-4-5"
    assert response.generated_text == "hello from claude"
    assert response.usage.input_tokens == 12
    assert response.usage.output_tokens == 34

    assert len(captured_requests) == 1
    sent = captured_requests[0]
    assert sent.url == httpx.URL("https://api.anthropic.com/v1/messages")
    assert sent.headers["x-api-key"] == "sk-ant-test-key"
    assert sent.headers["anthropic-version"] == "2023-06-01"
    assert sent.headers["content-type"] == "application/json"
    body = json.loads(sent.content)
    assert body["model"] == "claude-sonnet-4-5"
    assert body["max_tokens"] == 4096
    assert body["system"] == "Be terse."
    assert body["messages"] == [{"role": "user", "content": "Hi"}]


async def test_api_key_mode_uses_request_model_override(mock_httpx_transport):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "content": [{"type": "text", "text": "ok"}],
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
        )

    mock_httpx_transport(handler)
    adapter = AnthropicAdapter(api_key="sk-ant-test-key", model="claude-sonnet-4-5")

    request = LLMTaskRequest(task_id="t3", context_payload="Hi", model="claude-haiku-4-5")
    response = await adapter.execute_task(request)

    assert response.model_name == "claude-haiku-4-5"


# -- API-key mode: error paths -------------------------------------------------


async def test_api_key_mode_4xx_raises_clear_error(mock_httpx_transport):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            401,
            json={"error": {"type": "authentication_error", "message": "invalid key"}},
        )

    mock_httpx_transport(handler)
    adapter = AnthropicAdapter(api_key="bad-key")

    request = LLMTaskRequest(task_id="t4", context_payload="Hi")
    with pytest.raises(AnthropicApiError) as excinfo:
        await adapter.execute_task(request)
    assert excinfo.value.status_code == 401


async def test_api_key_mode_5xx_raises_clear_error(mock_httpx_transport):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": {"type": "api_error", "message": "oops"}})

    mock_httpx_transport(handler)
    adapter = AnthropicAdapter(api_key="sk-ant-test-key")

    request = LLMTaskRequest(task_id="t5", context_payload="Hi")
    with pytest.raises(AnthropicApiError) as excinfo:
        await adapter.execute_task(request)
    assert excinfo.value.status_code == 500


# -- Neither mode configured ---------------------------------------------------


async def test_execute_task_raises_value_error_when_unconfigured():
    adapter = AnthropicAdapter()
    request = LLMTaskRequest(task_id="t6", context_payload="Hi")
    with pytest.raises(ValueError):
        await adapter.execute_task(request)
