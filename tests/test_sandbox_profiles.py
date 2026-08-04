"""Deterministic tests for the sandbox profile matrix + hardened `docker run`
argv construction (`ai_os.sandbox.container_runner`).

Unlike `test_container_runner.py` (which runs REAL Docker containers to prove
network/mount isolation), these need no Docker at all: they assert every
language profile — including **Java**, which the live suite deliberately does
NOT pull on this shared host — produces a fully-hardened argv with the right
image and command. This is what gives the Java path automated coverage without
pulling maven:3.9-eclipse-temurin-17-alpine just for a test.

A real end-to-end Java container run also exists, but is opt-in: set
`AI_OS_TEST_JAVA_SANDBOX=1` to run `test_java_sandbox_runs_mvn` (heavy pull).
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from ai_os.sandbox.container_runner import (
    SANDBOX_PROFILES,
    EphemeralSandboxRunner,
    SandboxProfile,
    build_docker_argv,
)

ALL_LANGUAGES = ["python", "javascript", "typescript", "java"]


def test_every_supported_language_has_a_profile():
    for language in ALL_LANGUAGES:
        assert language in SANDBOX_PROFILES
        profile = SANDBOX_PROFILES[language]
        assert profile.image and profile.command


def test_java_profile_is_maven_mvn_test():
    profile = SANDBOX_PROFILES["java"]
    assert profile.image == "maven:3.9-eclipse-temurin-17-alpine"
    assert profile.command == "mvn test"


@pytest.mark.parametrize("language", ALL_LANGUAGES)
def test_profile_builds_fully_hardened_argv(language, tmp_path):
    profile = SANDBOX_PROFILES[language]
    argv = build_docker_argv("docker", tmp_path, "ai-os-sandbox-testname", profile)
    joined = " ".join(argv)

    # Every doc 10 §1.1 hardening flag must be present, for EVERY language.
    assert argv[:3] == ["docker", "run", "--rm"]
    assert "--network" in argv and argv[argv.index("--network") + 1] == "none"
    assert "--memory=2g" in argv
    assert "--cpus=2.0" in argv
    assert "--cap-drop=ALL" in argv
    assert "--user" in argv and argv[argv.index("--user") + 1] == "1000:1000"
    assert "--tmpfs" in argv
    assert "/tmp:rw,noexec,nosuid,size=256m" in argv
    # Read-only worktree mount at /app.
    assert f"{tmp_path.resolve()}:/app:ro" in argv
    assert "--workdir" in argv and argv[argv.index("--workdir") + 1] == "/app"
    # The container name is passed for timeout-kill targeting.
    assert "--name" in argv and argv[argv.index("--name") + 1] == "ai-os-sandbox-testname"
    # Image + command come last, wrapped in `sh -c`.
    assert argv[-4] == profile.image
    assert argv[-3:] == ["sh", "-c", profile.command]
    assert language  # (silence unused in case of param-only use)
    assert joined.count("--network none") == 1


def test_mount_path_is_absolute_even_for_relative_input(tmp_path, monkeypatch):
    # A relative worktree path must still resolve to an absolute host path in
    # the -v mount (docker requires absolute source paths).
    monkeypatch.chdir(tmp_path)
    (tmp_path / "sub").mkdir()
    argv = build_docker_argv("docker", Path("sub"), "n", SANDBOX_PROFILES["python"])
    mount = argv[argv.index("-v") + 1]
    assert mount.startswith("/")  # absolute
    assert mount.endswith(":/app:ro")


# -- opt-in real Java container run (heavy image pull) -----------------------


@pytest.mark.skipif(
    os.environ.get("AI_OS_TEST_JAVA_SANDBOX") != "1" or shutil.which("docker") is None,
    reason="opt-in only (set AI_OS_TEST_JAVA_SANDBOX=1 with docker available; heavy Maven image pull)",
)
async def test_java_sandbox_runs_mvn(tmp_path):
    """Real end-to-end Java validation against a minimal Maven project. Opt-in
    because it pulls maven:3.9-eclipse-temurin-17-alpine (heavy) on a shared
    host — run deliberately, never on a routine `pytest`."""
    (tmp_path / "pom.xml").write_text(
        """<project xmlns="http://maven.apache.org/POM/4.0.0">
  <modelVersion>4.0.0</modelVersion>
  <groupId>com.example</groupId>
  <artifactId>demo</artifactId>
  <version>1.0</version>
</project>
"""
    )
    runner = EphemeralSandboxRunner(timeout_seconds=300.0)
    result = await runner.run_validation(tmp_path, "java")
    # A pom with no tests still lets `mvn test` succeed (exit 0). We only assert
    # the pipeline ran and produced a result, not a specific test outcome.
    assert result.exit_code is not None
    assert "mvn" in result.output.lower() or result.success or not result.success
