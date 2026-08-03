"""Shared contract for MCP provider adapters (doc 07/15).

Mirrors `ai_os.core.models`'s role in Phase 2: a small, shared pydantic
contract that concrete adapters (`anthropic_adapter.py`, `gemini_adapter.py`,
`openrouter_adapter.py`) and the router (`protocol_router.py`) all depend on,
without depending on each other.
"""
from __future__ import annotations

from abc import ABC, abstractmethod

from pydantic import BaseModel, Field


class TokenUsage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0
    estimated_usd_cost: float = 0.0


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


class BaseMCPAdapter(ABC):
    """One adapter instance talks to exactly one provider."""

    @abstractmethod
    async def execute_task(self, request: LLMTaskRequest) -> LLMTaskResponse: ...
