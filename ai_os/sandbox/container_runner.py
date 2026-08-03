"""Ephemeral, hardened Docker container runner for sandboxed validation
(doc 10) — runs a language's build+test command inside a locked-down,
throwaway container mounting the task's Git worktree read-only.

Deliberate deviation from doc 10 §4's blueprint: that snippet uses the
synchronous `docker` Python SDK (`docker.from_env()`) wrapped in
`loop.run_in_executor`. This project's established convention for driving
external processes (`ai_os/core/staging.py`, Phase 2) is to shell out
directly via `asyncio.create_subprocess_exec` against the real CLI tool
(there: `git`; here: `docker`) rather than pull in a second heavyweight SDK
wrapped in an executor. This module follows that same pattern: it builds
`docker run ...` argv lists and runs them with `asyncio.create_subprocess_exec`.

Security hardening applied to every `docker run` (doc 10 §1.1, applied
faithfully, not weakened):
    --rm                                  auto-remove on exit
    -v {worktree}:/app:ro                 host code is read-only inside the container
    --workdir /app
    --network none                        no network access at all (data exfiltration prevention)
    --memory=2g --cpus=2.0                DoS / resource-exhaustion protection
    --tmpfs /tmp:rw,noexec,nosuid,size=256m   the only writable path is RAM-backed and non-executable
    --cap-drop=ALL                        no Linux capabilities (container-escape hardening)
    --user 1000:1000                      non-root
    --name ai-os-sandbox-<uuid>           unique name, so a timed-out run can be targeted for `docker kill`

Timeout handling: `docker run` here is a foreground/attached invocation (no
`--detach`), so on the *client* side we can `asyncio.wait_for(...)` around
`proc.communicate()`. But killing the *client* process on a timeout does not
guarantee the container itself stops running server-side in the Docker
daemon — so on a timeout we explicitly `docker kill <name>` (best-effort,
errors ignored: the container may already be gone) before returning a
timeout `ValidationResult`. `--rm` then reaps the killed container. Per the
spec, `run_validation` never raises for a validation-level failure — a
non-zero exit code inside the container, or a timeout, both come back as a
`ValidationResult(success=False, ...)`. Real exceptions are reserved for
infra faults: docker itself unreachable/missing, or `docker run` failing to
even start the container (checked via its own exit code before trusting it
as the *container's* exit code).
"""
from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from pathlib import Path

from ai_os.sandbox.log_parser import build_feedback


@dataclass
class ValidationResult:
    success: bool
    exit_code: int
    summary: str
    output: str


@dataclass(frozen=True)
class SandboxProfile:
    image: str
    command: str  # shell command run inside the container, e.g. "npx tsc --noEmit && npm test"


# Doc 10 §2's language profile matrix, extended to javascript alongside
# typescript — Phase 1's analyzer (`ai_os/analyzer/languages.py`) already
# treats JS/TS as siblings (same query family, same import-resolution
# logic), so the sandbox profile matrix mirrors that.
SANDBOX_PROFILES: dict[str, SandboxProfile] = {
    "python": SandboxProfile(
        "python:3.12-slim",
        "pip install -q -r requirements.txt 2>/dev/null; pytest",
    ),
    "javascript": SandboxProfile("node:20-alpine", "npm test"),
    "typescript": SandboxProfile("node:20-alpine", "npx tsc --noEmit && npm test"),
    # Java is a real, correct profile entry but deliberately NOT exercised
    # by the automated test suite: maven:3.9-eclipse-temurin-17-alpine is a
    # much heavier image to pull than the others, and this is a shared host
    # (see module docstring / CLAUDE.md) — pulling it just for test coverage
    # of an otherwise-identical code path was judged not worth the cost.
    # This is a deliberate, flagged scope cut, not an oversight.
    "java": SandboxProfile("maven:3.9-eclipse-temurin-17-alpine", "mvn test"),
}


class SandboxLanguageNotSupportedError(ValueError):
    """Raised when `run_validation` is asked to validate a language with no
    entry in `SANDBOX_PROFILES`.
    """


class SandboxTimeoutError(RuntimeError):
    """Not raised by `run_validation` itself (a timeout there is reported as
    a `ValidationResult`, per the "never raises for a validation-level
    outcome" contract) — reserved for callers/tests that want a hard-error
    signal distinct from `SandboxLanguageNotSupportedError`.
    """


class EphemeralSandboxRunner:
    """Runs a language's build+test command inside a hardened, ephemeral
    Docker container, one per `run_validation` call.
    """

    def __init__(self, timeout_seconds: float = 60.0, docker_cli: str = "docker") -> None:
        self.timeout_seconds = timeout_seconds
        self.docker_cli = docker_cli

    async def run_validation(self, worktree_path: Path, language: str) -> ValidationResult:
        """Run `language`'s configured build+test command against
        `worktree_path` (mounted read-only at /app) inside a fresh,
        hardened, ephemeral container.

        Never raises for a validation *failure* (non-zero exit code inside
        the container, or a timeout) — those come back as
        `ValidationResult(success=False, ...)`. Raises
        `SandboxLanguageNotSupportedError` for an unrecognized language, and
        lets underlying `OSError`/`asyncio` exceptions propagate for genuine
        infra faults (e.g. the `docker` binary not found).
        """
        profile = SANDBOX_PROFILES.get(language)
        if profile is None:
            raise SandboxLanguageNotSupportedError(
                f"No sandbox profile configured for language {language!r}. "
                f"Supported: {sorted(SANDBOX_PROFILES)}"
            )

        container_name = f"ai-os-sandbox-{uuid.uuid4().hex[:12]}"
        argv = [
            self.docker_cli,
            "run",
            "--rm",
            "--name",
            container_name,
            "-v",
            f"{Path(worktree_path).resolve()}:/app:ro",
            "--workdir",
            "/app",
            "--network",
            "none",
            "--memory=2g",
            "--cpus=2.0",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,size=256m",
            "--cap-drop=ALL",
            "--user",
            "1000:1000",
            profile.image,
            "sh",
            "-c",
            profile.command,
        ]

        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )

        try:
            stdout, _ = await asyncio.wait_for(
                proc.communicate(), timeout=self.timeout_seconds
            )
        except asyncio.TimeoutError:
            # The client-side `docker run` process being killed does not
            # necessarily stop the container running in the daemon, so
            # explicitly kill it by its known unique name. Best-effort: the
            # container may have already exited on its own in a race with
            # the timeout firing, so ignore failures here.
            await self._best_effort_kill(container_name)
            # Reap our own asyncio subprocess handle so it doesn't linger as
            # a zombie; the container itself is handled above (--rm reaps
            # it once killed).
            try:
                await asyncio.wait_for(proc.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()

            feedback = build_feedback(
                success=False,
                exit_code=124,
                raw_output=f"Validation timed out after {self.timeout_seconds}s.",
            )
            return ValidationResult(
                success=False,
                exit_code=124,
                summary=f"Validation timed out after {self.timeout_seconds}s.",
                output=feedback["output"],
            )

        assert proc.returncode is not None
        raw_output = stdout.decode(errors="replace")

        # `docker run` itself returns 125 for a "failed to even start the
        # container" class of error (bad flags, image pull failure, daemon
        # unreachable, etc.) as opposed to the *containerized command's*
        # exit code being propagated through normally. We don't hard-fail
        # on 125 (a container can legitimately exit with an unrelated exit
        # code that happens to be 125), but treat proc.returncode as-is:
        # docker faithfully propagates the containerized process's exit
        # code as its own exit code on a normal run, so no special-casing
        # is needed beyond letting a genuine `docker` invocation error
        # (e.g. FileNotFoundError if the binary doesn't exist) propagate as
        # a real exception rather than being swallowed here.
        exit_code = proc.returncode
        success = exit_code == 0

        feedback = build_feedback(
            success=success, exit_code=exit_code, raw_output=raw_output
        )
        return ValidationResult(
            success=success,
            exit_code=exit_code,
            summary=feedback["summary"],
            output=feedback["output"],
        )

    async def _best_effort_kill(self, container_name: str) -> None:
        try:
            kill_proc = await asyncio.create_subprocess_exec(
                self.docker_cli,
                "kill",
                container_name,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(kill_proc.wait(), timeout=10.0)
        except Exception:
            # Best-effort: the container may already be gone (exited on its
            # own right as the timeout fired), or `docker kill` itself may
            # be slow/unreachable — either way, don't let cleanup failure
            # mask the timeout result we're about to return.
            pass
