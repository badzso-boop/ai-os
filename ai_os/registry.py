"""External, updatable registry of project roots this tool can be pointed at.

Stored per-user at ~/.ai-os/projects.json (override via AI_OS_HOME) rather than inside
this repo checkout, since it records *other* projects on the machine, not ai-os itself.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

_NAME_PATTERN = re.compile(r"^[a-zA-Z0-9_-]+$")


def _home_dir() -> Path:
    override = os.environ.get("AI_OS_HOME")
    return Path(override) if override else Path.home() / ".ai-os"


def _registry_path() -> Path:
    return _home_dir() / "projects.json"


@dataclass
class ProjectEntry:
    name: str
    path: str
    added_at: str
    exists: bool = True


class ProjectNotFoundError(KeyError):
    pass


class InvalidProjectNameError(ValueError):
    pass


class ProjectPathError(ValueError):
    pass


def _load() -> dict:
    path = _registry_path()
    if not path.exists():
        return {"version": 1, "projects": {}}
    return json.loads(path.read_text(encoding="utf-8"))


def _save(data: dict) -> None:
    registry_path = _registry_path()
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=registry_path.parent, prefix=".projects-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp_name, registry_path)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise


def add(name: str, path: str, force: bool = False) -> ProjectEntry:
    if not _NAME_PATTERN.match(name):
        raise InvalidProjectNameError(
            f"Invalid project name {name!r}: only letters, digits, '-', '_' are allowed."
        )
    resolved = Path(path).expanduser().resolve()
    if not resolved.is_dir():
        raise ProjectPathError(f"{resolved} is not an existing directory.")

    data = _load()
    if name in data["projects"] and not force:
        raise ValueError(f"Project {name!r} is already registered (use force=True to overwrite).")

    entry = ProjectEntry(name=name, path=str(resolved), added_at=datetime.now(timezone.utc).isoformat())
    data["projects"][name] = {"path": entry.path, "added_at": entry.added_at}
    _save(data)
    return entry


def remove(name: str) -> None:
    data = _load()
    if name not in data["projects"]:
        raise ProjectNotFoundError(name)
    del data["projects"][name]
    _save(data)


def list_projects() -> list[ProjectEntry]:
    data = _load()
    entries = []
    for name, info in data["projects"].items():
        exists = Path(info["path"]).is_dir()
        entries.append(ProjectEntry(name=name, path=info["path"], added_at=info["added_at"], exists=exists))
    return sorted(entries, key=lambda e: e.name)


def resolve(name_or_path: str) -> Path:
    """Registered project name takes priority; otherwise treat the argument as a literal path."""
    data = _load()
    if name_or_path in data["projects"]:
        path = Path(data["projects"][name_or_path]["path"])
    else:
        path = Path(name_or_path).expanduser().resolve()
    if not path.is_dir():
        raise ProjectPathError(f"{path} is not an existing directory.")
    return path
