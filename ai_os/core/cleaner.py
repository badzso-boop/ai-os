"""`ai-os clean` — reclaim disk/state AI-OS leaves behind, especially after a
crash (SIGKILL/OOM can't run the best-effort teardowns).

On a long-running shared host this matters: per-task dependency images
(`ai-os-sandbox-dep:*`, `ai-os-sandbox-copy:*`) accumulate, and a hard crash can
leak DB sidecar containers + their `--internal` networks. This walks AI-OS's own
naming scheme (never touching unrelated Docker objects) and removes them; with a
project path it can also prune stale git worktrees + leftover `ai-os/*` branches.

All discovery is separated from removal so the CLI can show a dry-run first, and
so the git side is unit-testable against a real disposable repo without Docker.
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path

# AI-OS's own Docker naming (see container_runner.py / db_services.py). We only
# ever remove objects matching these — never anything else on the host.
IMAGE_REFERENCE_FILTERS = ("ai-os-sandbox-dep", "ai-os-sandbox-copy")
CONTAINER_NAME_FILTERS = ("ai-os-sandbox-", "ai-os-setup-", "ai-os-sbx-")
NETWORK_NAME_FILTER = "ai-os-sbx-net-"


@dataclass
class DockerArtifacts:
    images: list[str] = field(default_factory=list)      # "repo:tag" strings
    containers: list[str] = field(default_factory=list)  # names
    networks: list[str] = field(default_factory=list)    # names

    def is_empty(self) -> bool:
        return not (self.images or self.containers or self.networks)


def _run(argv: list[str], cwd: Path | None = None) -> tuple[int, str]:
    try:
        proc = subprocess.run(argv, cwd=cwd, capture_output=True, text=True)
    except FileNotFoundError:
        return 127, ""
    return proc.returncode, proc.stdout


def _lines(text: str) -> list[str]:
    return [ln.strip() for ln in text.splitlines() if ln.strip()]


def list_docker_artifacts(docker_cli: str = "docker") -> DockerArtifacts:
    """Discover AI-OS's leaked/cached Docker images, containers, and networks.
    Returns empties if `docker` isn't available (nothing to do)."""
    art = DockerArtifacts()
    seen_images: set[str] = set()
    for ref in IMAGE_REFERENCE_FILTERS:
        rc, out = _run([docker_cli, "images", "--filter", f"reference={ref}*",
                        "--format", "{{.Repository}}:{{.Tag}}"])
        if rc == 0:
            for name in _lines(out):
                if name not in seen_images:
                    seen_images.add(name)
                    art.images.append(name)
    seen_containers: set[str] = set()
    for name_filter in CONTAINER_NAME_FILTERS:
        rc, out = _run([docker_cli, "ps", "-a", "--filter", f"name={name_filter}",
                        "--format", "{{.Names}}"])
        if rc == 0:
            for name in _lines(out):
                if name not in seen_containers:
                    seen_containers.add(name)
                    art.containers.append(name)
    rc, out = _run([docker_cli, "network", "ls", "--filter", f"name={NETWORK_NAME_FILTER}",
                    "--format", "{{.Name}}"])
    if rc == 0:
        art.networks = _lines(out)
    return art


def remove_docker_artifacts(art: DockerArtifacts, docker_cli: str = "docker") -> list[str]:
    """Remove the discovered artifacts (containers first, then networks, then
    images — dependency order). Best-effort; returns a log of what was removed."""
    removed: list[str] = []
    for name in art.containers:
        rc, _ = _run([docker_cli, "rm", "-f", name])
        if rc == 0:
            removed.append(f"container {name}")
    for name in art.networks:
        rc, _ = _run([docker_cli, "network", "rm", name])
        if rc == 0:
            removed.append(f"network {name}")
    for name in art.images:
        rc, _ = _run([docker_cli, "rmi", "-f", name])
        if rc == 0:
            removed.append(f"image {name}")
    return removed


# -- git side (per-project) --------------------------------------------------


def list_ai_os_branches(repo_root: Path) -> list[str]:
    """Leftover `ai-os/*` branches (blocked-task branches kept for inspection,
    or an interrupted epic's integration/task branches)."""
    rc, out = _run(["git", "branch", "--list", "ai-os/*"], cwd=Path(repo_root))
    if rc != 0:
        return []
    return sorted(ln.lstrip("*+ ").strip() for ln in _lines(out))


def prune_worktrees(repo_root: Path) -> None:
    _run(["git", "worktree", "prune"], cwd=Path(repo_root))


def delete_branches(repo_root: Path, branches: list[str]) -> list[str]:
    """Force-delete the given branches. Returns the ones actually deleted."""
    deleted: list[str] = []
    for branch in branches:
        rc, _ = _run(["git", "branch", "-D", branch], cwd=Path(repo_root))
        if rc == 0:
            deleted.append(branch)
    return deleted
