"""Ephemeral database sidecar on an isolated Docker network, for validation
runs whose tests need a real database (declared via `.ai-os/sandbox.json`).

Security model: the network is created with `--internal`, which Docker gives no
gateway to the host/outside — so the setup + test containers joined to it can
reach the database (by its `--network-alias` hostname) but **cannot reach the
internet**. That preserves the "untrusted agent code can't exfiltrate" property
of the default `--network none` runs while still allowing DB-backed tests.

Follows the module-wide convention (see `container_runner.py`): shell out to the
`docker` CLI via `asyncio.create_subprocess_exec`, no docker SDK. Everything is
best-effort torn down in a `finally` so a failed run never leaks a container or
network.
"""
from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass

from ai_os.sandbox.sandbox_config import DatabaseService


class DatabaseSandboxError(RuntimeError):
    """The database sidecar failed to start or never became ready."""


@dataclass
class RunningDatabase:
    network: str      # the --internal docker network name
    container: str    # the DB container name
    hostname: str     # the DNS alias the test/setup containers connect to


class DatabaseSandbox:
    """Starts/stops a throwaway DB container on a fresh `--internal` network."""

    def __init__(
        self,
        docker_cli: str = "docker",
        ready_timeout_seconds: float = 90.0,
        poll_interval_seconds: float = 1.0,
    ) -> None:
        self.docker_cli = docker_cli
        self.ready_timeout_seconds = ready_timeout_seconds
        self.poll_interval_seconds = poll_interval_seconds

    async def start(self, db: DatabaseService) -> RunningDatabase:
        token = uuid.uuid4().hex[:12]
        network = f"ai-os-sbx-net-{token}"
        container = f"ai-os-sbx-db-{token}"

        await self._run_ok(
            [self.docker_cli, "network", "create", "--internal", network],
            error="create the isolated sandbox network",
        )
        try:
            # NB: no --cap-drop=ALL here. That hardening is for the untrusted
            # test container (which runs agent-written code); the DB is a trusted
            # official image whose entrypoint starts as root and drops to the
            # `postgres` user via gosu (needs CHOWN/SETUID/SETGID) — dropping all
            # caps makes it exit on startup. Isolation comes from the --internal
            # network (no egress) + --memory limit + a throwaway --rm container.
            argv = [
                self.docker_cli, "run", "-d",
                "--rm",
                "--name", container,
                "--network", network,
                "--network-alias", db.hostname,
                f"--memory={db.memory}",
            ]
            for key, value in db.env.items():
                argv += ["-e", f"{key}={value}"]
            argv.append(db.image)
            await self._run_ok(argv, error=f"start the database container ({db.image})")

            await self._await_ready(container, db.ready_command)
        except Exception:
            # Roll back the partially-created sidecar so nothing leaks.
            await self.teardown(RunningDatabase(network=network, container=container, hostname=db.hostname))
            raise

        return RunningDatabase(network=network, container=container, hostname=db.hostname)

    async def _await_ready(self, container: str, ready_command: str) -> None:
        loop = asyncio.get_event_loop()
        deadline = loop.time() + self.ready_timeout_seconds
        last_output = ""
        while loop.time() < deadline:
            proc = await asyncio.create_subprocess_exec(
                self.docker_cli, "exec", container, "sh", "-c", ready_command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            out, _ = await proc.communicate()
            if proc.returncode == 0:
                return
            last_output = out.decode(errors="replace")
            await asyncio.sleep(self.poll_interval_seconds)
        raise DatabaseSandboxError(
            f"database container {container!r} not ready within "
            f"{self.ready_timeout_seconds}s (last probe: {last_output.strip()!r})"
        )

    async def teardown(self, running: RunningDatabase) -> None:
        # `docker rm -f` stops+removes even a still-running container; `--rm` on
        # start also reaps it, so this is belt-and-suspenders. Then drop the
        # network (only possible once no container is attached).
        await self._run_quiet([self.docker_cli, "rm", "-f", running.container])
        await self._run_quiet([self.docker_cli, "network", "rm", running.network])

    async def _run_ok(self, argv: list[str], *, error: str) -> None:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        out, _ = await proc.communicate()
        if proc.returncode != 0:
            raise DatabaseSandboxError(
                f"failed to {error}: {out.decode(errors='replace').strip()}"
            )

    async def _run_quiet(self, argv: list[str]) -> None:
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(proc.wait(), timeout=30.0)
        except Exception:
            pass  # best-effort teardown
