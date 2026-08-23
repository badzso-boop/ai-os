"""Pure decision logic for whether a plan may bypass the PR flow via
`--merge-to-main`.

Extracted out of `ai_os/cli.py` (see `docs/23_JOB_LAYER_AND_SAFETY_SERVICE.md`
§1): the rule — *"if the plan writes a CI/secrets/build/ai-os-config file,
`--merge-to-main` is refused, the change must go through a reviewable PR"* — is
a security policy, not CLI-presentation logic. Keeping it here (no I/O, no
`console`, no `click`) means any future entry point (a REST endpoint, a job
API) can enforce the exact same rule without re-implementing or drifting from
it.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from ai_os.core.models import TaskNode
from ai_os.core.sensitive_files import sensitive_paths


@dataclass(frozen=True)
class PlanSafetyDecision:
    """The outcome of evaluating a task plan's write-set against the
    sensitive-file guard, for a given `--merge-to-main` request."""

    flagged_paths: frozenset[str]
    merge_to_main_allowed: bool
    reason: str | None


def evaluate_plan_safety(
    tasks: Sequence[TaskNode], merge_to_main_requested: bool
) -> PlanSafetyDecision:
    """Pure function. No I/O, no console, no click. Wraps `sensitive_paths()`.

    A plan that touches no sensitive path always allows `--merge-to-main`. A
    plan that touches at least one sensitive path allows `--merge-to-main`
    only if it wasn't requested (the default PR flow is always fine); a
    request combined with flagged paths is refused, with `reason` explaining
    why.
    """
    flagged = frozenset(sensitive_paths({p for t in tasks for p in t.write_set}))

    if not flagged:
        return PlanSafetyDecision(flagged_paths=flagged, merge_to_main_allowed=True, reason=None)

    if not merge_to_main_requested:
        return PlanSafetyDecision(flagged_paths=flagged, merge_to_main_allowed=True, reason=None)

    return PlanSafetyDecision(
        flagged_paths=flagged,
        merge_to_main_allowed=False,
        reason=(
            "Refusing --merge-to-main: this plan touches security-sensitive files. "
            "Run without --merge-to-main so the change goes through a PR a human reviews."
        ),
    )
