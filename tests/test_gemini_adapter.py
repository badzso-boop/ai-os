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


# -- CLI-session mode tests (issue #17: tool-lockdown asymmetry) -------------


def _write_fake_agy(tmp_path, script_body: str):
    import stat

    script_path = tmp_path / "fake_agy.py"
    script_path.write_text(script_body)
    script_path.chmod(script_path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return script_path


def _argv_capturing_stub(tmp_path, response_json: dict, exit_code: int = 0):
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
    return _write_fake_agy(tmp_path, script)


def _read_argv_log(tmp_path) -> list[str]:
    return json.loads((tmp_path / "argv.json").read_text())


async def test_cli_session_builds_locked_down_argv(tmp_path):
    """`agy` genuinely supports a plan/read-only mode and a sandbox flag —
    verified against its real `--help` output (issue #17) — so the
    CLI-session executor must use exactly those, not the fabricated
    `--disallowedTools` flag `agy` doesn't have (which a prior, closed fix
    attempt used, and which `agy` would reject outright as unrecognized).
    """
    from ai_os.mcp.adapters.gemini_adapter import GEMINI_CLI_SESSION_LOCKDOWN_FLAGS

    fake_response = {
        "status": "SUCCESS",
        "response": "hello from cli",
        "usage": {"input_tokens": 10, "output_tokens": 20},
    }
    stub = _argv_capturing_stub(tmp_path, fake_response)
    adapter = GeminiAdapter(gemini_cli=str(stub), use_cli_session=True)

    request = LLMTaskRequest(task_id="t10", context_payload="hello")
    response = await adapter.execute_task(request)

    assert response.generated_text == "hello from cli"
    argv = _read_argv_log(tmp_path)

    assert "--mode" in argv
    assert argv[argv.index("--mode") + 1] == "plan"
    assert "--sandbox" in argv
    assert "-p" in argv
    assert argv[argv.index("-p") + 1] == "hello"
    assert "--output-format" in argv
    assert argv[argv.index("--output-format") + 1] == "json"

    # The old, wrong lockdown attempt must be fully gone.
    assert "accept-edits" not in argv
    assert "--dangerously-skip-permissions" not in argv
    assert "--disallowedTools" not in argv

    # Every flag GEMINI_CLI_SESSION_LOCKDOWN_FLAGS declares actually landed
    # in argv, contiguously, so the constant and the real invocation can't
    # drift apart silently.
    joined = " ".join(argv)
    assert " ".join(GEMINI_CLI_SESSION_LOCKDOWN_FLAGS) in joined


def test_cli_session_selection_logs_reduced_isolation_warning(caplog):
    """Operators must see, at runtime (not just in a code comment), that the
    Gemini CLI-session path has coarser tool isolation than Anthropic's
    (no --disallowedTools equivalent in `agy`).
    """
    with caplog.at_level("WARNING", logger="ai_os.mcp.adapters.gemini_adapter"):
        GeminiAdapter(gemini_cli="agy", use_cli_session=True)

    assert any(
        "agy" in record.getMessage() and "disallowedTools" in record.getMessage()
        for record in caplog.records
    )


def test_api_key_mode_selection_does_not_log_warning(caplog):
    with caplog.at_level("WARNING", logger="ai_os.mcp.adapters.gemini_adapter"):
        GeminiAdapter(api_key="test-api-key", use_cli_session=False)

    assert not caplog.records
