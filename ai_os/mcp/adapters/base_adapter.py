"""Shared contract for MCP provider adapters (doc 07/15).

Mirrors `ai_os.core.models`'s role in Phase 2: a small, shared pydantic
contract that concrete adapters (`anthropic_adapter.py`, `gemini_adapter.py`,
`openrouter_adapter.py`) and the router (`protocol_router.py`) all depend on,
without depending on each other.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Awaitable, Callable

from pydantic import BaseModel, Field


class TokenUsage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0
    estimated_usd_cost: float = 0.0

    def __add__(self, other: "TokenUsage") -> "TokenUsage":
        """Accumulate usage across the multiple round-trips of an agentic
        tool-calling loop (`execute_with_tools`)."""
        return TokenUsage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            estimated_usd_cost=self.estimated_usd_cost + other.estimated_usd_cost,
        )


class LLMTaskRequest(BaseModel):
    task_id: str
    system_prompt: str = ""
    context_payload: str
    model: str | None = None  # overrides the adapter's configured default model


class LLMTaskResponse(BaseModel):
    task_id: str
    provider: str
    model_name: str
    generated_text: str
    usage: TokenUsage = Field(default_factory=TokenUsage)


class ToolSpec(BaseModel):
    """Provider-neutral description of a tool the model may call. Each adapter's
    `execute_with_tools` translates this into its provider's own function-calling
    schema (Anthropic `tools`, OpenAI/OpenRouter `tools`, Gemini
    `functionDeclarations`)."""

    name: str
    description: str
    json_schema: dict  # JSON Schema for the tool's arguments object


# Runs one tool call and returns its textual result. The executor supplies a
# dispatcher backed by the real MCP tool implementations
# (`ai_os.mcp.mcp_server.dispatch_tool_call`), so every provider reuses the
# same propose_file_patch/fetch_symbol_definition/trigger_sandbox_validation
# logic — no per-provider tool reimplementation.
ToolDispatch = Callable[[str, dict], Awaitable[str]]


class ToolCallingNotSupported(NotImplementedError):
    """Raised by an adapter that has no autonomous tool-calling loop (so callers
    fall back to a completion strategy rather than silently getting no tools)."""


class BaseMCPAdapter(ABC):
    """One adapter instance talks to exactly one provider."""

    @abstractmethod
    async def execute_task(self, request: LLMTaskRequest) -> LLMTaskResponse: ...

    async def execute_with_tools(
        self,
        request: LLMTaskRequest,
        tools: list[ToolSpec],
        dispatch: ToolDispatch,
        max_tool_iterations: int = 25,
    ) -> LLMTaskResponse:
        """Run an autonomous tool-calling loop: send the prompt + `tools`, and
        whenever the model asks to call a tool, run it via `dispatch`, feed the
        result back, and continue until the model produces a final answer (or
        `max_tool_iterations` is hit). Returns the final text plus `TokenUsage`
        summed across every round-trip.

        Default: not supported — an adapter without a real loop raises
        `ToolCallingNotSupported` so the caller can fall back to a completion
        strategy instead of silently losing tool access.
        """
        raise ToolCallingNotSupported(f"{type(self).__name__} has no tool-calling loop")

    def supports_tool_calling(self) -> bool:
        """Whether `execute_with_tools` is really implemented (overridden to
        True by adapters that implement the loop)."""
        return False
