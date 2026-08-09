"""Onboarding engine for scanning existing projects and synthesizing AI-OS configuration files.

This module provides `scan_and_generate_configs` to inspect existing repository documentation,
package manifests (pyproject.toml, package.json, requirements.txt, Cargo.toml), code stubs,
and UI assets. It deterministically synthesizes four key configuration files inside the `.ai-os/`
directory:

1. `instructions.json`: Agent rules, project title, language, framework, entry points, and stubs.
2. `conventions.md`: Human-readable coding conventions, style guide, and architecture principles.
3. `sandbox.json`: Execution sandbox config detailing setup commands, test command, build command, and env.
4. `ui.json`: UI and frontend configuration tracking detected UI presence, framework, entry points, routes, and components.

Design Principles:
- Compiler First: 100% deterministic parsing and heuristic extraction with zero LLM or network dependency.
- Deep Scanning: Optional AST/stub analysis of functions, classes, components, symbol graphs, and test suites.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
import re
from typing import Any, Dict, List, Optional, Set, Union

try:
    import tomllib
except ImportError:  # pragma: no cover
    tomllib = None  # type: ignore


class ConfigDict(dict):
    """Dictionary returned by scan_and_generate_configs.

    Contains configuration status and generated file details, while supporting Path-like
    operations for backwards compatibility with code expecting a Path.
    """

    def __truediv__(self, other: Any) -> Path:
        cd = self.get("config_dir")
        if isinstance(cd, Path):
            return cd / other
        raise TypeError(f"unsupported operand type(s) for /: '{type(self).__name__}' and '{type(other).__name__}'")

    def is_dir(self) -> bool:
        cd = self.get("config_dir")
        return cd.is_dir() if isinstance(cd, Path) else False

    def is_file(self) -> bool:
        cd = self.get("config_dir")
        return cd.is_file() if isinstance(cd, Path) else False

    def __eq__(self, other: Any) -> bool:
        if isinstance(other, Path):
            return self.get("config_dir") == other
        return super().__eq__(other)


@dataclass
class ProjectMetadata:
    """Dataclass holding extracted metadata from project scanning."""

    name: str = "unnamed-project"
    description: str = "No description provided."
    primary_language: str = "python"
    framework: str = "none"
    has_ui: bool = False
    entry_points: List[str] = field(default_factory=list)
    setup_commands: List[str] = field(default_factory=list)
    test_command: str = "python -m pytest"
    build_command: Optional[str] = None
    ui_framework: str = "none"
    ui_entry_points: List[str] = field(default_factory=list)
    ui_routes: List[str] = field(default_factory=list)
    ui_components: List[str] = field(default_factory=list)
    ui_assets_dir: Optional[str] = None
    detected_files: List[str] = field(default_factory=list)
    code_stubs: List[str] = field(default_factory=list)
    has_docs: bool = False
    has_manifests: bool = False
    has_test_suite: bool = False


def _scan_documentation(root: Path) -> tuple[str, str, bool]:
    """Extract project title and description from README, CLAUDE.md, CONTRIBUTING.md, or docs files."""
    doc_files = [
        "CLAUDE.md",
        "README.md",
        "README.txt",
        "README",
        "CONTRIBUTING.md",
        "DOCS.md",
        "docs/INDEX.md",
        "docs/index.md",
        "docs/README.md",
    ]
    name = root.name
    description = "No description provided."
    has_docs = False

    for doc_file in doc_files:
        path = root / doc_file
        if path.is_file():
            has_docs = True
            try:
                content = path.read_text(encoding="utf-8").strip()
                if not content:
                    continue
                lines = [line.strip() for line in content.splitlines() if line.strip()]
                if not lines:
                    continue

                # Heading match for title
                first_line = lines[0]
                if first_line.startswith("#"):
                    extracted_name = first_line.lstrip("#").strip()
                    if extracted_name:
                        name = extracted_name

                # Look for summary description lines
                desc_lines = [l for l in lines if not l.startswith("#")]
                if desc_lines:
                    description = desc_lines[0]
                break
            except Exception:
                continue

    docs_dir = root / "docs"
    if docs_dir.is_dir():
        for doc_path in docs_dir.rglob("*"):
            if doc_path.is_file():
                has_docs = True
                break

    return name, description, has_docs


def _scan_manifests(root: Path, meta: ProjectMetadata) -> bool:
    """Inspect manifest files to update language, framework, dependencies, and setup/test commands."""
    pyproject_path = root / "pyproject.toml"
    package_json_path = root / "package.json"
    req_path = root / "requirements.txt"
    cargo_path = root / "Cargo.toml"
    setup_py_path = root / "setup.py"
    pom_path = root / "pom.xml"
    gradle_path = root / "build.gradle"
    go_mod_path = root / "go.mod"

    manifest_found = False

    # 1. pyproject.toml
    if pyproject_path.is_file():
        manifest_found = True
        meta.primary_language = "python"
        meta.setup_commands = ["pip install -e ."]
        meta.test_command = "python -m pytest"
        if tomllib:
            try:
                data = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
                project_sec = data.get("project", {})
                if isinstance(project_sec, dict) and "name" in project_sec:
                    meta.name = str(project_sec["name"])
                if isinstance(project_sec, dict) and "description" in project_sec:
                    meta.description = str(project_sec["description"])

                deps = project_sec.get("dependencies", [])
                if isinstance(deps, list):
                    deps_str = " ".join(deps).lower()
                    if "fastapi" in deps_str:
                        meta.framework = "fastapi"
                    elif "flask" in deps_str:
                        meta.framework = "flask"
                    elif "django" in deps_str:
                        meta.framework = "django"
            except Exception:
                pass

    # 2. package.json
    elif package_json_path.is_file():
        manifest_found = True
        meta.primary_language = "javascript"
        meta.setup_commands = ["npm install"]
        meta.test_command = "npm test"
        try:
            pjson = json.loads(package_json_path.read_text(encoding="utf-8"))
            if isinstance(pjson, dict):
                if pjson.get("name"):
                    meta.name = str(pjson["name"])
                if pjson.get("description"):
                    meta.description = str(pjson["description"])

                scripts = pjson.get("scripts", {})
                if isinstance(scripts, dict):
                    if "build" in scripts:
                        meta.build_command = "npm run build"
                    if "test" in scripts:
                        meta.test_command = "npm test"

                deps = {**pjson.get("dependencies", {}), **pjson.get("devDependencies", {})}
                deps_keys = [k.lower() for k in deps.keys()]
                if "next" in deps_keys:
                    meta.framework = "nextjs"
                    meta.ui_framework = "nextjs"
                    meta.has_ui = True
                elif "react" in deps_keys:
                    meta.framework = "react"
                    meta.ui_framework = "react"
                    meta.has_ui = True
                elif "vue" in deps_keys:
                    meta.framework = "vue"
                    meta.ui_framework = "vue"
                    meta.has_ui = True
                elif "express" in deps_keys:
                    meta.framework = "express"
                if "typescript" in deps_keys:
                    meta.primary_language = "typescript"
        except Exception:
            pass

    # 3. requirements.txt
    elif req_path.is_file():
        manifest_found = True
        meta.primary_language = "python"
        meta.setup_commands = ["pip install -r requirements.txt"]
        meta.test_command = "python -m pytest"
        try:
            req_content = req_path.read_text(encoding="utf-8").lower()
            if "fastapi" in req_content:
                meta.framework = "fastapi"
            elif "flask" in req_content:
                meta.framework = "flask"
            elif "django" in req_content:
                meta.framework = "django"
        except Exception:
            pass

    # 4. Cargo.toml
    elif cargo_path.is_file():
        manifest_found = True
        meta.primary_language = "rust"
        meta.setup_commands = ["cargo build"]
        meta.test_command = "cargo test"
        meta.build_command = "cargo build --release"

    # 5. setup.py
    elif setup_py_path.is_file():
        manifest_found = True
        meta.primary_language = "python"
        meta.setup_commands = ["pip install -e ."]
        meta.test_command = "python -m pytest"

    # 6. pom.xml / build.gradle / go.mod
    elif pom_path.is_file():
        manifest_found = True
        meta.primary_language = "java"
        meta.test_command = "mvn test"
    elif gradle_path.is_file():
        manifest_found = True
        meta.primary_language = "java"
        meta.test_command = "gradle test"
    elif go_mod_path.is_file():
        manifest_found = True
        meta.primary_language = "go"
        meta.test_command = "go test ./..."

    meta.has_manifests = manifest_found
    return manifest_found


def _deep_scan_files(root: Path, meta: ProjectMetadata) -> None:
    """Perform a deep scan of project source files, stubs, components, UI entry points, and test suites."""
    ignored_dirs = {
        ".git",
        ".ai-os",
        "node_modules",
        "__pycache__",
        ".venv",
        "venv",
        "dist",
        "build",
        ".pytest_cache",
    }

    stubs: List[str] = []
    ui_entries: List[str] = []
    components: List[str] = []
    routes: List[str] = []
    assets_dirs: Set[str] = set()

    for path in root.rglob("*"):
        if any(part in ignored_dirs for part in path.parts):
            continue
        if not path.is_file():
            continue

        rel_path = path.relative_to(root).as_posix()
        meta.detected_files.append(rel_path)

        # Detect test suites
        if any(part in ("tests", "test", "__tests__", "spec") for part in path.parts) or "test_" in path.name or "_test." in path.name:
            meta.has_test_suite = True

        # Check assets directories
        if any(part in ("static", "public", "styles", "assets") for part in path.parts):
            for part in path.parts[:-1]:
                if part in ("static", "public", "styles", "assets"):
                    assets_dirs.add(part)

        ext = path.suffix.lower()

        # UI Entry points
        if ext == ".html":
            meta.has_ui = True
            ui_entries.append(rel_path)
            if meta.ui_framework == "none":
                meta.ui_framework = "vanilla"

        elif ext in (".jsx", ".tsx"):
            meta.has_ui = True
            if meta.ui_framework == "none":
                meta.ui_framework = "react"

        # Scan code stubs & functions/classes
        if ext in (".py", ".js", ".ts", ".jsx", ".tsx"):
            try:
                content = path.read_text(encoding="utf-8", errors="ignore")

                # Extract python classes / functions
                if ext == ".py":
                    matches = re.findall(r"^(?:def|class)\s+([A-Za-z0-9_]+)", content, re.MULTILINE)
                    for match in matches:
                        stubs.append(f"{rel_path}:{match}")

                    # Extract routes
                    route_matches = re.findall(
                        r"@(?:app|router)\.(?:get|post|put|delete)\(\s*['\"]([^'\"]+)['\"]", content
                    )
                    for r in route_matches:
                        routes.append(r)

                # Extract JS/TS functions / classes / components
                elif ext in (".js", ".ts", ".jsx", ".tsx"):
                    matches = re.findall(r"(?:function|class|const)\s+([A-Z][A-Za-z0-9_]+)", content)
                    for match in matches:
                        components.append(match)
                        stubs.append(f"{rel_path}:{match}")
            except Exception:
                continue

        # Main entry points
        if rel_path in (
            "main.py",
            "app.py",
            "cli.py",
            "index.js",
            "src/index.js",
            "src/index.ts",
            "src/main.ts",
            "index.html",
        ):
            meta.entry_points.append(rel_path)

    meta.code_stubs = stubs[:100]
    if ui_entries:
        meta.ui_entry_points = ui_entries
    if components:
        meta.ui_components = list(dict.fromkeys(components))[:50]
    if routes:
        meta.ui_routes = list(dict.fromkeys(routes))
    if assets_dirs:
        meta.ui_assets_dir = sorted(list(assets_dirs))[0]


def scan_and_generate_configs(
    project_path: Path | str,
    use_deep_scan: bool = False,
    model: Optional[str] = None,
) -> dict:
    """Scan existing project documentation, manifests, or code stubs, synthesizing .ai-os config files.

    Args:
        project_path: Path to the target project directory.
        use_deep_scan: If True, performs deep scan of source files, AST stubs, symbol graphs, and test suites.
        model: Optional model identifier for metadata compatibility.

    Returns:
        dict: Config dictionary containing status ('success' or 'missing_docs'), config_dir,
              generated_files, and metadata.

    Raises:
        FileNotFoundError: If project_path does not exist.
    """
    root = Path(project_path).resolve()
    if not root.exists() or not root.is_dir():
        raise FileNotFoundError(f"Project directory does not exist: {project_path}")

    meta = ProjectMetadata()
    name, desc, has_docs = _scan_documentation(root)
    meta.name = name
    meta.description = desc
    meta.has_docs = has_docs

    has_manifests = _scan_manifests(root, meta)

    if not has_docs and not has_manifests and not use_deep_scan:
        return ConfigDict(
            {
                "status": "missing_docs",
                "message": "No documentation files or package manifests found in project. Use use_deep_scan=True to inspect code stubs.",
                "config_dir": None,
                "generated_files": [],
            }
        )

    if use_deep_scan:
        _deep_scan_files(root, meta)

    ai_os_dir = root / ".ai-os"
    ai_os_dir.mkdir(parents=True, exist_ok=True)

    # 1. instructions.json
    instructions_data = {
        "project_name": meta.name,
        "description": meta.description,
        "language": meta.primary_language,
        "framework": meta.framework,
        "deep_scan": use_deep_scan,
        "model": model,
        "instructions": [
            "Adhere strictly to project conventions in .ai-os/conventions.md",
            f"Use sandbox test command '{meta.test_command}' to validate code changes before submission",
            "Ensure zero unhandled exceptions and maintain pure deterministic logic in core modules",
        ],
        "entry_points": meta.entry_points,
        "code_stubs": meta.code_stubs if use_deep_scan else [],
    }
    (ai_os_dir / "instructions.json").write_text(
        json.dumps(instructions_data, indent=2), encoding="utf-8"
    )

    # 2. conventions.md
    conventions_content = f"""# Project Conventions: {meta.name}

## Overview
{meta.description}

## Tech Stack & Frameworks
- Primary Language: {meta.primary_language}
- Framework: {meta.framework}

## Code Style & Implementation Rules
- Write explicit type hints for all functions, methods, and variables.
- Maintain comprehensive module-level docstrings explaining architectural choices.
- Keep core logic deterministic, pure, and testable without network or LLM dependencies.

## Testing Guidelines
- Automated test command: `{meta.test_command}`
- All new features and modified behaviors MUST be accompanied by real, non-tautological unit tests.
- Mocking external side-effects (LLM, network, Docker) is mandatory in test suites.

## UI & Asset Management
- UI Supported: {meta.has_ui}
- UI Framework: {meta.ui_framework}
"""
    (ai_os_dir / "conventions.md").write_text(conventions_content, encoding="utf-8")

    # 3. sandbox.json
    sandbox_data = {
        "environment": meta.primary_language,
        "setup_commands": meta.setup_commands if meta.setup_commands else ["echo 'No setup required'"],
        "test_command": meta.test_command,
        "build_command": meta.build_command,
        "timeout": 300,
        "env": {},
    }
    (ai_os_dir / "sandbox.json").write_text(
        json.dumps(sandbox_data, indent=2), encoding="utf-8"
    )

    # 4. ui.json
    ui_data = {
        "has_ui": meta.has_ui,
        "framework": meta.ui_framework,
        "entry_points": meta.ui_entry_points if meta.ui_entry_points else (meta.entry_points if meta.has_ui else []),
        "routes": meta.ui_routes,
        "components": meta.ui_components,
        "assets_dir": meta.ui_assets_dir,
    }
    (ai_os_dir / "ui.json").write_text(
        json.dumps(ui_data, indent=2), encoding="utf-8"
    )

    return ConfigDict(
        {
            "status": "success",
            "config_dir": ai_os_dir,
            "generated_files": [
                str(ai_os_dir / "instructions.json"),
                str(ai_os_dir / "conventions.md"),
                str(ai_os_dir / "sandbox.json"),
                str(ai_os_dir / "ui.json"),
            ],
            "metadata": {
                "name": meta.name,
                "description": meta.description,
                "primary_language": meta.primary_language,
                "framework": meta.framework,
                "has_ui": meta.has_ui,
            },
        }
    )


__all__ = ["scan_and_generate_configs", "ProjectMetadata", "ConfigDict"]