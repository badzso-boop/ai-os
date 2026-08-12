"""Google Gemini adapter (doc 07/15), Google AI Studio developer API-key mode only.

Only the official `generativelanguage.googleapis.com` developer API-key mode
(auth mode 1 in doc 15) is implemented here. The `gemini.google.com` web
session-cookie mode (auth mode 2) is explicitly out of scope: it scrapes a
private consumer web app's internal API and carries real ToS risk, so it is
not built.

Model default and current REST shape were verified against Google's official
Gemini API docs (ai.google.dev) rather than trusting doc 15's example, which
used the now-stale `gemini-1.5-flash` model id. As of verification:

- Endpoint: POST
  https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}
- Request body top-level fields: `contents` (list of {"parts": [{"text": ...}]})
  and, when a system prompt is supplied, a sibling top-level `systemInstruction`
  field shaped like `{"parts": [{"text": ...}]}` — NOT prepended into the user
  text, and NOT the snake_case `system_instruction` (Google's proto-JSON
  mapping for this API uses camelCase keys in the wire format).
- Response body: `candidates[0].content.parts[0].text` for the generated text,
  plus a top-level `usageMetadata` object with `promptTokenCount` /
  `candidatesTokenCount` (mapped into `TokenUsage`).
- Default model: `gemini-3.5-flash-lite`, the current cheap/fast "lite" model
  in the Flash family at verification time (the `gemini-2.x-flash` models
  suggested as a fallback default are being retired/phased out on Google's
  published deprecation schedule). Fully overridable via the constructor's
  `model` param or per-request via `request.model`, so a future rename is a
  one-line fix here, not a redesign.
"""
from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path

import httpx

from ai_os.mcp.adapters.base_adapter import (
    BaseMCPAdapter,
    LLMTaskRequest,
    LLMTaskResponse,
    TokenUsage,
    ToolDispatch,
    ToolSpec,
    raise_if_rate_limited,
)

GEMINI_API_BASE = "https://generativelanguage.googleapis.com/v1beta/models"

# Locks the CLI-session subprocess down to a pure prompt-in/text-out call —
# matches Anthropic adapter tool lockdown behavior.
DISALLOWED_TOOLS = "Bash Edit Write NotebookEdit Read Glob Grep WebFetch WebSearch"



class GeminiCliError(RuntimeError):
    """The `agy` CLI subprocess failed, or reported non-SUCCESS status."""

    def __init__(self, returncode: int, stderr: str) -> None:
        self.returncode = returncode
        self.stderr = stderr
        super().__init__(f"agy CLI failed (exit {returncode}): {stderr.strip()}")


class GeminiCliOutputError(RuntimeError):
    """The `agy` CLI's stdout wasn't parseable JSON."""

    def __init__(self, stdout: str) -> None:
        self.stdout = stdout
        super().__init__(
            f"agy CLI did not produce parseable JSON on stdout: {stdout[:500]!r}"
        )


class GeminiApiError(RuntimeError):
    """A Gemini API call returned a non-2xx response or an unexpected response shape."""


class GeminiAdapter(BaseMCPAdapter):
    """Talks to Google Gemini models, via the Antigravity CLI session or the Developer API.

    Supports two modes:
    1. CLI session mode (`use_cli_session=True`) — shells out to the `agy` CLI
       using the user's logged-in Google / Antigravity subscription.
    2. API-key mode (`api_key=...`) — direct HTTP requests to Google AI Studio API.
    """

    def __init__(
        self,
        api_key: str | None = None,
        use_cli_session: bool = False,
        model: str = "gemini-3.5-flash-lite",
        gemini_cli: str = "agy",
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.api_key = api_key
        self.use_cli_session = use_cli_session
        self.model = model
        self.gemini_cli = gemini_cli
        self.client = client if client is not None else httpx.AsyncClient(timeout=60.0)
        self._scratch_dir_path: Path | None = None

    def _scratch_dir(self) -> Path:
        if self._scratch_dir_path is None or not self._scratch_dir_path.exists():
            self._scratch_dir_path = Path(
                tempfile.mkdtemp(prefix="ai-os-gemini-")
            )
        return self._scratch_dir_path

    async def execute_task(self, request: LLMTaskRequest) -> LLMTaskResponse:
        if self.use_cli_session:
            return await self._execute_via_cli(request)
        if self.api_key:
            return await self._execute_via_api(request)
        raise ValueError(
            "GeminiAdapter requires either use_cli_session=True or an api_key"
        )

    # -- CLI session mode ---------------------------------------------------

    async def _execute_via_cli(self, request: LLMTaskRequest) -> LLMTaskResponse:
        model = request.model
        prompt = request.context_payload
        if request.system_prompt:
            prompt = f"System Instruction:\n{request.system_prompt}\n\nUser Context:\n{prompt}"

        argv: list[str] = [
            self.gemini_cli,
            "-p",
            prompt,
            "--output-format",
            "json",
            "--permission-mode",
            "plan",
            "--disallowedTools",
            DISALLOWED_TOOLS,
        ]
        if model:
            argv += ["--model", model]

        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=str(self._scratch_dir()),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        assert proc.returncode is not None
        stdout_text = stdout.decode()
        stderr_text = stderr.decode()

        if proc.returncode != 0:
            raise GeminiCliError(proc.returncode, stderr_text)

        try:
            payload = json.loads(stdout_text)
        except json.JSONDecodeError as exc:
            raise GeminiCliOutputError(stdout_text) from exc

        if payload.get("status") and payload.get("status") != "SUCCESS":
            raise GeminiCliError(0, stderr_text or json.dumps(payload))

        usage = payload.get("usage") or {}
        model_name = model or payload.get("model", "gemini-cli-session")
        return LLMTaskResponse(
            task_id=request.task_id,
            provider="gemini",
            model_name=model_name,
            generated_text=payload.get("response", ""),
            usage=TokenUsage(
                input_tokens=usage.get("input_tokens", 0),
                output_tokens=usage.get("output_tokens", 0),
                estimated_usd_cost=0.0,
            ),
        )

    # -- API-key mode --------------------------------------------------------

    async def _execute_via_api(self, request: LLMTaskRequest) -> LLMTaskResponse:
        model = request.model or self.model
        url = f"{GEMINI_API_BASE}/{model}:generateContent?key={self.api_key}"

        payload: dict = {
            "contents": [{"parts": [{"text": request.context_payload}]}],
        }
        if request.system_prompt:
            payload["systemInstruction"] = {"parts": [{"text": request.system_prompt}]}

        response = await self.client.post(url, json=payload)

        raise_if_rate_limited(response.status_code, response.headers, "gemini")
        if response.status_code < 200 or response.status_code >= 300:
            raise GeminiApiError(
                f"Gemini API returned HTTP {response.status_code} for model "
                f"'{model}': {response.text}"
            )

        try:
            data = response.json()
        except ValueError as exc:
            raise GeminiApiError(
                f"Gemini API returned a non-JSON response for model '{model}': {response.text}"
            ) from exc

        candidates = data.get("candidates") or []
        if not candidates:
            block_reason = (data.get("promptFeedback") or {}).get("blockReason")
            reason_suffix = f" (blockReason={block_reason!r})" if block_reason else ""
            raise GeminiApiError(
                f"Gemini API returned no candidates for model '{model}'{reason_suffix} — "
                "the prompt was likely blocked by safety filtering."
            )

        try:
            generated_text = candidates[0]["content"]["parts"][0]["text"]
        except (KeyError, IndexError, TypeError) as exc:
            raise GeminiApiError(
                f"Gemini API response for model '{model}' had an unexpected shape: "
                f"missing candidates[0].content.parts[0].text ({data!r})"
            ) from exc

        usage_meta = data.get("usageMetadata") or {}
        usage = TokenUsage(
            input_tokens=usage_meta.get("promptTokenCount", 0),
            output_tokens=usage_meta.get("candidatesTokenCount", 0),
            estimated_usd_cost=0.0,
        )

        return LLMTaskResponse(
            task_id=request.task_id,
            provider="gemini",
            model_name=model,
            generated_text=generated_text,
            usage=usage,
        )

    def supports_tool_calling(self) -> bool:
        return bool(self.api_key) and not self.use_cli_session

    async def _generate_content(self, model: str, payload: dict) -> dict:
        """One generateContent round-trip: POST + shared error handling.

        Returns the parsed JSON body. Raises GeminiApiError on a non-2xx
        response, a non-JSON body, or an empty/blocked `candidates` list — the
        same failure handling as `execute_task`, factored out for reuse by the
        tool-calling loop.
        """
        url = f"{GEMINI_API_BASE}/{model}:generateContent?key={self.api_key}"
        response = await self.client.post(url, json=payload)

        raise_if_rate_limited(response.status_code, response.headers, "gemini")
        if response.status_code < 200 or response.status_code >= 300:
            raise GeminiApiError(
                f"Gemini API returned HTTP {response.status_code} for model "
                f"'{model}': {response.text}"
            )

        try:
            data = response.json()
        except ValueError as exc:
            raise GeminiApiError(
                f"Gemini API returned a non-JSON response for model '{model}': {response.text}"
            ) from exc

        candidates = data.get("candidates") or []
        if not candidates:
            block_reason = (data.get("promptFeedback") or {}).get("blockReason")
            reason_suffix = f" (blockReason={block_reason!r})" if block_reason else ""
            raise GeminiApiError(
                f"Gemini API returned no candidates for model '{model}'{reason_suffix} — "
                "the prompt was likely blocked by safety filtering."
            )

        return data

    async def execute_with_tools(
        self,
        request: LLMTaskRequest,
        tools: list[ToolSpec],
        dispatch: ToolDispatch,
        max_tool_iterations: int = 25,
    ) -> LLMTaskResponse:
        """Gemini native function-calling agentic loop (API-key mode only)."""
        if self.use_cli_session:
            raise ValueError(
                "execute_with_tools is API-key-mode only. This adapter is in "
                "CLI-session mode."
            )
        if not self.api_key:
            raise ValueError(
                "execute_with_tools requires API-key mode, but this adapter has "
                "no api_key configured."
            )

        model = request.model or self.model

        function_declarations = [
            {
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.json_schema,
            }
            for tool in tools
        ]

        contents: list[dict] = [
            {"role": "user", "parts": [{"text": request.context_payload}]}
        ]

        base_payload: dict = {"tools": [{"functionDeclarations": function_declarations}]}
        if request.system_prompt:
            base_payload["systemInstruction"] = {
                "parts": [{"text": request.system_prompt}]
            }

        total_usage = TokenUsage()

        for _ in range(max_tool_iterations):
            payload = {**base_payload, "contents": contents}
            data = await self._generate_content(model, payload)

            usage_meta = data.get("usageMetadata") or {}
            total_usage = total_usage + TokenUsage(
                input_tokens=usage_meta.get("promptTokenCount", 0),
                output_tokens=usage_meta.get("candidatesTokenCount", 0),
                estimated_usd_cost=0.0,
            )

            candidate = data["candidates"][0]
            parts = ((candidate.get("content") or {}).get("parts")) or []

            function_calls = [p["functionCall"] for p in parts if "functionCall" in p]

            if not function_calls:
                text = "".join(p["text"] for p in parts if "text" in p)
                return LLMTaskResponse(
                    task_id=request.task_id,
                    provider="gemini",
                    model_name=model,
                    generated_text=text,
                    usage=total_usage,
                )

            # Append the model's turn verbatim (preserves functionCall parts,
            # including any text parts it emitted alongside them).
            contents.append({"role": "model", "parts": parts})

            # Dispatch every requested call, then append all their responses in a
            # single user turn before re-calling (handles parallel function calls).
            response_parts: list[dict] = []
            for call in function_calls:
                name = call["name"]
                args = call.get("args") or {}
                result_text = await dispatch(name, args)
                function_response: dict = {
                    "name": name,
                    "response": {"result": result_text},
                }
                if "id" in call:
                    function_response["id"] = call["id"]
                response_parts.append({"functionResponse": function_response})

            contents.append({"role": "user", "parts": response_parts})

        raise GeminiApiError(
            f"Gemini tool-calling loop exceeded max_tool_iterations="
            f"{max_tool_iterations} for model '{model}' without producing a final "
            "text answer (the model kept requesting tool calls)."
        )
