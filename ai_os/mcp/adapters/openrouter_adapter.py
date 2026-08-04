"""OpenRouter MCP adapter (doc 07/15) — one endpoint, caller-chosen model.

OpenRouter (https://openrouter.ai) exposes an OpenAI-compatible Chat
Completions API that fronts many providers' models (Anthropic, OpenAI,
Google, Meta, DeepSeek, ...) behind one account/API key. Verified against
OpenRouter's official docs (fetched 2026-08-03):

- Endpoint: `POST https://openrouter.ai/api/v1/chat/completions`
  (https://openrouter.ai/docs/api-reference/overview,
  https://openrouter.ai/docs/api/api-reference/chat/send-chat-completion-request)
- Required headers: `Authorization: Bearer <key>`, `Content-Type: application/json`.
- Optional attribution headers (site ranking/leaderboard, no functional
  effect on the completion itself): `HTTP-Referer` and `X-Title` — these are
  the two names used throughout OpenRouter's own quickstart examples and
  every third-party SDK/integration surveyed (langchain, litellm, Helicone).
  OpenRouter's docs also mention a newer `X-OpenRouter-Title` alias for
  `X-Title`; `X-Title` is what we send since it's the long-standing,
  universally-recognized name.
- Response is OpenAI-compatible: `choices[0].message.content` is the
  generated text; `usage.prompt_tokens` / `usage.completion_tokens` map to
  `TokenUsage`. OpenRouter's `usage` object *may* additionally include a
  `cost` field (actual USD spent, since routed providers price differently)
  — mapped to `estimated_usd_cost` when present; left at the `TokenUsage`
  default of 0.0 when absent, rather than guessed at.
- Errors (https://openrouter.ai/docs/api_reference/errors-and-debugging):
  non-2xx responses carry a JSON body shaped
  `{"error": {"code": int, "message": str, "metadata": {...}}}` where
  `error.code` mirrors the HTTP status. An invalid/unavailable model ID
  surfaces as an HTTP 400/404 with that same shape (e.g.
  `error.metadata.error_type == "not_found"`) — handled the same way as any
  other non-2xx: raised as `OpenRouterApiError` with the parsed message,
  never an opaque `KeyError` from indexing a missing `choices`/`message`.

Unlike the Anthropic/Gemini adapters, this adapter deliberately has **no**
hardcoded default model. OpenRouter's entire value proposition is letting
the caller pick from many providers' models through one endpoint — silently
falling back to some arbitrary hardcoded model would defeat that. A model
must be supplied either per-request (`LLMTaskRequest.model`) or at
construction (`OpenRouterAdapter(..., model=...)`); if neither is set,
`execute_task` raises `ValueError`.
"""
from __future__ import annotations

import json

import httpx

from ai_os.mcp.adapters.base_adapter import (
    BaseMCPAdapter,
    LLMTaskRequest,
    LLMTaskResponse,
    ToolDispatch,
    ToolSpec,
    TokenUsage,
)

OPENROUTER_API_URL = "https://openrouter.ai/api/v1/chat/completions"


class OpenRouterApiError(RuntimeError):
    """An OpenRouter API call returned a non-2xx response or an unexpected
    response shape (e.g. missing `choices`/`message` on an ostensibly-2xx
    body).
    """

    def __init__(self, status_code: int, body: str) -> None:
        self.status_code = status_code
        self.body = body
        super().__init__(
            f"OpenRouter API request failed (HTTP {status_code}): {body[:500]}"
        )


class OpenRouterAdapter(BaseMCPAdapter):
    """Talks to any model OpenRouter routes to, via its OpenAI-compatible
    Chat Completions endpoint.

    `model` here is only a *fallback default* — per-request `LLMTaskRequest.model`
    always takes priority when set (see `execute_task`). If neither is
    provided, `execute_task` raises `ValueError` rather than silently picking
    a model, since there is no sensible default across OpenRouter's many
    providers.
    """

    def __init__(
        self,
        api_key: str,
        model: str | None = None,
        client: httpx.AsyncClient | None = None,
        site_url: str | None = None,
        app_title: str | None = None,
    ) -> None:
        self.api_key = api_key
        self.model = model
        self.site_url = site_url
        self.app_title = app_title
        # Injectable so tests can supply an `httpx.MockTransport`-backed
        # client instead of hitting the network; a real client is created
        # lazily (and reused) otherwise.
        self._client = client
        self._owns_client = client is None

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient()
        return self._client

    async def execute_task(self, request: LLMTaskRequest) -> LLMTaskResponse:
        model = request.model or self.model
        if not model:
            raise ValueError(
                "OpenRouterAdapter requires a model (per-request or at "
                "construction) — there is no sensible default across "
                "OpenRouter's many providers."
            )

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        # Optional attribution headers — see module docstring. Only sent
        # when actually configured; OpenRouter treats their absence as
        # perfectly normal (just no leaderboard attribution), so we don't
        # invent placeholder values.
        if self.site_url:
            headers["HTTP-Referer"] = self.site_url
        if self.app_title:
            headers["X-Title"] = self.app_title

        messages = []
        if request.system_prompt:
            messages.append({"role": "system", "content": request.system_prompt})
        messages.append({"role": "user", "content": request.context_payload})

        payload = {"model": model, "messages": messages}

        client = self._get_client()
        response = await client.post(OPENROUTER_API_URL, headers=headers, json=payload)

        if response.status_code // 100 != 2:
            raise OpenRouterApiError(response.status_code, response.text)

        try:
            body = response.json()
        except ValueError as exc:
            raise OpenRouterApiError(response.status_code, response.text) from exc

        choices = body.get("choices") or []
        if not choices or "message" not in choices[0]:
            raise OpenRouterApiError(response.status_code, response.text)

        generated_text = choices[0]["message"].get("content") or ""
        usage = body.get("usage") or {}

        return LLMTaskResponse(
            task_id=request.task_id,
            provider="openrouter",
            model_name=body.get("model", model),
            generated_text=generated_text,
            usage=TokenUsage(
                input_tokens=usage.get("prompt_tokens", 0),
                output_tokens=usage.get("completion_tokens", 0),
                # OpenRouter sometimes includes an actual-USD `cost` field on
                # `usage` (providers price differently); use it when present,
                # otherwise leave the TokenUsage default (0.0) rather than
                # guess from a hardcoded per-token price table that would go
                # stale the moment a routed provider repriced.
                estimated_usd_cost=usage.get("cost", 0.0),
            ),
        )

    def supports_tool_calling(self) -> bool:
        return True

    def _build_headers(self) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        # Optional attribution headers — see module docstring / `execute_task`.
        if self.site_url:
            headers["HTTP-Referer"] = self.site_url
        if self.app_title:
            headers["X-Title"] = self.app_title
        return headers

    @staticmethod
    def _usage_from_body(body: dict) -> TokenUsage:
        usage = body.get("usage") or {}
        return TokenUsage(
            input_tokens=usage.get("prompt_tokens", 0),
            output_tokens=usage.get("completion_tokens", 0),
            estimated_usd_cost=usage.get("cost", 0.0),
        )

    async def execute_with_tools(
        self,
        request: LLMTaskRequest,
        tools: list[ToolSpec],
        dispatch: ToolDispatch,
        max_tool_iterations: int = 25,
    ) -> LLMTaskResponse:
        """Autonomous OpenAI-compatible function-calling loop against the same
        OpenRouter chat/completions endpoint as `execute_task`.

        Wire format verified against OpenRouter's official tool-calling docs
        (https://openrouter.ai/docs/guides/features/tool-calling, fetched
        2026-08-03): request `tools[]` are `{"type":"function","function":
        {"name","description","parameters":<JSON Schema>}}`; the model asks to
        call tools via `choices[0].message.tool_calls[]` (each `{"id","type":
        "function","function":{"name","arguments":<JSON STRING>}}`) with
        `finish_reason == "tool_calls"`; tool results are fed back as
        `{"role":"tool","tool_call_id":<id>,"content":<text>}` messages, and
        the `tools` parameter is resent on every follow-up request.
        """
        model = request.model or self.model
        if not model:
            raise ValueError(
                "OpenRouterAdapter requires a model (per-request or at "
                "construction) — there is no sensible default across "
                "OpenRouter's many providers."
            )

        headers = self._build_headers()

        # Map each provider-neutral ToolSpec into OpenRouter's OpenAI-compatible
        # tool entry; `parameters` is the spec's JSON Schema verbatim.
        tool_payload = [
            {
                "type": "function",
                "function": {
                    "name": spec.name,
                    "description": spec.description,
                    "parameters": spec.json_schema,
                },
            }
            for spec in tools
        ]

        messages: list[dict] = []
        if request.system_prompt:
            messages.append({"role": "system", "content": request.system_prompt})
        messages.append({"role": "user", "content": request.context_payload})

        total_usage = TokenUsage()
        client = self._get_client()
        last_model_name = model

        for _ in range(max_tool_iterations):
            payload = {
                "model": model,
                "messages": messages,
                "tools": tool_payload,
                "tool_choice": "auto",
            }

            response = await client.post(
                OPENROUTER_API_URL, headers=headers, json=payload
            )
            if response.status_code // 100 != 2:
                raise OpenRouterApiError(response.status_code, response.text)

            try:
                body = response.json()
            except ValueError as exc:
                raise OpenRouterApiError(response.status_code, response.text) from exc

            total_usage = total_usage + self._usage_from_body(body)
            last_model_name = body.get("model", model)

            choices = body.get("choices") or []
            if not choices or "message" not in choices[0]:
                raise OpenRouterApiError(response.status_code, response.text)

            choice = choices[0]
            message = choice["message"]
            tool_calls = message.get("tool_calls") or []

            if not tool_calls:
                # A normal assistant answer — we're done.
                return LLMTaskResponse(
                    task_id=request.task_id,
                    provider="openrouter",
                    model_name=last_model_name,
                    generated_text=message.get("content") or "",
                    usage=total_usage,
                )

            # Append the assistant turn verbatim (it must carry its tool_calls
            # so the follow-up request keeps a consistent conversation).
            messages.append(message)

            for call in tool_calls:
                call_id = call.get("id")
                function = call.get("function") or {}
                name = function.get("name")
                raw_args = function.get("arguments")

                try:
                    parsed_args = json.loads(raw_args) if raw_args else {}
                except (ValueError, TypeError) as exc:
                    raise OpenRouterApiError(
                        response.status_code,
                        f"Model returned invalid JSON for tool '{name}' "
                        f"arguments: {raw_args!r} ({exc})",
                    ) from exc

                result_text = await dispatch(name, parsed_args)

                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call_id,
                        "content": result_text,
                    }
                )

        raise OpenRouterApiError(
            0,
            f"Tool-calling loop exceeded max_tool_iterations="
            f"{max_tool_iterations} without a final answer (model kept "
            f"requesting tools).",
        )
