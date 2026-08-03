"""ANSI log cleanup + pass/fail feedback envelope for sandbox validation runs.

Deliberately scoped down from doc 10 §3's example, which parses toolchain-
specific compiler output (e.g. TypeScript's `TS2345`-style diagnostics) into
structured `{file, line, column, rule, message}` error objects. Building a
distinct structured-error parser per toolchain (tsc vs. flake8 vs. mypy vs.
pytest, each with its own output format) is real, speculative, high-
maintenance scope for a benefit — structured line/column data — that an LLM
can already extract fine from clean, readable text. So this module does only
two things: strip ANSI escape codes, and wrap the result in a small envelope
dict with a one-line pass/fail summary. No per-toolchain parsing.
"""
from __future__ import annotations

import re

# Doc 10 §4's own regex, verbatim — it's correct, no need to invent a
# different one. Matches both the two-byte "Fe" escapes (`ESC @`..`ESC _`)
# and CSI sequences (`ESC [ ... final-byte`), which together cover color
# codes, cursor movement, and other common terminal control sequences.
_ANSI_ESCAPE_RE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")


def strip_ansi_codes(text: str) -> str:
    """Removes ANSI escape sequences (color codes, cursor movement, etc.)."""
    return _ANSI_ESCAPE_RE.sub("", text)


def build_feedback(
    success: bool, exit_code: int, raw_output: str, max_output_chars: int = 4000
) -> dict:
    """Build the small JSON-able feedback envelope handed back to the
    orchestrator's feedback loop / HITL surface.

    `output` is ANSI-stripped and, if longer than `max_output_chars`,
    truncated to keep the *end* of the output rather than the start.
    Compiler and test-runner errors are almost always printed last (a
    traceback, a final "N failed" pytest summary, tsc's error list at the
    bottom) — the interesting signal for a fix-it feedback loop lives at the
    tail of the log, not the top, so truncation must preserve that tail
    even though it means silently dropping earlier context (e.g. build
    setup / dependency install noise, which is rarely diagnostic).
    """
    clean_output = strip_ansi_codes(raw_output)
    if len(clean_output) > max_output_chars:
        clean_output = clean_output[-max_output_chars:]

    if success:
        summary = "Validation passed."
    else:
        summary = f"Validation failed (exit code {exit_code})."

    return {
        "status": "VALIDATION_PASSED" if success else "VALIDATION_FAILED",
        "exit_code": exit_code,
        "summary": summary,
        "output": clean_output,
    }
