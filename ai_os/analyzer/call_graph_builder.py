"""Project-wide walk: builds IMPORTS and CALLS edges on top of per-file symbol extraction
(doc 03 / doc 08). Import resolution and call-site resolution are both deterministic,
name/path-based heuristics — no AI involved, per the project's "Compiler First" rule.
"""
from __future__ import annotations

import json
import os
import re
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from tree_sitter import Node

from ai_os.analyzer.languages import detect_language
from ai_os.analyzer.tree_sitter_engine import ParsedFile, Symbol, TreeSitterEngine, node_text

# Node.js builtin modules: bare specifiers that will never appear in package.json but
# are always legitimately external, not a resolution gap.
_NODE_BUILTINS = frozenset(
    {
        "assert", "buffer", "child_process", "cluster", "crypto", "dns", "events",
        "fs", "http", "https", "net", "os", "path", "querystring", "readline",
        "stream", "string_decoder", "timers", "tls", "tty", "url", "util", "vm",
        "zlib", "process", "punycode",
    }
)
_JAVA_STDLIB_PREFIXES = ("java.", "javax.", "jakarta.")
_PY_STDLIB = frozenset(getattr(sys, "stdlib_module_names", ()))

DEFAULT_EXCLUDED_DIRS = frozenset(
    {
        ".git",
        "node_modules",
        "target",
        "build",
        "dist",
        ".idea",
        "venv",
        ".venv",
        "__pycache__",
        ".mypy_cache",
        ".pytest_cache",
        ".ai-os",
        ".next",
        "out",
        "coverage",
    }
)

_JS_EXTENSIONS = (".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx")


@dataclass
class ImportEdge:
    source_relpath: str
    raw_specifier: str
    target_relpath: str | None
    resolved: bool
    # Only meaningful when resolved=False: True means the specifier was matched against a
    # declared dependency manifest (package.json) or a known stdlib module/prefix, i.e. it's
    # an expected external dependency, not a genuine resolution gap worth investigating.
    external: bool = False


@dataclass
class CallEdge:
    caller_fqn: str
    callee_fqns: list[str]
    ambiguous: bool
    line: int


@dataclass
class ExtendsEdge:
    source_fqn: str
    target_fqns: list[str]
    ambiguous: bool
    via: str  # "extends" | "implements"


@dataclass
class FileScanResult:
    parsed: ParsedFile
    symbols: list[Symbol]


@dataclass
class ProjectScanResult:
    root: Path
    files: list[FileScanResult] = field(default_factory=list)
    import_edges: list[ImportEdge] = field(default_factory=list)
    call_edges: list[CallEdge] = field(default_factory=list)
    extends_edges: list[ExtendsEdge] = field(default_factory=list)
    file_count_by_language: dict[str, int] = field(default_factory=dict)
    other_file_count: int = 0


def iter_project_files(root: Path, excluded_dirs: set[str]) -> Iterable[Path]:
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in excluded_dirs]
        for filename in filenames:
            yield Path(dirpath) / filename


def _find_all(node: Node, types: set[str]) -> list[Node]:
    out: list[Node] = []
    stack = [node]
    while stack:
        current = stack.pop()
        if current.type in types:
            out.append(current)
        stack.extend(current.children)
    return out


def _load_js_dependencies(root: Path) -> set[str]:
    """Union of dependency names declared in every package.json under root (excluding
    node_modules), so bare imports can be told apart from genuine resolution gaps."""
    deps: set[str] = set()
    for pkg_json in root.rglob("package.json"):
        if "node_modules" in pkg_json.parts:
            continue
        try:
            data = json.loads(pkg_json.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        for key in ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies"):
            deps.update(data.get(key, {}) or {})
    return deps


def _js_package_name(spec: str) -> str:
    parts = spec.split("/")
    if spec.startswith("@") and len(parts) >= 2:
        return "/".join(parts[:2])
    return parts[0]


_PY_DEP_NAME_SPLIT = re.compile(r"[<>=!~\[; ]")


def _normalize_py_name(name: str) -> str:
    return name.strip().lower().replace("-", "_")


def _load_python_dependencies(root: Path) -> set[str]:
    """Union of dependency names declared in every requirements*.txt / pyproject.toml under
    root, so bare Python imports can be told apart from genuine resolution gaps."""
    deps: set[str] = set()

    for req_file in root.rglob("requirements*.txt"):
        if DEFAULT_EXCLUDED_DIRS & set(req_file.parts):
            continue
        for line in req_file.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.split("#", 1)[0].strip()
            if not line or line.startswith("-"):
                continue
            name = _PY_DEP_NAME_SPLIT.split(line, 1)[0].strip()
            if name:
                deps.add(_normalize_py_name(name))

    for pyproject in root.rglob("pyproject.toml"):
        if DEFAULT_EXCLUDED_DIRS & set(pyproject.parts):
            continue
        try:
            data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError):
            continue
        for dep in data.get("project", {}).get("dependencies", []):
            name = _PY_DEP_NAME_SPLIT.split(dep, 1)[0].strip()
            if name:
                deps.add(_normalize_py_name(name))
        poetry_deps = data.get("tool", {}).get("poetry", {}).get("dependencies", {})
        deps.update(_normalize_py_name(name) for name in poetry_deps)

    return deps


def _relpath_of(path: Path, root: Path) -> str | None:
    try:
        return path.resolve().relative_to(root).as_posix()
    except ValueError:
        return None


def _enclosing_symbol(symbols: list[Symbol], byte_offset: int) -> Symbol | None:
    candidates = [
        s
        for s in symbols
        if s.kind in ("function", "method") and s.start_byte <= byte_offset <= s.end_byte
    ]
    if not candidates:
        return None
    return min(candidates, key=lambda s: s.end_byte - s.start_byte)


class CallGraphBuilder:
    """Walks a project, extracts symbols per file, and resolves IMPORTS/CALLS edges."""

    def __init__(self, engine: TreeSitterEngine | None = None) -> None:
        self.engine = engine or TreeSitterEngine()

    def scan(
        self,
        root: Path,
        languages: set[str] | None = None,
        extra_excluded_dirs: Iterable[str] = (),
    ) -> ProjectScanResult:
        root = root.resolve()
        excluded = set(DEFAULT_EXCLUDED_DIRS) | set(extra_excluded_dirs)

        files: list[FileScanResult] = []
        lang_counts: dict[str, int] = {}
        other_count = 0

        for path in iter_project_files(root, excluded):
            lang = detect_language(path)
            if lang is None:
                other_count += 1
                continue
            if languages is not None and lang not in languages:
                continue
            parsed = self.engine.parse_file(path, root)
            if parsed is None:
                continue
            symbols = self.engine.extract_symbols(parsed)
            files.append(FileScanResult(parsed=parsed, symbols=symbols))
            lang_counts[lang] = lang_counts.get(lang, 0) + 1

        all_relpaths = {fr.parsed.relpath for fr in files}
        name_index: dict[str, list[str]] = {}
        for fr in files:
            for sym in fr.symbols:
                name_index.setdefault(sym.name, []).append(sym.fqn)
        java_package_index = self._build_java_package_index(files)
        type_name_index: dict[str, list[str]] = {}
        for fr in files:
            for sym in fr.symbols:
                if sym.kind in ("class", "interface"):
                    type_name_index.setdefault(sym.name, []).append(sym.fqn)

        js_dependencies = _load_js_dependencies(root)
        python_dependencies = _load_python_dependencies(root)

        import_edges: list[ImportEdge] = []
        call_edges: list[CallEdge] = []
        extends_edges: list[ExtendsEdge] = []
        for fr in files:
            import_edges.extend(
                self._extract_imports(
                    fr.parsed,
                    root,
                    all_relpaths,
                    java_package_index,
                    js_dependencies,
                    python_dependencies,
                )
            )
            call_edges.extend(self._extract_calls(fr.parsed, fr.symbols, name_index))
            extends_edges.extend(self._extract_extends(fr.symbols, type_name_index))

        return ProjectScanResult(
            root=root,
            files=files,
            import_edges=import_edges,
            call_edges=call_edges,
            extends_edges=extends_edges,
            file_count_by_language=lang_counts,
            other_file_count=other_count,
        )

    # -- CALLS -----------------------------------------------------------------

    def _extract_calls(
        self, parsed: ParsedFile, symbols: list[Symbol], name_index: dict[str, list[str]]
    ) -> list[CallEdge]:
        edges: list[CallEdge] = []
        for call_node, callee_name in self.engine.extract_call_sites(parsed):
            caller = _enclosing_symbol(symbols, call_node.start_byte)
            if caller is None:
                continue
            candidates = name_index.get(callee_name, [])
            if not candidates:
                continue
            edges.append(
                CallEdge(
                    caller_fqn=caller.fqn,
                    callee_fqns=list(candidates),
                    ambiguous=len(candidates) > 1,
                    line=call_node.start_point[0] + 1,
                )
            )
        return edges

    def _extract_extends(
        self, symbols: list[Symbol], type_name_index: dict[str, list[str]]
    ) -> list[ExtendsEdge]:
        edges: list[ExtendsEdge] = []
        for sym in symbols:
            if sym.kind not in ("class", "interface"):
                continue
            for base_name, via in sym.extends:
                candidates = [fqn for fqn in type_name_index.get(base_name, []) if fqn != sym.fqn]
                if not candidates:
                    continue
                edges.append(
                    ExtendsEdge(
                        source_fqn=sym.fqn,
                        target_fqns=candidates,
                        ambiguous=len(candidates) > 1,
                        via=via,
                    )
                )
        return edges

    # -- IMPORTS -----------------------------------------------------------------

    def _extract_imports(
        self,
        parsed: ParsedFile,
        root: Path,
        all_relpaths: set[str],
        java_package_index: dict[str, str],
        js_dependencies: set[str],
        python_dependencies: set[str],
    ) -> list[ImportEdge]:
        if parsed.language == "python":
            return self._python_imports(parsed, root, all_relpaths, python_dependencies)
        if parsed.language in ("javascript", "typescript"):
            return self._js_imports(parsed, root, all_relpaths, js_dependencies)
        if parsed.language == "java":
            return self._java_imports(parsed, java_package_index)
        if parsed.language == "html":
            return self._html_imports(parsed, root, all_relpaths)
        if parsed.language == "css":
            return self._css_imports(parsed, root, all_relpaths)
        return []

    def _python_imports(
        self, parsed: ParsedFile, root: Path, all_relpaths: set[str], python_dependencies: set[str]
    ) -> list[ImportEdge]:
        edges: list[ImportEdge] = []
        for node in _find_all(parsed.tree.root_node, {"import_statement", "import_from_statement"}):
            raw = node_text(node) or ""
            target_relpath: str | None = None
            module: str | None = None
            if node.type == "import_statement":
                name_node = node.child_by_field_name("name")
                module = node_text(name_node)
                if module:
                    target_relpath = self._resolve_python_module(module, root, all_relpaths)
            else:  # import_from_statement
                module_node = node.child_by_field_name("module_name")
                if module_node is not None and module_node.type == "relative_import":
                    dotted = next(
                        (c for c in module_node.named_children if c.type == "dotted_name"), None
                    )
                    subpath = node_text(dotted).replace(".", "/") if dotted is not None else ""
                    base_dir = Path(parsed.path).parent
                    target_relpath = self._resolve_python_path(base_dir / subpath, root, all_relpaths)
                elif module_node is not None:
                    module = node_text(module_node)
                    if module:
                        target_relpath = self._resolve_python_module(module, root, all_relpaths)
            resolved = target_relpath is not None
            top_level = (module or "").split(".")[0]
            external = not resolved and (
                top_level in _PY_STDLIB or _normalize_py_name(top_level) in python_dependencies
            )
            edges.append(
                ImportEdge(
                    source_relpath=parsed.relpath,
                    raw_specifier=raw,
                    target_relpath=target_relpath,
                    resolved=resolved,
                    external=external,
                )
            )
        return edges

    @staticmethod
    def _resolve_python_module(module: str, root: Path, all_relpaths: set[str]) -> str | None:
        return CallGraphBuilder._resolve_python_path(
            root / module.replace(".", "/"), root, all_relpaths
        )

    @staticmethod
    def _resolve_python_path(base: Path, root: Path, all_relpaths: set[str]) -> str | None:
        for candidate in (base.with_suffix(".py"), base / "__init__.py"):
            rel = _relpath_of(candidate, root)
            if rel is not None and rel in all_relpaths:
                return rel
        return None

    def _js_imports(
        self, parsed: ParsedFile, root: Path, all_relpaths: set[str], js_dependencies: set[str]
    ) -> list[ImportEdge]:
        edges: list[ImportEdge] = []
        specifiers: list[str] = []

        for node in _find_all(parsed.tree.root_node, {"import_statement", "export_statement"}):
            source_node = node.child_by_field_name("source")
            if source_node is not None:
                text = node_text(source_node) or ""
                specifiers.append(text.strip("'\""))

        for call_node in _find_all(parsed.tree.root_node, {"call_expression"}):
            fn_node = call_node.child_by_field_name("function")
            if fn_node is not None and node_text(fn_node) == "require":
                args = call_node.child_by_field_name("arguments")
                if args is not None and args.named_child_count:
                    arg = args.named_children[0]
                    if arg.type == "string":
                        specifiers.append((node_text(arg) or "").strip("'\""))

        file_dir = Path(parsed.path).parent
        for spec in specifiers:
            target_relpath: str | None = None
            is_bare = not (spec.startswith(".") or spec.startswith("/"))
            # Vite-style query/hash suffixes (?raw, ?url, ?worker, #foo) are load-time hints,
            # not part of the filesystem path — strip them before resolving.
            resolution_spec = spec.split("?", 1)[0].split("#", 1)[0]
            base: Path | None = None
            if not is_bare:
                base = (
                    file_dir / resolution_spec
                    if not resolution_spec.startswith("/")
                    else root / resolution_spec.lstrip("/")
                )
                target_relpath = self._resolve_js_path(base, root, all_relpaths)
            resolved = target_relpath is not None
            external = False
            if not resolved:
                if is_bare:
                    package_name = _js_package_name(spec)
                    external = package_name in js_dependencies or package_name in _NODE_BUILTINS
                elif base is not None:
                    # Points at a real file that just isn't a recognized source language
                    # (markdown, images, JSON data, ...) - not a graph gap, just untracked.
                    external = base.is_file()
            edges.append(
                ImportEdge(
                    source_relpath=parsed.relpath,
                    raw_specifier=spec,
                    target_relpath=target_relpath,
                    resolved=resolved,
                    external=external,
                )
            )
        return edges

    @staticmethod
    def _resolve_js_path(base: Path, root: Path, all_relpaths: set[str]) -> str | None:
        candidates = [base.with_suffix(ext) for ext in _JS_EXTENSIONS]
        candidates.append(base)
        for ext in _JS_EXTENSIONS:
            candidates.append(base / f"index{ext}")
        for candidate in candidates:
            rel = _relpath_of(candidate, root)
            if rel is not None and rel in all_relpaths:
                return rel
        return None

    def _build_java_package_index(self, files: list[FileScanResult]) -> dict[str, str]:
        index: dict[str, str] = {}
        for fr in files:
            if fr.parsed.language != "java":
                continue
            package = self._java_package_name(fr.parsed)
            for sym in fr.symbols:
                if sym.kind in ("class", "interface"):
                    fqn_name = f"{package}.{sym.name}" if package else sym.name
                    index[fqn_name] = fr.parsed.relpath
        return index

    @staticmethod
    def _java_package_name(parsed: ParsedFile) -> str | None:
        matches = _find_all(parsed.tree.root_node, {"package_declaration"})
        if not matches:
            return None
        for child in matches[0].named_children:
            if child.type == "scoped_identifier" or child.type == "identifier":
                return node_text(child)
        return None

    def _java_imports(
        self, parsed: ParsedFile, java_package_index: dict[str, str]
    ) -> list[ImportEdge]:
        edges: list[ImportEdge] = []
        for node in _find_all(parsed.tree.root_node, {"import_declaration"}):
            raw = node_text(node) or ""
            qualified = None
            for child in node.named_children:
                if child.type in ("scoped_identifier", "identifier"):
                    qualified = node_text(child)
                    break
            target_relpath = java_package_index.get(qualified) if qualified else None
            resolved = target_relpath is not None
            # java_package_index already covers every internal class in the project, so a
            # miss here is never "we failed to find an internal file" — it's always a JDK
            # class or a third-party library import (Spring, Lombok, ...).
            edges.append(
                ImportEdge(
                    source_relpath=parsed.relpath,
                    raw_specifier=raw,
                    target_relpath=target_relpath,
                    resolved=resolved,
                    external=not resolved,
                )
            )
        return edges

    def _html_imports(
        self, parsed: ParsedFile, root: Path, all_relpaths: set[str]
    ) -> list[ImportEdge]:
        edges: list[ImportEdge] = []
        file_dir = Path(parsed.path).parent
        for tag_node in _find_all(parsed.tree.root_node, {"script_element", "element"}):
            start_tag = next((c for c in tag_node.children if c.type == "start_tag"), tag_node)
            tag_name_node = next((c for c in start_tag.children if c.type == "tag_name"), None)
            tag_name = (node_text(tag_name_node) or "").lower()
            attr_name = "src" if tag_name == "script" else "href" if tag_name == "link" else None
            if attr_name is None:
                continue
            value = self._html_attr_value(start_tag, attr_name)
            if not value or value.startswith(("http://", "https://", "//")):
                continue
            # A "/"-prefixed src/href is root-relative, but pathlib's `dir / "/x"` silently
            # discards `dir` and returns the filesystem-absolute "/x" — so try it relative to
            # the HTML file's own directory first (the common single-app/Vite-root case),
            # then fall back to the scan root (e.g. a `public/` mounted at the server root).
            bases = [file_dir] if not value.startswith("/") else [file_dir, root]
            rel_value = value.lstrip("/") if value.startswith("/") else value

            target_relpath: str | None = None
            for base_dir in bases:
                target_relpath = self._resolve_js_path(base_dir / rel_value, root, all_relpaths)
                if target_relpath is None:
                    rel = _relpath_of(base_dir / rel_value, root)
                    target_relpath = rel if rel in all_relpaths else None
                if target_relpath is not None:
                    break
            edges.append(
                ImportEdge(
                    source_relpath=parsed.relpath,
                    raw_specifier=value,
                    target_relpath=target_relpath,
                    resolved=target_relpath is not None,
                )
            )
        return edges

    @staticmethod
    def _html_attr_value(start_tag: Node, attr_name: str) -> str | None:
        for attr in start_tag.children:
            if attr.type != "attribute":
                continue
            name_node = next((c for c in attr.children if c.type == "attribute_name"), None)
            if name_node is None or (node_text(name_node) or "").lower() != attr_name:
                continue
            value_node = next(
                (c for c in attr.children if c.type == "quoted_attribute_value"), None
            )
            if value_node is None:
                continue
            inner = next((c for c in value_node.children if c.type == "attribute_value"), None)
            return node_text(inner)
        return None

    def _css_imports(
        self, parsed: ParsedFile, root: Path, all_relpaths: set[str]
    ) -> list[ImportEdge]:
        edges: list[ImportEdge] = []
        file_dir = Path(parsed.path).parent
        for node in _find_all(parsed.tree.root_node, {"import_statement"}):
            string_content = _find_all(node, {"string_content"})
            if not string_content:
                continue
            spec = node_text(string_content[0]) or ""
            target_relpath = None
            rel = _relpath_of(file_dir / spec, root)
            if rel is not None and rel in all_relpaths:
                target_relpath = rel
            edges.append(
                ImportEdge(
                    source_relpath=parsed.relpath,
                    raw_specifier=spec,
                    target_relpath=target_relpath,
                    resolved=target_relpath is not None,
                )
            )
        return edges
