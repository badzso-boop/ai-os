"""Deterministic tests for Node package-manager detection + copy-isolation
Dockerfile + the mount-less argv (no Docker needed)."""
from __future__ import annotations

from ai_os.sandbox.container_runner import SANDBOX_PROFILES, build_docker_argv
from ai_os.sandbox.node_toolchain import (
    build_copy_dockerfile,
    detect_node_package_manager,
    discover_node_manifests,
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


# -- discover_node_manifests (pnpm/yarn/npm workspace support) --------------


def test_discover_manifests_single_package_unchanged(tmp_path):
    (tmp_path / "package.json").write_text("{}")
    (tmp_path / "pnpm-lock.yaml").write_text("")
    assert discover_node_manifests(tmp_path) == ["package.json", "pnpm-lock.yaml"]


def test_discover_manifests_pnpm_workspace_includes_members(tmp_path):
    (tmp_path / "package.json").write_text('{"name": "root"}')
    (tmp_path / "pnpm-lock.yaml").write_text("")
    (tmp_path / "pnpm-workspace.yaml").write_text("packages:\n  - apps/*\n  - packages/*\n")
    (tmp_path / "apps" / "web").mkdir(parents=True)
    (tmp_path / "apps" / "web" / "package.json").write_text('{"name": "web"}')
    (tmp_path / "packages" / "i18n").mkdir(parents=True)
    (tmp_path / "packages" / "i18n" / "package.json").write_text('{"name": "i18n"}')

    manifests = discover_node_manifests(tmp_path)

    assert "pnpm-workspace.yaml" in manifests
    assert "apps/web/package.json" in manifests
    assert "packages/i18n/package.json" in manifests
    # root manifests still come first (deterministic, matches the plain case)
    assert manifests[0] == "package.json"
    assert manifests[1] == "pnpm-lock.yaml"


def test_discover_manifests_npm_yarn_workspaces_field(tmp_path):
    (tmp_path / "package.json").write_text('{"name": "root", "workspaces": ["packages/*"]}')
    (tmp_path / "yarn.lock").write_text("")
    (tmp_path / "packages" / "core").mkdir(parents=True)
    (tmp_path / "packages" / "core" / "package.json").write_text('{"name": "core"}')

    manifests = discover_node_manifests(tmp_path)

    assert "packages/core/package.json" in manifests
    assert "pnpm-workspace.yaml" not in manifests  # no such file here


def test_discover_manifests_skips_node_modules_and_build_dirs(tmp_path):
    (tmp_path / "package.json").write_text('{"workspaces": ["packages/*"]}')
    (tmp_path / "node_modules" / "some-dep").mkdir(parents=True)
    (tmp_path / "node_modules" / "some-dep" / "package.json").write_text("{}")
    (tmp_path / "packages" / "core" / "dist").mkdir(parents=True)
    (tmp_path / "packages" / "core" / "dist" / "package.json").write_text("{}")
    (tmp_path / "packages" / "core" / "package.json").write_text('{"name": "core"}')

    manifests = discover_node_manifests(tmp_path)

    assert manifests == ["package.json", "packages/core/package.json"]


def test_discover_manifests_skips_ai_os_worktree_admin_dir(tmp_path):
    # A stale/leftover .ai-os/worktrees/<task>/ (untracked, self-referential
    # nested checkout of this same repo) must never be scanned as if it were
    # a real workspace member.
    (tmp_path / "package.json").write_text('{"workspaces": ["packages/*"]}')
    (tmp_path / "packages" / "core").mkdir(parents=True)
    (tmp_path / "packages" / "core" / "package.json").write_text('{"name": "core"}')
    stale = tmp_path / ".ai-os" / "worktrees" / "TASK-8" / "packages" / "core"
    stale.mkdir(parents=True)
    (stale / "package.json").write_text('{"name": "core"}')

    manifests = discover_node_manifests(tmp_path)

    assert manifests == ["package.json", "packages/core/package.json"]


def test_discover_manifests_non_workspace_root_with_workspaces_key_absent(tmp_path):
    # A package.json with no "workspaces" key and no pnpm-workspace.yaml is
    # NOT treated as a workspace, even if a nested package.json happens to
    # exist somewhere (e.g. a vendored example) - don't over-scan.
    (tmp_path / "package.json").write_text('{"name": "root"}')
    (tmp_path / "examples" / "demo").mkdir(parents=True)
    (tmp_path / "examples" / "demo" / "package.json").write_text('{"name": "demo"}')

    assert discover_node_manifests(tmp_path) == ["package.json"]


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
