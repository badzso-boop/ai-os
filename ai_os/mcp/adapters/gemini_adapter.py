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

import httpx

from ai_os.mcp.adapters.base_adapter import (
    BaseMCPAdapter,
    LLMTaskRequest,
    LLMTaskResponse,
    TokenUsage,
)

GEMINI_API_BASE = "https://generativelanguage.googleapis.com/v1beta/models"


class GeminiApiError(RuntimeError):
    """A Gemini API call returned a non-2xx response or an unexpected response shape."""


class GeminiAdapter(BaseMCPAdapter):
    """One adapter instance talks to the Google AI Studio Gemini API via one API key."""

    def __init__(
        self,
        api_key: str,
        model: str = "gemini-3.5-flash-lite",
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.api_key = api_key
        self.model = model
        self.client = client if client is not None else httpx.AsyncClient(timeout=60.0)

    async def execute_task(self, request: LLMTaskRequest) -> LLMTaskResponse:
        model = request.model or self.model
        url = f"{GEMINI_API_BASE}/{model}:generateContent?key={self.api_key}"

        payload: dict = {
            "contents": [{"parts": [{"text": request.context_payload}]}],
        }
        if request.system_prompt:
            payload["systemInstruction"] = {"parts": [{"text": request.system_prompt}]}

        response = await self.client.post(url, json=payload)

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
