"""Real-Docker tests for the two-phase dependency flow
(`EphemeralSandboxRunner`): phase 1 installs a project's declared dependencies
in a network-enabled per-task image build, phase 2 runs the tests against that
image with `--network none`.

These prove the thing that was impossible before: a project whose tests need a
third-party package (beyond the standard library + pytest) now validates
successfully, because the dependency is installed in the (network-enabled) build
rather than inside the (network-isolated) test run. A tiny, fast, pure-Python
package (`six`) is used so the build stays quick.

Requires Docker; skipped otherwise. Builds a small python:3.12-slim+pytest base
(shared, cached) plus a per-requirements dependency image.
"""
from __future__ import annotations

import shutil
import subprocess

import pytest

from ai_os.sandbox import container_runner
from ai_os.sandbox.container_runner import EphemeralSandboxRunner, SandboxProfile

pytestmark = pytest.mark.skipif(
    shutil.which("docker") is None, reason="docker CLI not available"
)

_TEST_PYTHON_IMAGE = "ai-os-sandbox-test-python:3.12-slim-pytest"


@pytest.fixture(scope="session", autouse=True)
def _build_test_python_image():
    dockerfile = "FROM python:3.12-slim\nRUN pip install --no-cache-dir pytest\n"
    subprocess.run(
        ["docker", "build", "-t", _TEST_PYTHON_IMAGE, "-"],
        input=dockerfile, text=True, capture_output=True, check=True,
    )


@pytest.fixture(autouse=True)
def _two_phase_python_profile(monkeypatch):
    """Point the python profile at the pytest-preinstalled test base image, but
    KEEP the two-phase dependency config (unlike test_container_runner.py, which
    strips it) so these tests actually exercise phase 1."""
    monkeypatch.setitem(
        container_runner.SANDBOX_PROFILES,
        "python",
        SandboxProfile(
            image=_TEST_PYTHON_IMAGE,
            command="pytest -p no:cacheprovider",
            dependency_manifests=("requirements.txt",),
            install_command="pip install --no-cache-dir -r requirements.txt",
        ),
    )


async def test_declared_dependency_is_installed_and_importable(tmp_path):
    # `six` is NOT in the base image; under the OLD single-phase --network none
    # flow this import would fail (no way to install it). Two-phase installs it
    # in the build, so the network-free test run can import it.
    (tmp_path / "requirements.txt").write_text("six\n")
    (tmp_path / "test_dep.py").write_text(
        "import six\n"
        "def test_six_is_importable():\n"
        "    assert six.PY3 is True\n"
    )
    runner = EphemeralSandboxRunner(timeout_seconds=60.0, build_timeout_seconds=300.0)
    result = await runner.run_validation(tmp_path, "python")

    assert result.success is True, f"expected pass, got:\n{result.output}"
    assert result.exit_code == 0


async def test_bad_requirement_reports_validation_failure_not_crash(tmp_path):
    # A nonexistent package must surface as a validation FAILURE (so the agent
    # can fix its requirements.txt), not raise an infra exception.
    (tmp_path / "requirements.txt").write_text(
        "ai-os-nonexistent-package-xyzzy==9.9.9\n"
    )
    (tmp_path / "test_never_runs.py").write_text(
        "def test_placeholder():\n    assert True\n"
    )
    runner = EphemeralSandboxRunner(timeout_seconds=60.0, build_timeout_seconds=300.0)
    result = await runner.run_validation(tmp_path, "python")

    assert result.success is False
    assert "dependency install failed" in result.output.lower()


async def test_no_requirements_falls_back_to_base_image(tmp_path):
    # Empty requirements.txt -> no phase-1 build; stdlib+pytest still validates.
    (tmp_path / "requirements.txt").write_text("")
    (tmp_path / "test_stdlib.py").write_text(
        "def test_ok():\n    assert 2 + 2 == 4\n"
    )
    runner = EphemeralSandboxRunner(timeout_seconds=60.0)
    result = await runner.run_validation(tmp_path, "python")
    assert result.success is True
    assert result.exit_code == 0
