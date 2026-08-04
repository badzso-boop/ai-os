"""Adaptive rate-limit + cost-aware scheduling (doc 02 §2.2, Phase 5 Stage 4).

Deliberately ADAPTIVE, not hardcoded TPM/RPM numbers. For the Claude
*subscription* CLI path there is no clean per-minute token figure to police;
for the metered HTTP providers the real, honest signal is an HTTP 429 (with an
optional `Retry-After`). So instead of guessing rate limits up front, this
reacts to the limits the providers actually report:

- **Backoff**: on a `RateLimitedError` (raised by the adapters on a 429), wait
  — honoring `Retry-After` when present, else exponential — and retry the same
  provider a bounded number of times.
- **Provider fallback**: if a provider keeps rate-limiting past that budget,
  move the task to the NEXT configured provider in its risk order (this is why
  `risk_provider_order` / `assignments_in_order` are ordered *lists*).
- **Cost cap**: an optional `AI_OS_EPIC_BUDGET_USD` — before dispatching a
  task, the epic's spend so far (from Stage 3's `TokenCostModel` rows, via
  `Persistence.epic_total_usd`) is checked, and the run stops with the rest of
  the tasks SKIPPED if the cap is (or would be) blown.

Still explicitly NOT a full distributed rate-limiter: in-process, single-run
scope, which is all `ai-os epic run` needs.
"""
from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional, TypeVar

from ai_os.mcp.adapters.base_adapter import RateLimitedError

T = TypeVar("T")

# A no-arg async attempt (e.g. `lambda: executor(ctx)`), retried on rate-limit.
AttemptFn = Callable[[], Awaitable[T]]


class BudgetExceededError(RuntimeError):
    """The epic's spend reached the configured `AI_OS_EPIC_BUDGET_USD` cap.
    `EpicRunner` catches this and marks the remaining tasks SKIPPED."""

    def __init__(self, spent_usd: float, budget_usd: float) -> None:
        self.spent_usd = spent_usd
        self.budget_usd = budget_usd
        super().__init__(
            f"epic budget exceeded: spent ${spent_usd:.4f} >= cap ${budget_usd:.4f}"
        )


@dataclass
class SchedulingPolicy:
    """Rate-limit backoff + provider fallback + optional cost cap. `sleep` is
    injectable so tests assert backoff timing without real waiting."""

    max_rate_limit_retries: int = 3
    base_backoff_seconds: float = 1.0
    budget_usd: Optional[float] = None
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep

    @classmethod
    def from_env(
        cls,
        environ: Optional[dict[str, str]] = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> "SchedulingPolicy":
        environ = environ if environ is not None else os.environ
        raw_budget = environ.get("AI_OS_EPIC_BUDGET_USD")
        budget = None
        if raw_budget:
            try:
                budget = float(raw_budget)
            except ValueError:
                budget = None  # a malformed cap is ignored, not fatal
        raw_retries = environ.get("AI_OS_RATE_LIMIT_RETRIES")
        retries = 3
        if raw_retries:
            try:
                retries = max(0, int(raw_retries))
            except ValueError:
                retries = 3
        return cls(max_rate_limit_retries=retries, budget_usd=budget, sleep=sleep)

    async def with_backoff(self, attempt: AttemptFn) -> T:
        """Run `attempt()`; on `RateLimitedError`, back off (honoring
        `retry_after`, else exponential) and retry up to `max_rate_limit_retries`
        times. Re-raises the last `RateLimitedError` once retries are exhausted
        (so `run_over_providers` can then fall back to the next provider). Any
        non-rate-limit exception propagates immediately."""
        for attempt_index in range(self.max_rate_limit_retries + 1):
            try:
                return await attempt()
            except RateLimitedError as exc:
                if attempt_index >= self.max_rate_limit_retries:
                    raise
                delay = (
                    exc.retry_after
                    if exc.retry_after is not None
                    else self.base_backoff_seconds * (2 ** attempt_index)
                )
                await self.sleep(delay)
        raise AssertionError("unreachable")  # pragma: no cover

    async def run_over_providers(
        self, attempts: list[tuple[str, AttemptFn]]
    ) -> T:
        """Try each `(provider, attempt)` in order, each with `with_backoff`.
        Move to the next provider only when this one keeps rate-limiting past
        the retry budget; any other exception propagates. Re-raises the last
        `RateLimitedError` if every provider is exhausted."""
        last_exc: Optional[RateLimitedError] = None
        for _provider, attempt in attempts:
            try:
                return await self.with_backoff(attempt)
            except RateLimitedError as exc:
                last_exc = exc
                continue
        if last_exc is not None:
            raise last_exc
        raise RuntimeError("run_over_providers called with no attempts")

    def check_budget(self, spent_usd: float) -> None:
        """Raise `BudgetExceededError` if `spent_usd` has reached the cap. A
        no-op when no cap is configured."""
        if self.budget_usd is not None and spent_usd >= self.budget_usd:
            raise BudgetExceededError(spent_usd, self.budget_usd)
