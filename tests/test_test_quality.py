"""Tests for `ai_os.core.test_quality` — the zero-LLM test-presence classifier
(Phase 6, feature 1a). Pure functions over path strings, no git/LLM/Docker."""
from __future__ import annotations

from ai_os.core.test_quality import assess_test_presence


def test_source_without_test_flags_missing():
    report = assess_test_presence(["src/app/service.py", "src/app/models.py"])
    assert report.source_files == ["src/app/service.py", "src/app/models.py"]
    assert report.test_files == []
    assert report.missing_tests is True
    assert report.has_concerns is True


def test_source_with_test_is_ok():
    report = assess_test_presence(["src/app/service.py", "tests/test_service.py"])
    assert report.source_files == ["src/app/service.py"]
    assert report.test_files == ["tests/test_service.py"]
    assert report.missing_tests is False


def test_recognizes_test_conventions_across_languages():
    paths = [
        "test_foo.py",              # python prefix
        "foo_test.py",              # python suffix
        "conftest.py",             # pytest fixtures
        "web/Button.test.tsx",     # JS/TS .test.
        "web/api.spec.ts",         # JS/TS .spec.
        "web/__tests__/util.ts",   # __tests__ dir
        "src/test/java/a/FooIT.java",   # java IT + src/test dir
        "com/example/FooTest.java",     # java *Test
    ]
    report = assess_test_presence(paths)
    assert report.source_files == []          # every path is a test
    assert set(report.test_files) == set(paths)
    assert report.missing_tests is False


def test_non_code_files_are_other_not_source():
    report = assess_test_presence(["README.md", "package.json", "config.yaml"])
    assert report.source_files == []
    assert report.other_files == ["README.md", "package.json", "config.yaml"]
    # No testable source changed -> not a "missing tests" situation.
    assert report.missing_tests is False


def test_sensitive_ci_files_are_flagged():
    report = assess_test_presence(
        ["src/app/service.py", "tests/test_service.py", ".github/workflows/ci.yml"]
    )
    assert ".github/workflows/ci.yml" in report.sensitive_files
    # A CI change makes it a concern even though tests were added.
    assert report.missing_tests is False
    assert report.has_concerns is True


def test_empty_and_blank_paths_are_ignored():
    report = assess_test_presence(["", "  ", "src/x.py"])
    assert report.source_files == ["src/x.py"]
    assert report.test_files == [] and report.other_files == []


def test_summary_line_mentions_concerns():
    report = assess_test_presence(["src/x.py", ".github/workflows/ci.yml"])
    line = report.summary_line()
    assert "no test added" in line
    assert "sensitive" in line.lower()
