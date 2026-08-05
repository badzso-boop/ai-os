"""Rough pre-flight token + cost estimate for a decomposed epic DAG.

Shown at the plan-review gate so a user can gauge the spend BEFORE approving.
Deliberately labelled a *rough* estimate: the real token count depends on how
many retries + tool-use round-trips each task actually takes, which isn't known
up front — so treat this as an order-of-magnitude figure (easily ±2-3×), useful
for relative comparison (which tasks are expensive) and a ballpark total.

Two honest caveats baked in:
- **Anthropic CLI-session mode is subscription usage** — there is no per-token
  dollar cost, so those tasks are priced as `None` ("subscription") rather than
  a fake dollar figure.
- **Prices go stale.** `DEFAULT_PRICES` is an approximate table (USD per 1M
  tokens); override it with a `<AI_OS_HOME>/prices.json` file (same
  `{model_substr: [in, out]}` shape) for accurate numbers.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from ai_os.core.models import TaskNode
from ai_os.core.scheduler import DynamicScheduler
from ai_os.knowledge.graph_engine import KnowledgeEngine
from ai_os.mcp.adapters.base_adapter import BaseMCPAdapter

# Approximate USD per 1M tokens (input, output), matched by substring against the
# resolved model name. Rough as of writing; override via <AI_OS_HOME>/prices.json.
DEFAULT_PRICES: dict[str, tuple[float, float]] = {
    "haiku": (0.80, 4.00),
    "sonnet": (3.00, 15.00),
    "opus": (15.00, 75.00),
    "gemini-2.5-pro": (1.25, 10.00),
    "gemini-pro": (1.25, 10.00),
    "gemini-2.5-flash": (0.30, 2.50),
    "gemini-flash": (0.10, 0.40),
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4o": (2.50, 10.00),
    "deepseek": (0.27, 1.10),
    "qwen": (0.20, 0.80),
}


@dataclass(frozen=True)
class EstimatorConfig:
    """Tunable heuristics. Defaults are deliberately conservative-ish guesses;
    override via env for a project whose tasks run longer/shorter."""

    tokens_per_char: float = 0.27  # ~1 token per ~3.7 chars
    prompt_overhead_chars: int = 1500  # system prompt + task framing per turn
    output_tokens_per_turn: int = 800
    turns_per_task: float = 4.0  # tool-use round-trips + an expected retry

    @classmethod
    def from_env(cls, environ: dict[str, str] | None = None) -> "EstimatorConfig":
        environ = environ if environ is not None else os.environ

        def _f(key: str, default: float) -> float:
            try:
                return float(environ[key])
            except (KeyError, ValueError):
                return default

        return cls(
            tokens_per_char=_f("AI_OS_EST_TOKENS_PER_CHAR", cls.tokens_per_char),
            prompt_overhead_chars=int(_f("AI_OS_EST_PROMPT_OVERHEAD_CHARS", cls.prompt_overhead_chars)),
            output_tokens_per_turn=int(_f("AI_OS_EST_OUTPUT_TOKENS_PER_TURN", cls.output_tokens_per_turn)),
            turns_per_task=_f("AI_OS_EST_TURNS_PER_TASK", cls.turns_per_task),
        )


@dataclass
class TaskEstimate:
    task_id: str
    provider: str
    model: str | None
    input_tokens: int
    output_tokens: int
    usd: float | None  # None = subscription (Anthropic CLI session) or unpriced model


@dataclass
class EpicEstimate:
    per_task: list[TaskEstimate]
    total_input_tokens: int
    total_output_tokens: int
    total_usd: float  # sum over priced tasks only
    has_subscription: bool  # at least one task runs on subscription usage
    has_unpriced: bool  # at least one metered task has no price entry


def load_prices() -> dict[str, tuple[float, float]]:
    """`DEFAULT_PRICES` overlaid with an optional `<AI_OS_HOME>/prices.json`."""
    prices = dict(DEFAULT_PRICES)
    home = os.environ.get("AI_OS_HOME")
    path = (Path(home) if home else Path.home() / ".ai-os") / "prices.json"
    try:
        if path.is_file():
            raw = json.loads(path.read_text(encoding="utf-8"))
            for key, val in raw.items():
                if isinstance(val, (list, tuple)) and len(val) == 2:
                    prices[str(key).lower()] = (float(val[0]), float(val[1]))
    except (OSError, ValueError):
        pass
    return prices


def price_for(
    provider: str, model: str | None, adapter: BaseMCPAdapter | None,
    prices: dict[str, tuple[float, float]],
) -> tuple[float, float] | None:
    """Return (in, out) $/M for a (provider, model), or `None` when the task is
    subscription usage (Anthropic CLI session) or the model has no price entry."""
    if provider == "anthropic" and getattr(adapter, "use_cli_session", False):
        return None  # subscription — no per-token dollar cost
    if not model:
        return None
    lowered = model.lower()
    for key, price in prices.items():
        if key in lowered:
            return price
    return None


def estimate_epic(
    tasks: list[TaskNode],
    engine: KnowledgeEngine,
    scheduler: DynamicScheduler,
    adapters: dict[str, BaseMCPAdapter],
    config: EstimatorConfig | None = None,
) -> EpicEstimate:
    config = config or EstimatorConfig()
    prices = load_prices()
    per_task: list[TaskEstimate] = []
    total_in = total_out = 0
    total_usd = 0.0
    has_subscription = has_unpriced = False

    for task in tasks:
        assignment = scheduler.assign(task.risk_level)
        # The context cache is the real per-turn input the agent receives.
        try:
            cache = engine.build_context_cache(task.target_files, max_hops=2)
        except Exception:
            cache = ""
        input_chars = len(cache) + config.prompt_overhead_chars + len(task.description)
        input_per_turn = int(input_chars * config.tokens_per_char)
        input_tokens = int(input_per_turn * config.turns_per_task)
        output_tokens = int(config.output_tokens_per_turn * config.turns_per_task)

        price = price_for(assignment.provider, assignment.model, adapters.get(assignment.provider), prices)
        usd: float | None = None
        if price is not None:
            usd = input_tokens / 1_000_000 * price[0] + output_tokens / 1_000_000 * price[1]
            total_usd += usd
        elif assignment.provider == "anthropic" and getattr(
            adapters.get("anthropic"), "use_cli_session", False
        ):
            has_subscription = True
        else:
            has_unpriced = True

        per_task.append(TaskEstimate(
            task_id=task.id, provider=assignment.provider, model=assignment.model,
            input_tokens=input_tokens, output_tokens=output_tokens, usd=usd,
        ))
        total_in += input_tokens
        total_out += output_tokens

    return EpicEstimate(
        per_task=per_task,
        total_input_tokens=total_in,
        total_output_tokens=total_out,
        total_usd=total_usd,
        has_subscription=has_subscription,
        has_unpriced=has_unpriced,
    )
