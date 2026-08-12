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
import hashlib
import tempfile
import uuid
from dataclasses import dataclass, replace
from pathlib import Path

from ai_os.sandbox.db_services import DatabaseSandbox, DatabaseSandboxError
from ai_os.sandbox.log_parser import build_feedback
from ai_os.sandbox.node_toolchain import (
    NODE_LANGUAGES,
    NODE_MANIFESTS,
    build_copy_dockerfile,
    detect_node_package_manager,
    discover_node_manifests,
    node_install_command,
    node_test_command,
)
from ai_os.sandbox.sandbox_config import SandboxConfigError, load_sandbox_config


def _as_setup_failure(result: ValidationResult) -> ValidationResult:
    """Re-label a failed setup/seed phase so the agent sees it wasn't the tests
    that failed, but the migration/seed step before them."""
    return ValidationResult(
        success=False,
        exit_code=result.exit_code,
        summary="Setup/seed step failed before tests ran.",
        output="SETUP/SEED FAILED (before tests ran)\n" + result.output,
    )


@dataclass
class ValidationResult:
    success: bool
    exit_code: int
    summary: str
    output: str


@dataclass(frozen=True)
class SandboxProfile:
    image: str
    command: str  # phase-2 (validation) shell command, e.g. "npx tsc --noEmit && npm test"
    # Two-phase dependency handling (optional). If `install_command` is set and
    # the worktree contains at least one non-empty `dependency_manifests` file, a
    # per-task image is built WITH network that runs `install_command` (phase 1),
    # and the network-isolated validation (phase 2) runs against that image — so a
    # project's third-party deps install without ever giving the untrusted,
    # agent-written TEST code network access. `install_command=None` (or no
    # manifest present) = single-phase (the base image + `command`).
    #
    # Per ecosystem, deps must be discoverable from the read-only /app mount in
    # phase 2: Python installs to the global site-packages (found anywhere);
    # Java uses a fixed `-Dmaven.repo.local=/deps/.m2` (offline in phase 2);
    # Node installs into /deps/node_modules and phase 2 sets NODE_PATH/PATH to it
    # via `run_env`.
    dependency_manifests: tuple[str, ...] = ()
    install_command: str | None = None
    run_env: tuple[tuple[str, str], ...] = ()  # extra -e VAR=VALUE for phase 2
    # Isolation strategy for the validation run:
    #   "mount" (default) — bind-mount the worktree read-only at /app and run
    #     against a base/dependency image (deps live outside the tree: Python
    #     site-packages, Java .m2). Fast, no code copy.
    #   "copy" — COPY the code AND install deps into one per-task image, run with
    #     NO bind mount. Needed for Node, whose bundler-based test runners
    #     (Vite/Vitest/Next) resolve node_modules from the tree and ignore the
    #     out-of-tree trick. See `node_toolchain.py`.
    isolation: str = "mount"


# Doc 10 §2's language profile matrix, extended to javascript alongside
# typescript — Phase 1's analyzer (`ai_os/analyzer/languages.py`) already
# treats JS/TS as siblings (same query family, same import-resolution
# logic), so the sandbox profile matrix mirrors that.
#
# Python note: since every validation run is --network none (doc 10's own
# hardening requirement), a `pip install` *inside* the validation container can
# never reach PyPI. "ai-os-sandbox-python:3.12" (docker/python-sandbox.Dockerfile,
# build once: `docker build -t ai-os-sandbox-python:3.12 -f
# docker/python-sandbox.Dockerfile .`) bakes pytest in ahead of time so the
# validation run needs no network for pytest itself. A project's OWN third-party
# deps (its requirements.txt) are handled by the TWO-PHASE flow: they're
# installed in a network-enabled per-task image build (phase 1), then the tests
# run against that image with --network none (phase 2). See
# `dependency_manifest`/`install_command` below and `_ensure_dependency_image`.
SANDBOX_PROFILES: dict[str, SandboxProfile] = {
    "python": SandboxProfile(
        image="ai-os-sandbox-python:3.12",
        # `-p no:cacheprovider`: the worktree is mounted read-only, so
        # pytest's cache plugin can never write `.pytest_cache/` into it —
        # harmless but noisy `PytestCacheWarning`s on every single run.
        # Disabled outright rather than redirected to /tmp: the container is
        # single-shot and thrown away immediately after, so there is no
        # cache to actually reuse between runs anyway.
        command="pytest -p no:cacheprovider",
        dependency_manifests=("requirements.txt",),
        install_command="pip install --no-cache-dir -r requirements.txt",
    ),
    # Node: copy-isolation (code + node_modules in one image, no bind mount) so
    # Vite/Vitest/Next resolve node_modules normally; package manager (pnpm/yarn/
    # npm) auto-detected from the lockfile via corepack. The default test command
    # is `<pm> test` (see node_toolchain), overridable via .ai-os/sandbox.json.
    "javascript": SandboxProfile(
        image="node:22-alpine",
        command="npm test",
        dependency_manifests=NODE_MANIFESTS,
        isolation="copy",
    ),
    "typescript": SandboxProfile(
        image="node:22-alpine",
        command="npm test",
        dependency_manifests=NODE_MANIFESTS,
        isolation="copy",
    ),
    # Java: phase 1 pre-fetches deps into a fixed, world-readable local repo
    # (`-Dmaven.repo.local=/deps/.m2`) via `dependency:go-offline`; phase 2 runs
    # tests offline (`-o`) against that same repo. `go-offline` is best-effort —
    # it can miss a few plugin deps, in which case an offline `mvn test` reports
    # them (the agent then sees the failure) — a known Maven quirk, not a bug here.
    # The profile config + hardened argv are covered deterministically by
    # tests/test_sandbox_profiles.py; a real Maven container run is opt-in via
    # AI_OS_TEST_JAVA_SANDBOX=1 (heavy image, shared host).
    "java": SandboxProfile(
        image="maven:3.9-eclipse-temurin-17-alpine",
        command="mvn -o -Dmaven.repo.local=/deps/.m2 test",
        dependency_manifests=("pom.xml",),
        install_command="mvn -B -Dmaven.repo.local=/deps/.m2 dependency:go-offline",
    ),
}


def build_docker_argv(
    docker_cli: str,
    worktree_path: Path,
    container_name: str,
    profile: SandboxProfile,
    image: str | None = None,
    network: str = "none",
    extra_env: tuple[tuple[str, str], ...] = (),
    command: str | None = None,
    mount: bool = True,
) -> list[str]:
    """Build the hardened `docker run ...` argv for one validation run.

    Factored out of `run_validation` so the exact hardening flags + per-language
    image/command can be asserted deterministically in tests without actually
    pulling a (possibly heavy, e.g. Maven/JDK) image or running a container on a
    shared host — the Java profile in particular is covered this way. The order
    and content here must stay in lockstep with doc 10 §1.1's hardening list.

    - `image` overrides `profile.image` (used to run against a per-task
      dependency image built by the two-phase flow).
    - `network` is `"none"` by default (full isolation); a DB-backed run passes
      an `--internal` docker network name so the container can reach the DB
      sidecar but still has no route to the internet.
    - `profile.run_env` + `extra_env` are passed through as `-e VAR=VALUE`
      (run_env for the Node NODE_PATH/PATH; extra_env for `.ai-os/sandbox.json`
      env + the DB connection).
    - `command` overrides `profile.command` (used for the setup/seed phase and a
      project's `test_command` override).
    - `mount=False` omits the read-only worktree bind mount — for copy-isolation
      runs where the code is already baked into `image` (see `isolation="copy"`).
    """
    argv = [
        docker_cli,
        "run",
        "--rm",
        "--name",
        container_name,
    ]
    if mount:
        argv += ["-v", f"{Path(worktree_path).resolve()}:/app:ro"]
    argv += [
        "--workdir",
        "/app",
        "--network",
        network,
        "--memory=2g",
        "--cpus=2.0",
        "--tmpfs",
        "/tmp:rw,noexec,nosuid,size=256m",
        "--cap-drop=ALL",
        "--user",
        "1000:1000",
    ]
    for key, value in tuple(profile.run_env) + tuple(extra_env):
        argv += ["-e", f"{key}={value}"]
    argv += [
        image or profile.image,
        "sh",
        "-c",
        command if command is not None else profile.command,
    ]
    return argv


def build_coverage_command(language: str, base_command: str, coverage) -> str | None:
    """Wrap a base test command with a coverage gate (Phase 6, feature 1b), or
    return `None` when AI-OS can't safely wrap this language/command (Node/Java, or
    a custom non-pytest Python command) — in which case the base command runs
    unchanged and the threshold is advisory.

    Out-of-the-box support is Python + `pytest` (the sandbox image bakes in
    `pytest-cov`): we insert `--cov=<path> --cov-report=term-missing
    --cov-fail-under=<min>` so pytest itself fails the run when coverage drops
    below the threshold — turning "no test exercises this change" into a
    validation failure the agent must fix, not a silently-passing green."""
    if coverage is None or not getattr(coverage, "enabled", False):
        return None
    stripped = base_command.strip()
    if language == "python" and stripped.split()[:1] == ["pytest"]:
        paths = coverage.paths or (".",)
        cov_args = " ".join(f"--cov={p}" for p in paths)
        return (
            f"{stripped} {cov_args} --cov-report=term-missing "
            f"--cov-fail-under={coverage.min_percent:g}"
        )
    # Node/Java (and custom Python commands) own their own coverage tooling —
    # AI-OS won't guess how to instrument them, so the threshold is advisory.
    return None


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

    def __init__(
        self,
        timeout_seconds: float = 60.0,
        docker_cli: str = "docker",
        build_timeout_seconds: float = 600.0,
    ) -> None:
        self.timeout_seconds = timeout_seconds
        self.docker_cli = docker_cli
        # Phase-1 dependency-image builds get a longer budget than a validation
        # run: `pip install`/`npm install`/`mvn go-offline` can be slow the first
        # time (they're cached by manifest hash afterwards).
        self.build_timeout_seconds = build_timeout_seconds

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

        try:
            config = load_sandbox_config(Path(worktree_path), language=language)
        except SandboxConfigError as exc:
            feedback = build_feedback(
                success=False, exit_code=1, raw_output=f"Invalid .ai-os/sandbox.json: {exc}"
            )
            return ValidationResult(False, 1, "Invalid .ai-os/sandbox.json.", feedback["output"])

        # A project can override the base image (e.g. a Playwright image for e2e).
        if config is not None and config.image:
            profile = replace(profile, image=config.image)

        worktree = Path(worktree_path)
        if profile.isolation == "copy":
            # Copy-isolation (Node): build one image with the code + deps baked
            # in, run with NO bind mount so bundler-based runners resolve
            # node_modules from the tree. The default test command is the
            # detected package manager's `<pm> test`.
            image, build_failure = await self._build_copy_image(profile, worktree, language)
            if build_failure is not None:
                return build_failure
            mount = False
            default_command = self._copy_default_command(worktree, language)
        else:
            # Two-phase (phase 1): bake a per-task dependency image WITH network,
            # then validate against it network-free (bind-mounting the worktree).
            image, build_failure = await self._resolve_image(profile, worktree)
            if build_failure is not None:
                return build_failure
            mount = True
            default_command = None  # -> profile.command

        try:
            if config is not None and config.needs_database:
                return await self._run_with_database(
                    worktree, profile, image, config, mount=mount,
                    default_command=default_command, language=language,
                )
            return await self._run_isolated(
                worktree, profile, image, config, mount=mount,
                default_command=default_command, language=language,
            )
        finally:
            if profile.isolation == "copy":
                await self._best_effort_rmi(image)

    def _copy_default_command(self, worktree: Path, language: str) -> str | None:
        if language in NODE_LANGUAGES:
            return node_test_command(detect_node_package_manager(worktree))
        return None

    # -- run phases ----------------------------------------------------------

    def _effective_command(
        self, profile: SandboxProfile, config, default_command: str | None, language: str
    ) -> str | None:
        """The test command to run: a `.ai-os/sandbox.json` `test_command`
        override, else the language default (`None` -> the profile's own
        command), wrapped with the coverage gate when one is configured (Phase 6,
        feature 1b)."""
        command = config.test_command if config is not None and config.test_command else default_command
        cov = getattr(config, "coverage", None) if config is not None else None
        if cov is not None and getattr(cov, "enabled", False):
            base = command if command is not None else profile.command
            wrapped = build_coverage_command(language, base, cov)
            if wrapped is not None:
                return wrapped
        return command

    async def _run_isolated(
        self, worktree_path: Path, profile: SandboxProfile, image: str, config,
        *, mount: bool, default_command: str | None, language: str,
    ) -> ValidationResult:
        """The default flow: a fully network-isolated (`--network none`)
        container. Honors an optional (DB-less) `.ai-os/sandbox.json`'s env,
        setup_commands, test_command override, and coverage gate."""
        extra_env = tuple(config.env.items()) if config is not None else ()
        if config is not None and config.setup_commands:
            setup = await self._run_phase(
                worktree_path, profile, image, network="none", extra_env=extra_env,
                command=" && ".join(config.setup_commands),
                timeout=self.build_timeout_seconds, name_prefix="ai-os-setup", mount=mount,
            )
            if not setup.success:
                return _as_setup_failure(setup)
        command = self._effective_command(profile, config, default_command, language)
        return await self._run_phase(
            worktree_path, profile, image, network="none", extra_env=extra_env,
            command=command, timeout=self.timeout_seconds, mount=mount,
        )

    async def _run_with_database(
        self, worktree_path: Path, profile: SandboxProfile, image: str, config,
        *, mount: bool, default_command: str | None, language: str,
    ) -> ValidationResult:
        """DB-backed flow: bring up a Postgres (or other) sidecar on a fresh
        `--internal` network (no internet), run the setup/seed commands, then the
        tests — both reaching the DB by its hostname but unable to exfiltrate."""
        db_sandbox = DatabaseSandbox(self.docker_cli)
        try:
            running = await db_sandbox.start(config.database)
        except DatabaseSandboxError as exc:
            feedback = build_feedback(
                success=False, exit_code=1, raw_output=f"Database sidecar failed to start: {exc}"
            )
            return ValidationResult(False, 1, "Database sidecar failed to start.", feedback["output"])

        try:
            extra_env = tuple(config.env.items())
            if config.setup_commands:
                setup = await self._run_phase(
                    worktree_path, profile, image, network=running.network, extra_env=extra_env,
                    command=" && ".join(config.setup_commands),
                    timeout=self.build_timeout_seconds, name_prefix="ai-os-setup", mount=mount,
                )
                if not setup.success:
                    return _as_setup_failure(setup)
            command = self._effective_command(profile, config, default_command, language)
            return await self._run_phase(
                worktree_path, profile, image, network=running.network, extra_env=extra_env,
                command=command, timeout=self.timeout_seconds, mount=mount,
            )
        finally:
            await db_sandbox.teardown(running)

    async def _run_phase(
        self, worktree_path: Path, profile: SandboxProfile, image: str, *,
        network: str, extra_env: tuple[tuple[str, str], ...], command: str | None,
        timeout: float, name_prefix: str = "ai-os-sandbox", mount: bool = True,
    ) -> ValidationResult:
        container_name = f"{name_prefix}-{uuid.uuid4().hex[:12]}"
        argv = build_docker_argv(
            self.docker_cli, worktree_path, container_name, profile,
            image=image, network=network, extra_env=extra_env, command=command, mount=mount,
        )
        return await self._run_container(argv, container_name, timeout)

    async def _run_container(
        self, argv: list[str], container_name: str, timeout: float
    ) -> ValidationResult:
        """Run one hardened container to completion (or timeout) and turn its
        exit code + output into a `ValidationResult`."""
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )

        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            # The client-side `docker run` process being killed does not
            # necessarily stop the container running in the daemon, so
            # explicitly kill it by its known unique name. Best-effort: the
            # container may have already exited on its own in a race with
            # the timeout firing, so ignore failures here.
            await self._best_effort_kill(container_name)
            try:
                await asyncio.wait_for(proc.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()

            feedback = build_feedback(
                success=False, exit_code=124,
                raw_output=f"Validation timed out after {timeout}s.",
            )
            return ValidationResult(
                success=False, exit_code=124,
                summary=f"Validation timed out after {timeout}s.", output=feedback["output"],
            )

        assert proc.returncode is not None
        raw_output = stdout.decode(errors="replace")
        # docker faithfully propagates the containerized process's exit code as
        # its own; a genuine `docker` invocation error (e.g. FileNotFoundError if
        # the binary doesn't exist) propagates as a real exception instead.
        exit_code = proc.returncode
        success = exit_code == 0

        feedback = build_feedback(success=success, exit_code=exit_code, raw_output=raw_output)
        return ValidationResult(
            success=success, exit_code=exit_code,
            summary=feedback["summary"], output=feedback["output"],
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

    # -- two-phase dependency image (phase 1) --------------------------------

    async def _resolve_image(
        self, profile: SandboxProfile, worktree_path: Path
    ) -> tuple[str, ValidationResult | None]:
        """Decide which image the validation run should use. If the profile has
        no two-phase config, or the worktree ships no non-empty dependency
        manifest, returns the base image unchanged. Otherwise builds (or reuses)
        a per-task dependency image and returns its tag. On a dependency-install
        failure, returns `(base_image, ValidationResult(success=False, ...))` so
        the caller reports it as a validation failure the agent can fix."""
        if not profile.install_command:
            return profile.image, None

        present = [
            name
            for name in profile.dependency_manifests
            if (worktree_path / name).is_file()
            and (worktree_path / name).read_text(encoding="utf-8", errors="replace").strip()
        ]
        if not present:
            return profile.image, None

        return await self._ensure_dependency_image(profile, worktree_path, present)

    async def _ensure_dependency_image(
        self, profile: SandboxProfile, worktree_path: Path, manifests: list[str]
    ) -> tuple[str, ValidationResult | None]:
        """Build (or reuse, cached by a hash of base image + install command +
        manifest bytes) a per-task image that installs `manifests` WITH network,
        so the subsequent validation run stays --network none. Returns
        `(tag, None)` on success, or `(base_image, failure_result)` if the deps
        don't install."""
        hasher = hashlib.sha256()
        hasher.update(profile.image.encode())
        hasher.update(b"\n")
        hasher.update((profile.install_command or "").encode())
        for name in manifests:
            hasher.update(b"\n--\n")
            hasher.update(name.encode())
            hasher.update(b"\n")
            hasher.update((worktree_path / name).read_bytes())
        tag = f"ai-os-sandbox-dep:{hasher.hexdigest()[:16]}"

        if await self._image_exists(tag):
            return tag, None

        with tempfile.TemporaryDirectory(prefix="ai-os-depbuild-") as ctx_dir:
            ctx = Path(ctx_dir)
            copy_lines = []
            for name in manifests:
                (ctx / name).write_bytes((worktree_path / name).read_bytes())
                copy_lines.append(f"COPY {name} ./{name}")
            dockerfile = (
                f"FROM {profile.image}\n"
                "WORKDIR /deps\n"
                + "\n".join(copy_lines)
                + f"\nRUN {profile.install_command}\n"
            )
            (ctx / "Dockerfile").write_text(dockerfile, encoding="utf-8")

            proc = await asyncio.create_subprocess_exec(
                self.docker_cli, "build", "-t", tag, str(ctx),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            try:
                out, _ = await asyncio.wait_for(
                    proc.communicate(), timeout=self.build_timeout_seconds
                )
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                feedback = build_feedback(
                    success=False, exit_code=124,
                    raw_output=f"Dependency install timed out after {self.build_timeout_seconds}s.",
                )
                return profile.image, ValidationResult(
                    success=False, exit_code=124,
                    summary="Dependency install timed out.", output=feedback["output"],
                )

            if proc.returncode != 0:
                raw = out.decode(errors="replace")
                feedback = build_feedback(
                    success=False, exit_code=proc.returncode or 1,
                    raw_output=(
                        "Dependency install failed while building the sandbox image "
                        f"from {', '.join(manifests)}:\n{raw}"
                    ),
                )
                return profile.image, ValidationResult(
                    success=False, exit_code=proc.returncode or 1,
                    summary="Dependency install failed.", output=feedback["output"],
                )

        return tag, None

    async def _image_exists(self, tag: str) -> bool:
        proc = await asyncio.create_subprocess_exec(
            self.docker_cli, "image", "inspect", tag,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.wait()
        return proc.returncode == 0

    # -- copy-isolation image (Node) -----------------------------------------

    async def _build_copy_image(
        self, profile: SandboxProfile, worktree_path: Path, language: str
    ) -> tuple[str, ValidationResult | None]:
        """Build a per-task image with the worktree code + installed deps baked
        in (deps layer cached across attempts; code layer changes each run). The
        Docker build context IS the worktree (so `COPY . ./` picks up the code);
        network is on for the install. A failed install/build comes back as a
        validation failure with the build log, not a crash. The image is tagged
        uniquely (the code changes each run) and cleaned up by the caller."""
        worktree = Path(worktree_path).resolve()
        if language in NODE_LANGUAGES:
            # Workspace-aware: a plain package.json/lockfile pair isn't enough
            # for a pnpm/yarn/npm monorepo - see discover_node_manifests()'s
            # docstring for why a root-only copy silently breaks `pnpm
            # install` for every workspace member.
            present = discover_node_manifests(worktree)
            install = node_install_command(detect_node_package_manager(worktree))
        else:
            present = [m for m in profile.dependency_manifests if (worktree / m).is_file()]
            install = profile.install_command or "true"
        dockerfile = build_copy_dockerfile(profile.image, present, install)
        tag = f"ai-os-sandbox-copy:{uuid.uuid4().hex[:16]}"

        with tempfile.TemporaryDirectory(prefix="ai-os-copybuild-") as tmp_dir:
            dockerfile_path = Path(tmp_dir) / "Dockerfile"
            dockerfile_path.write_text(dockerfile, encoding="utf-8")
            proc = await asyncio.create_subprocess_exec(
                self.docker_cli, "build", "-t", tag, "-f", str(dockerfile_path), str(worktree),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            try:
                out, _ = await asyncio.wait_for(
                    proc.communicate(), timeout=self.build_timeout_seconds
                )
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                feedback = build_feedback(
                    success=False, exit_code=124,
                    raw_output=f"Dependency install/build timed out after {self.build_timeout_seconds}s.",
                )
                return profile.image, ValidationResult(
                    False, 124, "Dependency install/build timed out.", feedback["output"]
                )
            if proc.returncode != 0:
                feedback = build_feedback(
                    success=False, exit_code=proc.returncode or 1,
                    raw_output="Dependency install failed while building the sandbox image:\n"
                    + out.decode(errors="replace"),
                )
                return profile.image, ValidationResult(
                    False, proc.returncode or 1, "Dependency install failed.", feedback["output"]
                )
        return tag, None

    async def _best_effort_rmi(self, tag: str) -> None:
        try:
            proc = await asyncio.create_subprocess_exec(
                self.docker_cli, "rmi", "-f", tag,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(proc.wait(), timeout=30.0)
        except Exception:
            pass  # cached layers persist for reuse; the thin top image ref is best-effort
