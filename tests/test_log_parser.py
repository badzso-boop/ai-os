"""Tests for `ai_os.sandbox.log_parser`.

Pure string-processing unit tests — no Docker, no subprocess involved.
"""
from __future__ import annotations

from ai_os.sandbox.log_parser import build_feedback, strip_ansi_codes


def test_strip_ansi_codes_removes_color_codes():
    raw = "\x1b[31mERROR\x1b[0m: \x1b[1msomething broke\x1b[0m"
    assert strip_ansi_codes(raw) == "ERROR: something broke"


def test_strip_ansi_codes_removes_cursor_movement():
    # \x1b[2K = erase line, \x1b[1A = cursor up one line — common in
    # progress-bar-style CLI output (npm, pip, pytest with a live counter).
    raw = "line one\x1b[2K\x1b[1Aline two\r\n"
    cleaned = strip_ansi_codes(raw)
    assert "\x1b" not in cleaned
    assert "line one" in cleaned
    assert "line two" in cleaned


def test_strip_ansi_codes_no_escapes_is_noop():
    raw = "plain text, nothing fancy\nsecond line\n"
    assert strip_ansi_codes(raw) == raw


def test_build_feedback_pass_envelope_shape():
    feedback = build_feedback(success=True, exit_code=0, raw_output="all good\n")
    assert feedback == {
        "status": "VALIDATION_PASSED",
        "exit_code": 0,
        "summary": "Validation passed.",
        "output": "all good\n",
    }


def test_build_feedback_fail_envelope_shape():
    feedback = build_feedback(success=False, exit_code=1, raw_output="boom\n")
    assert feedback["status"] == "VALIDATION_FAILED"
    assert feedback["exit_code"] == 1
    assert feedback["summary"] == "Validation failed (exit code 1)."
    assert feedback["output"] == "boom\n"


def test_build_feedback_strips_ansi_from_output():
    raw = "\x1b[31mFAILED\x1b[0m tests/test_foo.py::test_bar"
    feedback = build_feedback(success=False, exit_code=1, raw_output=raw)
    assert "\x1b" not in feedback["output"]
    assert feedback["output"] == "FAILED tests/test_foo.py::test_bar"


def test_build_feedback_truncates_keeping_the_tail():
    # Errors are almost always at the end of a compiler/test log, so
    # truncation must keep the *end*, not the beginning.
    raw_output = ("x" * 100) + "IMPORTANT_TAIL_MARKER"
    feedback = build_feedback(
        success=False, exit_code=1, raw_output=raw_output, max_output_chars=30
    )
    assert len(feedback["output"]) == 30
    assert feedback["output"].endswith("IMPORTANT_TAIL_MARKER")
    assert "IMPORTANT_TAIL_MARKER" in feedback["output"]


def test_build_feedback_no_truncation_when_under_limit():
    raw_output = "short output\n"
    feedback = build_feedback(
        success=True, exit_code=0, raw_output=raw_output, max_output_chars=4000
    )
    assert feedback["output"] == raw_output
