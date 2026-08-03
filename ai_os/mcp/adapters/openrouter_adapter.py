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

import httpx

from ai_os.mcp.adapters.base_adapter import (
    BaseMCPAdapter,
    LLMTaskRequest,
    LLMTaskResponse,
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
