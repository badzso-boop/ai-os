"""Deterministic tests for Node package-manager detection + copy-isolation
Dockerfile + the mount-less argv (no Docker needed)."""
from __future__ import annotations

from ai_os.sandbox.container_runner import SANDBOX_PROFILES, build_docker_argv
from ai_os.sandbox.node_toolchain import (
    build_copy_dockerfile,
    detect_node_package_manager,
    node_install_command,
    node_test_command,
)


def test_detect_pnpm(tmp_path):
    (tmp_path / "package.json").write_text("{}")
    (tmp_path / "pnpm-lock.yaml").write_text("")
    assert detect_node_package_manager(tmp_path) == "pnpm"


def test_detect_yarn(tmp_path):
    (tmp_path / "yarn.lock").write_text("")
    assert detect_node_package_manager(tmp_path) == "yarn"


def test_detect_npm_from_lock(tmp_path):
    (tmp_path / "package-lock.json").write_text("{}")
    assert detect_node_package_manager(tmp_path) == "npm"


def test_detect_defaults_to_npm(tmp_path):
    (tmp_path / "package.json").write_text("{}")
    assert detect_node_package_manager(tmp_path) == "npm"


def test_pnpm_wins_over_others(tmp_path):
    (tmp_path / "pnpm-lock.yaml").write_text("")
    (tmp_path / "yarn.lock").write_text("")
    (tmp_path / "package-lock.json").write_text("{}")
    assert detect_node_package_manager(tmp_path) == "pnpm"


def test_install_and_test_commands():
    assert node_test_command("pnpm") == "pnpm test"
    assert node_test_command("npm") == "npm test"
    assert "pnpm install" in node_install_command("pnpm")
    assert "npm ci" in node_install_command("npm")


def test_copy_dockerfile_layers_deps_before_code_and_chowns():
    df = build_copy_dockerfile("node:22-alpine", ["package.json", "pnpm-lock.yaml"], "pnpm install")
    lines = df.splitlines()
    # corepack + non-root ownership so the hardened test user can write /app
    assert "RUN corepack enable || true" in lines
    assert "USER 1000:1000" in lines
    # deps (manifest copy + install) come BEFORE the code copy (cache reuse)
    manifest_idx = lines.index("COPY --chown=1000:1000 package.json ./package.json")
    install_idx = lines.index("RUN pnpm install")
    code_idx = lines.index("COPY --chown=1000:1000 . ./")
    assert manifest_idx < install_idx < code_idx


def test_copy_dockerfile_without_manifests_skips_install():
    df = build_copy_dockerfile("node:22-alpine", [], "pnpm install")
    assert "RUN pnpm install" not in df
    assert "COPY --chown=1000:1000 . ./" in df


def test_argv_without_mount_omits_bind_mount(tmp_path):
    argv = build_docker_argv(
        "docker", tmp_path, "n", SANDBOX_PROFILES["javascript"],
        image="ai-os-sandbox-copy:abc", mount=False, command="pnpm test",
    )
    # No -v bind mount, but the image + hardening + command are still there.
    assert "-v" not in argv
    assert "ai-os-sandbox-copy:abc" in argv
    assert argv[-3:] == ["sh", "-c", "pnpm test"]
    assert "--cap-drop=ALL" in argv
    assert argv[argv.index("--workdir") + 1] == "/app"


def test_argv_with_mount_still_binds(tmp_path):
    argv = build_docker_argv("docker", tmp_path, "n", SANDBOX_PROFILES["python"])
    assert "-v" in argv
    assert f"{tmp_path.resolve()}:/app:ro" in argv
