"""Thin MCP provider router (doc 02 §2, doc 07).

Deliberately NOT the full Dynamic Scheduler from doc 02 §2.2 — no TPM/RPM
tracking, no cost-based backoff/fallback. Building real rate-limit-aware
scheduling without real usage data to tune it against would be premature;
that's future work once this project actually has live traffic patterns to
look at. This is just a registry of configured adapters plus a static,
overridable risk-level -> provider preference order.
"""
from __future__ import annotations

import os

from ai_os.mcp.adapters.base_adapter import BaseMCPAdapter, LLMTaskRequest, LLMTaskResponse

RISK_LEVELS = ("LOW", "MEDIUM", "HIGH", "CRITICAL")

# doc 02 §2.1's risk matrix, narrowed to the providers this project actually
# integrates with (no OpenAI/local adapters yet — see CLAUDE.md). Each list is
# tried in order; the first provider that's actually configured wins. Tuned for
# cost: cheaper providers first for LOW/MEDIUM, the strongest last for
# HIGH/CRITICAL. Overridable per risk level via `AI_OS_PROVIDER_ORDER_<RISK>`
# (see `risk_provider_order_from_env`).
DEFAULT_RISK_PROVIDER_ORDER: dict[str, list[str]] = {
    "LOW": ["gemini", "openrouter", "anthropic"],
    "MEDIUM": ["openrouter", "gemini", "anthropic"],
    "HIGH": ["anthropic", "openrouter"],
    "CRITICAL": ["anthropic"],
}


def risk_provider_order_from_env(environ: dict[str, str] | None = None) -> dict[str, list[str]]:
    """Builds the risk-level -> provider preference order, overlaying any
    `AI_OS_PROVIDER_ORDER_<RISK>` env vars (comma-separated provider names,
    e.g. `AI_OS_PROVIDER_ORDER_MEDIUM=openrouter,anthropic`) on top of
    `DEFAULT_RISK_PROVIDER_ORDER`. A named provider that isn't actually
    configured is harmlessly skipped at resolution time, so a typo or an
    absent provider never breaks routing — it just falls through to the next
    one in the list.
    """
    environ = environ if environ is not None else os.environ
    order = {level: list(providers) for level, providers in DEFAULT_RISK_PROVIDER_ORDER.items()}
    for level in RISK_LEVELS:
        raw = environ.get(f"AI_OS_PROVIDER_ORDER_{level}")
        if raw:
            parsed = [p.strip() for p in raw.split(",") if p.strip()]
            if parsed:
                order[level] = parsed
    return order


class NoAdapterConfiguredError(RuntimeError):
    """No configured adapter is available for the requested provider/risk level."""


class ProtocolRouter:
    def __init__(
        self,
        adapters: dict[str, BaseMCPAdapter],
        risk_provider_order: dict[str, list[str]] | None = None,
    ) -> None:
        self.adapters = adapters
        self.risk_provider_order = risk_provider_order or DEFAULT_RISK_PROVIDER_ORDER

    def resolve_provider(self, risk_level: str) -> str:
        """Returns the first configured provider name in this risk level's
        preference order. Raises NoAdapterConfiguredError if none of the
        preferred providers for `risk_level` are actually configured.
        """
        candidates = self.risk_provider_order.get(risk_level, [])
        for provider in candidates:
            if provider in self.adapters:
                return provider
        raise NoAdapterConfiguredError(
            f"No configured adapter available for risk level {risk_level!r} "
            f"(tried: {candidates}, configured: {sorted(self.adapters)})"
        )

    async def execute(self, provider: str, request: LLMTaskRequest) -> LLMTaskResponse:
        """Explicit-provider-choice execution, bypassing risk-level routing."""
        adapter = self.adapters.get(provider)
        if adapter is None:
            raise NoAdapterConfiguredError(
                f"Provider {provider!r} is not configured (configured: {sorted(self.adapters)})"
            )
        return await adapter.execute_task(request)

    async def execute_for_risk(self, risk_level: str, request: LLMTaskRequest) -> LLMTaskResponse:
        provider = self.resolve_provider(risk_level)
        return await self.execute(provider, request)
