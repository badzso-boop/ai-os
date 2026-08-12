"""Node package-manager detection + copy-isolation Dockerfile generation.

Why copy-isolation for Node (not the bind-mount + out-of-tree node_modules the
other ecosystems use): Node's dependency resolution is cwd-relative
(`node_modules` walked up from the importing file), and modern bundler-based
runners (Vite/Vitest/Next) do their OWN resolution that ignores `NODE_PATH`. So
an out-of-tree `/deps/node_modules` pointed at by `NODE_PATH` is fragile for
exactly the tools real projects use. The robust approach is to COPY the code AND
install `node_modules` in the same `/app` inside a per-task image, then run the
tests there with no bind mount — so resolution is completely normal.

Package manager is detected from the lockfile the repo commits, and enabled via
`corepack` (bundled with Node 16.9+), so pnpm / yarn / npm all work.
"""
from __future__ import annotations

import json

from pathlib import Path

NODE_LANGUAGES = ("javascript", "typescript")

# All the Node dependency manifests we COPY (deps layer) — whichever the repo
# actually has. package.json is the always-present one; the lockfiles pin the
# package manager + exact versions.
NODE_MANIFESTS = ("package.json", "pnpm-lock.yaml", "yarn.lock", "package-lock.json")

# The pnpm-workspace.yaml file itself has no dependencies of its own, but its
# mere presence is what tells pnpm "this is a workspace" — without it, pnpm
# treats the root package.json as a standalone package even though the
# lockfile is workspace-shaped, which is exactly the mismatch that causes the
# failure discover_node_manifests() below exists to avoid.
_PNPM_WORKSPACE_FILE = "pnpm-workspace.yaml"

# Directories never worth descending into when hunting for workspace member
# package.json files - dependency trees, VCS metadata, and common build
# output. node_modules shouldn't exist yet at this point (nothing's been
# installed), but a worktree copied from a repo that committed it (or a
# leftover from a prior local run) would make the scan needlessly slow/wrong.
# `.ai-os` specifically guards against AI-OS's own `.ai-os/worktrees/<task>/`
# admin directories (an untracked, self-referential nested checkout of this
# same repo left over from a prior/crashed run) being scanned as if their
# contents were real workspace members - self-inflicted duplicate manifests.
_SKIP_DIRS = frozenset({
    "node_modules", ".git", ".next", ".turbo", ".cache", "dist", "build", "out", ".ai-os",
})


def _is_npm_or_yarn_workspace_root(worktree: Path) -> bool:
    """True if the root package.json declares an npm/yarn `"workspaces"` key
    (either the array form or the `{"packages": [...]}` object form)."""
    package_json = worktree / "package.json"
    if not package_json.is_file():
        return False
    try:
        data = json.loads(package_json.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return isinstance(data, dict) and "workspaces" in data


def discover_node_manifests(worktree: Path) -> list[str]:
    """The manifest files (relative POSIX paths) the phase-1 dependency image
    build needs to COPY before installing.

    For a plain, single-package Node project this is just the root-level
    files in NODE_MANIFESTS that actually exist - unchanged behavior.

    For a pnpm/yarn/npm WORKSPACE monorepo (pnpm-workspace.yaml present, or
    package.json declares `"workspaces"`), the root manifest alone isn't
    enough: the package manager needs to see every workspace member's own
    package.json to recognize the workspace at all. Without them, `pnpm
    install` silently installs only the root's own (usually few or zero)
    direct dependencies, producing a `node_modules` that doesn't match the
    real workspace shape once the actual code (with its member package.json
    files) is copied in phase 2 - and the first `pnpm` command run after that
    (e.g. a `setup_commands` migration/generate step) then tries to
    self-repair via an implicit `pnpm install`, which fails outright because
    that phase runs with `--network none`.

    We don't parse the workspace glob patterns (pnpm-workspace.yaml's
    `packages:` list, npm/yarn's `workspaces` array) - simpler and just as
    correct to gather every package.json in the worktree (skipping
    node_modules/build-output dirs): a member outside the declared globs adds
    a harmless extra layer-cache input, not a correctness problem, and it
    avoids a YAML-parsing dependency for pnpm's config format.
    """
    worktree = Path(worktree)
    manifests = [m for m in NODE_MANIFESTS if (worktree / m).is_file()]

    is_workspace = (worktree / _PNPM_WORKSPACE_FILE).is_file() or _is_npm_or_yarn_workspace_root(
        worktree
    )
    if not is_workspace:
        return manifests

    if (worktree / _PNPM_WORKSPACE_FILE).is_file():
        manifests.append(_PNPM_WORKSPACE_FILE)

    member_package_jsons: list[str] = []
    for path in worktree.rglob("package.json"):
        if path == worktree / "package.json":
            continue  # already in `manifests`
        if any(part in _SKIP_DIRS for part in path.relative_to(worktree).parts):
            continue
        member_package_jsons.append(path.relative_to(worktree).as_posix())

    return manifests + sorted(member_package_jsons)

# Lockfile -> package manager (first match wins); npm is the fallback.
_LOCKFILE_PM = (
    ("pnpm-lock.yaml", "pnpm"),
    ("yarn.lock", "yarn"),
    ("package-lock.json", "npm"),
)

# Install honoring the lockfile when present, falling back to a plain install so
# a missing/stale lockfile doesn't hard-fail the build.
#
# pnpm note: pnpm 10+ blocks dependency build scripts by default and EXITS
# non-zero (`ERR_PNPM_IGNORED_BUILDS`) — which breaks packages like esbuild
# (vitest's dep) that need a postinstall to fetch their native binary. We opt
# into running them (`dangerouslyAllowAllBuilds`). This is the same residual
# supply-chain trust as any CI `npm install`/`npm ci` (which run postinstall by
# default) — and the phase-1 build is exactly where that's already accepted; the
# untrusted agent TEST code still runs network-free.
_INSTALL = {
    "pnpm": "pnpm config set dangerouslyAllowAllBuilds true; pnpm install --frozen-lockfile || pnpm install",
    "yarn": "yarn install --immutable || yarn install",
    "npm": "npm ci || npm install",
}
_TEST = {"pnpm": "pnpm test", "yarn": "yarn test", "npm": "npm test"}


def detect_node_package_manager(worktree_path: Path) -> str:
    for lockfile, pm in _LOCKFILE_PM:
        if (Path(worktree_path) / lockfile).is_file():
            return pm
    return "npm"


def node_install_command(pm: str) -> str:
    return _INSTALL[pm]


def node_test_command(pm: str) -> str:
    return _TEST[pm]


def build_copy_dockerfile(base_image: str, manifests: list[str], install_command: str) -> str:
    """Dockerfile for the copy-isolation build.

    - `corepack enable` (as root) sets up the pnpm/yarn shims globally.
    - `/app` is pre-owned by uid 1000 and everything is `COPY --chown=1000:1000`
      + installed as uid 1000, so the hardened (`--user 1000:1000`) test run can
      read AND write /app (coverage reports, Vite caches, snapshots, …) — a plain
      root-owned copy would leave /app unwritable for the test user.
    - deps layer (manifests + install) comes BEFORE the code layer, so Docker's
      build cache reuses installed node_modules across attempts (only the code
      layer changes when the agent edits a file).
    """
    lines = [
        f"FROM {base_image}",
        "RUN corepack enable || true",
        "RUN mkdir -p /app && chown 1000:1000 /app",
        "WORKDIR /app",
        "USER 1000:1000",
    ]
    if manifests:
        for manifest in manifests:
            lines.append(f"COPY --chown=1000:1000 {manifest} ./{manifest}")
        lines.append(f"RUN {install_command}")
    lines.append("COPY --chown=1000:1000 . ./")
    return "\n".join(lines) + "\n"
