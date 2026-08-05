"""Detect security-sensitive files a task wants to write, so AI-OS can force a
human review instead of auto-merging them.

The risk: an agent can write a `.github/workflows/*.yml` (or a build script, a
Dockerfile, a secrets file) that the sandbox never runs — but which executes in
the project's REAL CI with the repo's secrets once merged. So a task touching any
of these is flagged: the plan-review gate warns, and a direct `--merge-to-main`
is refused (the change must go through a PR a human reviews). This is the
human-in-the-loop answer to validator-gaming + CI-tampering.
"""
from __future__ import annotations

from fnmatch import fnmatch

# Glob patterns (matched against POSIX repo-relative paths) considered sensitive.
SENSITIVE_PATTERNS = (
    # CI / pipeline definitions — run with repo secrets post-merge.
    ".github/workflows/*",
    ".github/actions/*",
    ".gitlab-ci.yml",
    ".circleci/*",
    "Jenkinsfile",
    "azure-pipelines.yml",
    "bitbucket-pipelines.yml",
    ".drone.yml",
    # Package install hooks / build that can run arbitrary code.
    "Dockerfile",
    "*/Dockerfile",
    "docker-compose*.yml",
    "docker-compose*.yaml",
    # Secrets / credentials.
    ".env",
    ".env.*",
    "*.pem",
    "*.key",
    "**/secrets*",
    "id_rsa",
    # AI-OS's own config — tampering here changes how AI-OS validates.
    ".ai-os/*",
    # Git hooks.
    ".git/hooks/*",
    ".husky/*",
)


def _matches(path: str, pattern: str) -> bool:
    # fnmatch's `*` also spans `/`, which is what we want for the `**` and dir
    # patterns here; a plain `*/Dockerfile` still matches `a/b/Dockerfile`.
    return fnmatch(path, pattern)


def sensitive_paths(paths) -> list[str]:
    """Return the subset of `paths` (repo-relative POSIX) that match a sensitive
    pattern — sorted + de-duplicated."""
    hits = {p for p in paths if any(_matches(p, pat) for pat in SENSITIVE_PATTERNS)}
    return sorted(hits)
