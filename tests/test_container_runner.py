"""Tests for `ai_os.sandbox.container_runner.EphemeralSandboxRunner`.

Per this project's testing philosophy (real behavior over mocks — see
Phase 2's `test_staging.py`/`test_orchestrator_integration.py`), these tests
run REAL Docker containers against `python:3.12-slim`, not mocked
subprocess calls. That's the only way to actually prove the security
hardening (network isolation, read-only mount) works, rather than just
trusting that the flags were accepted.

These tests require a working Docker daemon reachable without sudo (true on
this machine — the running user is in the `docker` group) and will pull
`python:3.12-slim` on first run if not already cached locally. Timeouts are
kept short (a few seconds) so the suite stays reasonably fast.
"""
from __future__ import annotations

import shutil
import subprocess
import time

import pytest

from ai_os.sandbox import container_runner
from ai_os.sandbox.container_runner import (
    EphemeralSandboxRunner,
    SandboxLanguageNotSupportedError,
    SandboxProfile,
    ValidationResult,
)

pytestmark = pytest.mark.skipif(
    shutil.which("docker") is None, reason="docker CLI not available"
)

# --- Test-only image note -------------------------------------------------
#
# The production `SANDBOX_PROFILES["python"]` entry (image `python:3.12-slim`,
# command `pip install -q -r requirements.txt 2>/dev/null; pytest`) assumes
# pytest is already available inside the container — but vanilla
# `python:3.12-slim` has no pytest preinstalled, and the container itself
# runs with `--network none`, so a real `pip install` inside it can never
# reach PyPI to fetch one. That tension is inherent to the spec's profile
# design (dependencies are expected to already be resolvable offline), not
# a bug in `container_runner.py`.
#
# To exercise real Docker behavior without either (a) weakening the network
# hardening we're specifically trying to prove, or (b) mutating the shared
# host's `python:3.12-slim` tag that other tools/pulls may rely on, these
# tests build a small locally-tagged derived image
# (`ai-os-sandbox-test-python:3.12-slim-pytest`, FROM python:3.12-slim +
# `pip install pytest`, built once with normal host network access — image
# *builds* are unaffected by the sandbox's own `--network none`, which only
# applies to the containers `EphemeralSandboxRunner` runs) and monkeypatches
# just the `image` field of the in-memory `SANDBOX_PROFILES["python"]` entry
# for the duration of these tests. `container_runner.py` itself is never
# edited — only this test file's view of the profile dict.
_TEST_PYTHON_IMAGE = "ai-os-sandbox-test-python:3.12-slim-pytest"


@pytest.fixture(scope="session", autouse=True)
def _build_test_python_image():
    """Build (once per test session) a python:3.12-slim derivative with
    pytest preinstalled, so sandboxed pytest runs work under --network none
    without ever needing network access *inside* the sandboxed container.
    """
    dockerfile = "FROM python:3.12-slim\nRUN pip install --no-cache-dir pytest\n"
    subprocess.run(
        ["docker", "build", "-t", _TEST_PYTHON_IMAGE, "-"],
        input=dockerfile,
        text=True,
        capture_output=True,
        check=True,
    )


@pytest.fixture(autouse=True)
def _use_test_python_image(monkeypatch):
    """Point the 'python' sandbox profile at the pytest-preinstalled test
    image (see note above) instead of vanilla python:3.12-slim, without
    touching the command (still exactly the production
    "pip install -q -r requirements.txt 2>/dev/null; pytest").
    """
    original = container_runner.SANDBOX_PROFILES["python"]
    monkeypatch.setitem(
        container_runner.SANDBOX_PROFILES,
        "python",
        SandboxProfile(_TEST_PYTHON_IMAGE, original.command),
    )


def _docker_ps_names(extra_args: list[str] | None = None) -> list[str]:
    """Best-effort listing of container names matching our naming scheme,
    used to assert no sandbox container leaks past its intended lifetime.
    """
    args = ["docker", "ps"] + (extra_args or []) + [
        "--filter",
        "name=ai-os-sandbox",
        "--format",
        "{{.Names}}",
    ]
    result = subprocess.run(args, capture_output=True, text=True, check=False)
    return [line for line in result.stdout.splitlines() if line.strip()]


@pytest.fixture()
def worktree(tmp_path):
    (tmp_path / "requirements.txt").write_text("")
    return tmp_path


async def test_happy_path_passing_pytest(worktree):
    (worktree / "test_happy.py").write_text(
        "def test_trivially_passes():\n    assert 1 + 1 == 2\n"
    )
    runner = EphemeralSandboxRunner(timeout_seconds=30.0)
    result = await runner.run_validation(worktree, "python")

    assert isinstance(result, ValidationResult)
    assert result.success is True
    assert result.exit_code == 0


async def test_failing_pytest_reports_failure_and_output(worktree):
    (worktree / "test_fail.py").write_text(
        "def test_deliberately_fails():\n"
        "    assert 1 == 2, 'ONE_IS_NOT_TWO_MARKER'\n"
    )
    runner = EphemeralSandboxRunner(timeout_seconds=30.0)
    result = await runner.run_validation(worktree, "python")

    assert result.success is False
    assert result.exit_code != 0
    # ANSI-stripped output should genuinely contain the assertion failure
    # text, not just report a bare non-zero exit code.
    assert "\x1b" not in result.output
    assert "ONE_IS_NOT_TWO_MARKER" in result.output


async def test_network_isolation_is_real(worktree):
    # This test PASSES (inside the container) only if the outbound
    # connection attempt genuinely fails — i.e. only if --network none is
    # actually enforced by the Docker daemon, not just accepted as a flag.
    # If network isolation were broken, the connection would succeed, the
    # inner pytest test would fail its `pytest.raises`, and
    # run_validation() would report success=False — which is exactly the
    # failure mode this test is designed to catch.
    (worktree / "test_network.py").write_text(
        "import socket\n"
        "import pytest\n"
        "\n"
        "def test_outbound_connection_is_blocked():\n"
        "    with pytest.raises(OSError):\n"
        "        socket.create_connection(('8.8.8.8', 53), timeout=3)\n"
    )
    runner = EphemeralSandboxRunner(timeout_seconds=30.0)
    result = await runner.run_validation(worktree, "python")

    assert result.success is True, (
        "Expected the outbound connection attempt to fail inside the "
        f"container (proving --network none), but validation reported "
        f"failure. Output:\n{result.output}"
    )
    assert result.exit_code == 0


async def test_readonly_mount_is_real(worktree):
    # Same shape of proof as the network test: this inner pytest test only
    # passes if writing into /app genuinely raises, which is only true if
    # the -v ...:/app:ro mount is truly read-only.
    (worktree / "test_readonly.py").write_text(
        "import pytest\n"
        "\n"
        "def test_writing_into_app_is_blocked():\n"
        "    with pytest.raises(OSError):\n"
        "        open('/app/should_fail.txt', 'w').write('x')\n"
    )
    runner = EphemeralSandboxRunner(timeout_seconds=30.0)
    result = await runner.run_validation(worktree, "python")

    assert result.success is True, (
        "Expected writing into /app to fail inside the container (proving "
        f"the read-only mount), but validation reported failure. Output:\n"
        f"{result.output}"
    )
    assert result.exit_code == 0
    # Belt-and-suspenders: confirm the file genuinely never landed on the
    # host side of the (supposedly read-only) bind mount either.
    assert not (worktree / "should_fail.txt").exists()


async def test_unsupported_language_raises(worktree):
    runner = EphemeralSandboxRunner(timeout_seconds=5.0)
    with pytest.raises(SandboxLanguageNotSupportedError):
        await runner.run_validation(worktree, "cobol")


async def test_timeout_returns_result_and_leaves_no_lingering_container(worktree):
    (worktree / "test_slow.py").write_text(
        "import time\n"
        "\n"
        "def test_sleeps_too_long():\n"
        "    time.sleep(30)\n"
    )
    runner = EphemeralSandboxRunner(timeout_seconds=3.0)

    start = time.monotonic()
    result = await runner.run_validation(worktree, "python")
    elapsed = time.monotonic() - start

    assert result.success is False
    assert result.exit_code == 124
    assert "timed out" in result.summary.lower()
    # Generous margin over the 3s configured timeout to allow for
    # container startup + docker-kill round trip, while still proving we
    # didn't just wait for the full 30s sleep.
    assert elapsed < 20.0

    # Best-effort poll: confirm no leftover ai-os-sandbox-* container is
    # still running after the timeout/kill path completed.
    for _ in range(5):
        names = _docker_ps_names(["-a"])
        if not names:
            break
        time.sleep(1.0)
    assert _docker_ps_names(["-a"]) == []


async def test_setup_commands_under_mount_isolation_combines_with_test_command(worktree):
    """Under mount isolation (Python), setup_commands runs in the SAME container
    as the test command, combined via && with the setup-done marker."""
    import json
    ai_os_dir = worktree / ".ai-os"
    ai_os_dir.mkdir()
    (ai_os_dir / "sandbox.json").write_text(json.dumps({
        "setup_commands": ["echo 'setup_ran' > /tmp/setup.txt"],
    }))
    (worktree / "test_setup.py").write_text(
        "import os\n"
        "def test_setup_effect():\n"
        "    assert os.path.exists('/tmp/setup.txt')\n"
        "    with open('/tmp/setup.txt') as f:\n"
        "        assert f.read().strip() == 'setup_ran'\n"
    )
    runner = EphemeralSandboxRunner(timeout_seconds=30.0)
    result = await runner.run_validation(worktree, "python")

    assert result.success is True, f"Expected setup_commands + pytest pass, got:\n{result.output}"
    assert result.exit_code == 0

