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

from pathlib import Path

NODE_LANGUAGES = ("javascript", "typescript")

# All the Node dependency manifests we COPY (deps layer) — whichever the repo
# actually has. package.json is the always-present one; the lockfiles pin the
# package manager + exact versions.
NODE_MANIFESTS = ("package.json", "pnpm-lock.yaml", "yarn.lock", "package-lock.json")

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
