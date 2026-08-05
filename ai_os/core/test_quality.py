"""Validator-quality assessment (Phase 6, feature 1a + the deterministic half of
1c's surfacing).

The sandbox proves "the tests pass" — but an agent can *game* that by writing
weak/absent tests and code that trivially satisfies them. This module is the
cheap, zero-LLM first line of defense: given the set of files a task actually
changed (from the worktree git diff, not the planner-declared write_set), it
classifies them into source / test / other and reports whether a code change
shipped WITHOUT any accompanying test change — the single most common form of
validator-gaming.

It deliberately does NOT block by itself (some real changes legitimately need no
test — a config tweak, a docs edit, a pure rename). It produces a signal that:
  - the CLI shows live,
  - the PR body surfaces for the human reviewer (the HITL gate the user chose as
    the answer to validator-gaming),
  - and the optional cheap-model "test critic" (1c) deepens with a judgement.

It also flags changes to CI / security-sensitive config (reusing
`sensitive_files`), because a task that rewrites `.github/workflows/*` is exactly
the "self-certifying validator" problem: a green CI it authored proves nothing.
"""
from __future__ import annotations

import posixpath
from dataclasses import dataclass, field
from fnmatch import fnmatch
from typing import Iterable

from ai_os.analyzer.languages import detect_language
from ai_os.core.sensitive_files import sensitive_paths

# Languages whose changes we expect to be backed by unit tests. HTML/CSS/SQL are
# code files to the analyzer but aren't unit-tested the same way, so a change to
# one of them doesn't trigger a "missing tests" warning.
_TESTABLE_LANGUAGES = {"python", "java", "javascript", "typescript"}

# Filename/path globs that mark a file as a TEST (matched against the POSIX
# repo-relative path, case-insensitively). `fnmatch`'s `*` spans `/`, so a
# `*/tests/*`-style pattern matches at any depth.
_TEST_PATH_PATTERNS = (
    # Python
    "test_*.py", "*_test.py", "conftest.py",
    # JS / TS
    "*.test.js", "*.test.jsx", "*.test.ts", "*.test.tsx", "*.test.mjs",
    "*.spec.js", "*.spec.jsx", "*.spec.ts", "*.spec.tsx", "*.spec.mjs",
    # Java
    "*Test.java", "*Tests.java", "*IT.java",
)
# Directory-name segments that mark everything under them as tests.
_TEST_DIR_SEGMENTS = {"test", "tests", "__tests__", "spec", "e2e"}


def _is_test_file(path: str) -> bool:
    posix = path.replace("\\", "/")
    low = posix.lower()
    name = posixpath.basename(posix)
    if any(fnmatch(name.lower(), pat.lower()) for pat in _TEST_PATH_PATTERNS):
        return True
    segments = low.split("/")
    # Java's `src/test/java/...` and JS `__tests__/` etc.: any directory segment
    # in the path is a test dir. Exclude the basename itself from this check so a
    # plain `spec.py` module isn't mis-flagged.
    return any(seg in _TEST_DIR_SEGMENTS for seg in segments[:-1])


def _is_testable_source(path: str) -> bool:
    from pathlib import PurePosixPath

    lang = detect_language(PurePosixPath(path.replace("\\", "/")))
    return lang in _TESTABLE_LANGUAGES


@dataclass
class TestPresenceReport:
    """The deterministic verdict on a task's changed-file set."""

    __test__ = False  # not a pytest test class despite the "Test" prefix

    source_files: list[str] = field(default_factory=list)
    test_files: list[str] = field(default_factory=list)
    other_files: list[str] = field(default_factory=list)
    # Changes to CI / secrets / build / AI-OS config — a green result on these is
    # not self-certifying (the agent could have weakened its own validator/CI).
    sensitive_files: list[str] = field(default_factory=list)

    @property
    def missing_tests(self) -> bool:
        """True when testable source code changed but no test file did — the
        headline "no test was added for this change" signal."""
        return bool(self.source_files) and not self.test_files

    @property
    def has_concerns(self) -> bool:
        return self.missing_tests or bool(self.sensitive_files)

    def summary_line(self) -> str:
        parts = [
            f"{len(self.source_files)} source",
            f"{len(self.test_files)} test",
            f"{len(self.other_files)} other",
        ]
        line = "changed: " + ", ".join(parts)
        if self.missing_tests:
            line += " — ⚠ no test added for a code change"
        if self.sensitive_files:
            line += f" — ⚠ {len(self.sensitive_files)} CI/sensitive file(s)"
        return line


def assess_test_presence(changed_files: Iterable[str]) -> TestPresenceReport:
    """Classify a task's changed files and report whether tests are missing.

    `changed_files` is the real diff of the task's worktree against the base
    branch (POSIX, repo-relative) — what the agent actually touched, which may
    differ from the planner's declared `write_set`."""
    report = TestPresenceReport()
    for path in changed_files:
        path = path.strip()
        if not path:
            continue
        if _is_test_file(path):
            report.test_files.append(path)
        elif _is_testable_source(path):
            report.source_files.append(path)
        else:
            report.other_files.append(path)
    report.sensitive_files = sensitive_paths(changed_files)
    return report
