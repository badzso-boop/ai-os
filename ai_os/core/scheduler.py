"""Dynamic Scheduler (doc 02 §2, doc 14's planned `core/scheduler.py`).

Maps a task's `risk_level` to a concrete (provider, model) assignment. Builds
on Phase 3a's `ProtocolRouter` (which already resolves risk_level -> the first
*configured* provider) and adds the missing half: which *model* within that
provider to use. This is what makes "assign cheaper tasks to cheaper models,
harder tasks to stronger models" real — e.g. for a user whose only provider is
the Anthropic CLI session, LOW -> haiku, MEDIUM/HIGH -> sonnet, CRITICAL ->
opus.

Deliberately still NOT the full doc 02 §2.2 Dynamic Scheduler: no TPM/RPM
tracking, no cost-based backoff/fallback (same deferral `protocol_router.py`
documents — premature without real usage data). This only adds model
selection on top of the existing provider selection.

The model matrix is overridable via env vars `AI_OS_MODEL_<PROVIDER>_<RISK>`
(e.g. `AI_OS_MODEL_ANTHROPIC_LOW=haiku`), so a user can retune it without
touching code. A matrix value of `None` means "use the adapter's own default
model" (the right behavior for Gemini, whose adapter already has a sensible
default, and the only honest option for OpenRouter, which has no safe default
across its many providers — configure `AI_OS_MODEL_OPENROUTER_<RISK>` or
`OPENROUTER_DEFAULT_MODEL` for those tasks).
"""
from __future__ import annotations

import os
from dataclasses import dataclass

from ai_os.mcp.protocol_router import ProtocolRouter

RISK_LEVELS = ("LOW", "MEDIUM", "HIGH", "CRITICAL")

# Anthropic model *aliases* (`haiku`/`sonnet`/`opus`) rather than pinned version
# ids: the `claude` CLI's --model accepts these aliases and always resolves them
# to the current release, so this matrix never goes stale on a model rename.
# Note: the aliases work for the CLI-session path (this user's setup); the
# Anthropic *API-key* path may need full model ids instead — override via
# AI_OS_MODEL_ANTHROPIC_<RISK> if you use api_key mode.
DEFAULT_MODEL_MATRIX: dict[str, dict[str, str | None]] = {
    "anthropic": {
        "LOW": "haiku",
        "MEDIUM": "sonnet",
        "HIGH": "sonnet",
        "CRITICAL": "opus",
    },
    # None -> GeminiAdapter's own default model (fine; it has a sensible one).
    "gemini": {level: None for level in RISK_LEVELS},
    # None -> no default; OpenRouter needs an explicit model per task. Set
    # AI_OS_MODEL_OPENROUTER_<RISK> (or OPENROUTER_DEFAULT_MODEL) to route
    # tasks here, otherwise the OpenRouterAdapter raises at execution time.
    "openrouter": {level: None for level in RISK_LEVELS},
}


@dataclass(frozen=True)
class Assignment:
    """A resolved routing decision for one task. `model=None` means "let the
    chosen provider's adapter use its own default model"."""

    provider: str
    model: str | None


class DynamicScheduler:
    def __init__(
        self,
        router: ProtocolRouter,
        model_matrix: dict[str, dict[str, str | None]] | None = None,
        environ: dict[str, str] | None = None,
    ) -> None:
        self.router = router
        self.model_matrix = self._apply_env_overrides(
            model_matrix or _deep_copy_matrix(DEFAULT_MODEL_MATRIX),
            environ if environ is not None else os.environ,
        )

    @staticmethod
    def _apply_env_overrides(
        matrix: dict[str, dict[str, str | None]], environ
    ) -> dict[str, dict[str, str | None]]:
        for provider in list(matrix) + ["anthropic", "gemini", "openrouter"]:
            matrix.setdefault(provider, {})
            for level in RISK_LEVELS:
                key = f"AI_OS_MODEL_{provider.upper()}_{level}"
                value = environ.get(key)
                if value:
                    matrix[provider][level] = value
        return matrix

    def assign(self, risk_level: str) -> Assignment:
        """Resolve `risk_level` to a concrete (provider, model). The provider
        comes from the existing `ProtocolRouter` (first configured provider in
        that risk level's preference order — raises `NoAdapterConfiguredError`
        if none is configured); the model comes from the matrix.
        """
        provider = self.router.resolve_provider(risk_level)
        model = self.model_matrix.get(provider, {}).get(risk_level)
        return Assignment(provider=provider, model=model)

    def planning_assignment(self) -> Assignment:
        """Decomposition/planning is architectural work — route it as CRITICAL
        so it lands on the strongest configured model."""
        return self.assign("CRITICAL")


def _deep_copy_matrix(matrix: dict[str, dict[str, str | None]]) -> dict[str, dict[str, str | None]]:
    return {provider: dict(levels) for provider, levels in matrix.items()}
