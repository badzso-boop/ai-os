"""Per-file Tree-sitter parsing and deterministic symbol extraction (doc 03 / doc 08).

Zero AI tokens: everything here is Tree-sitter AST traversal and Query matching.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from tree_sitter import Language, Node, Parser, Query, QueryCursor, Tree

from ai_os.analyzer.languages import LANGUAGES, detect_language

_CLASS_DEF_TYPES = {"class_definition", "class_declaration", "interface_declaration"}
_FUNCTION_DEF_TYPES = {
    "function_definition",
    "function_declaration",
    "method_definition",
    "method_declaration",
    "constructor_declaration",
}


@dataclass
class ParsedFile:
    path: Path
    relpath: str
    language: str
    source: bytes
    tree: Tree


@dataclass
class Symbol:
    kind: str  # "class" | "interface" | "function" | "method" | "type"
    name: str
    fqn: str
    language: str
    relpath: str
    start_line: int
    end_line: int
    start_byte: int
    end_byte: int
    body_start_byte: int | None
    params: list[str] = field(default_factory=list)
    return_type: str | None = None
    docstring: str | None = None
    parent_class: str | None = None
    is_method: bool = False
    extends: list[tuple[str, str]] = field(default_factory=list)  # (base_name, "extends"|"implements")


def node_text(node: Node | None) -> str | None:
    if node is None:
        return None
    return node.text.decode("utf-8", errors="replace")


_COMMENT_NODE_TYPES = {"comment", "block_comment", "line_comment"}


def _leading_doc_comment(def_node: Node) -> str | None:
    """Best-effort Javadoc/JSDoc lookup: a `/** ... */` comment immediately preceding the node."""
    prev = def_node.prev_sibling
    if prev is not None and prev.type in _COMMENT_NODE_TYPES:
        text = node_text(prev)
        if text and text.startswith("/**"):
            return text
    return None


def _python_docstring(body_node: Node | None) -> str | None:
    if body_node is None or body_node.named_child_count == 0:
        return None
    first = body_node.named_children[0]
    if first.type == "expression_statement" and first.named_child_count:
        expr = first.named_children[0]
        if expr.type == "string":
            return node_text(expr)
    return None


def _param_names(params_node: Node | None) -> list[str]:
    if params_node is None:
        return []
    names: list[str] = []
    for child in params_node.named_children:
        name_field = child.child_by_field_name("name")
        names.append(node_text(name_field) if name_field is not None else (node_text(child) or ""))
    return names


def _find_all(node: Node, types: set[str]) -> list[Node]:
    out: list[Node] = []
    stack = [node]
    while stack:
        current = stack.pop()
        if current.type in types:
            out.append(current)
        stack.extend(current.children)
    return out


def _extends_names(def_node: Node) -> list[tuple[str, str]]:
    """Best-effort base class/interface names for a class/interface def node, tagged by
    relation kind. Deterministic name extraction only — resolution to FQNs happens later
    in call_graph_builder, the same way CALLS name-matching does."""
    names: list[tuple[str, str]] = []

    superclasses = def_node.child_by_field_name("superclasses")  # Python
    if superclasses is not None:
        for child in superclasses.named_children:
            text = node_text(child)
            if text:
                names.append((text, "extends"))

    superclass = def_node.child_by_field_name("superclass")  # Java extends: wraps type_identifier
    if superclass is not None:
        type_id = next((c for c in superclass.named_children if c.type == "type_identifier"), None)
        text = node_text(type_id) if type_id is not None else node_text(superclass)
        if text:
            names.append((text, "extends"))

    interfaces = def_node.child_by_field_name("interfaces")  # Java implements
    if interfaces is not None:
        for type_node in _find_all(interfaces, {"type_identifier"}):
            text = node_text(type_node)
            if text:
                names.append((text, "implements"))

    for heritage in (c for c in def_node.children if c.type == "class_heritage"):  # JS/TS
        for named in heritage.named_children:
            if named.type in ("identifier", "type_identifier"):
                text = node_text(named)
                if text:
                    names.append((text, "extends"))

    return names


def _nearest_enclosing_kind(node: Node) -> tuple[str, Node] | None:
    """Walk up the AST from `node` and return the first enclosing class-like or function-like
    definition, whichever is closer. Used to decide is_method vs. plain function/nested closure."""
    current = node.parent
    while current is not None:
        if current.type in _CLASS_DEF_TYPES:
            return "class", current
        if current.type in _FUNCTION_DEF_TYPES:
            return "function", current
        current = current.parent
    return None


class TreeSitterEngine:
    """Parses source files and extracts symbol information via Tree-sitter queries."""

    def __init__(self) -> None:
        self._languages: dict[str, Language] = {}
        self._parsers: dict[str, Parser] = {}
        self._queries: dict[str, Query | None] = {}

    def _language(self, name: str) -> Language:
        if name not in self._languages:
            self._languages[name] = Language(LANGUAGES[name].language_factory())
        return self._languages[name]

    def _parser(self, name: str) -> Parser:
        if name not in self._parsers:
            self._parsers[name] = Parser(self._language(name))
        return self._parsers[name]

    def _query(self, name: str) -> Query | None:
        if name not in self._queries:
            source = LANGUAGES[name].load_query_source()
            self._queries[name] = Query(self._language(name), source) if source else None
        return self._queries[name]

    def parse_file(self, path: Path, root: Path) -> ParsedFile | None:
        language = detect_language(path)
        if language is None:
            return None
        source = path.read_bytes()
        tree = self._parser(language).parse(source)
        relpath = path.resolve().relative_to(root.resolve()).as_posix()
        return ParsedFile(path=path, relpath=relpath, language=language, source=source, tree=tree)

    def extract_symbols(self, parsed: ParsedFile) -> list[Symbol]:
        query = self._query(parsed.language)
        if query is None:
            return []
        if parsed.language == "sql":
            return self._extract_sql_symbols(parsed, query)
        return self._extract_code_symbols(parsed, query)

    def _extract_code_symbols(self, parsed: ParsedFile, query: Query) -> list[Symbol]:
        cursor = QueryCursor(query)
        symbols: list[Symbol] = []
        for _, captures in cursor.matches(parsed.tree.root_node):
            if "class.def" in captures:
                symbols.append(self._build_class_symbol(parsed, captures))
            elif "function.def" in captures:
                symbols.append(self._build_function_symbol(parsed, captures))
        return symbols

    def _build_class_symbol(self, parsed: ParsedFile, captures: dict[str, list[Node]]) -> Symbol:
        def_node = captures["class.def"][0]
        name = node_text(captures["class.name"][0]) or "<anonymous>"
        kind = "interface" if def_node.type == "interface_declaration" else "class"
        body_node = def_node.child_by_field_name("body")
        docstring = (
            _python_docstring(body_node)
            if parsed.language == "python"
            else _leading_doc_comment(def_node)
        )
        return Symbol(
            kind=kind,
            name=name,
            fqn=f"{parsed.relpath}::{name}",
            language=parsed.language,
            relpath=parsed.relpath,
            start_line=def_node.start_point[0] + 1,
            end_line=def_node.end_point[0] + 1,
            start_byte=def_node.start_byte,
            end_byte=def_node.end_byte,
            body_start_byte=body_node.start_byte if body_node is not None else None,
            docstring=docstring,
            extends=_extends_names(def_node),
        )

    def _build_function_symbol(self, parsed: ParsedFile, captures: dict[str, list[Node]]) -> Symbol:
        def_node = captures["function.def"][0]
        name = node_text(captures["function.name"][0]) or "<anonymous>"

        enclosing = _nearest_enclosing_kind(def_node)
        is_method = enclosing is not None and enclosing[0] == "class"
        parent_class = node_text(enclosing[1].child_by_field_name("name")) if is_method else None

        body_node = def_node.child_by_field_name("body")
        params_node = def_node.child_by_field_name("parameters")
        return_type_node = def_node.child_by_field_name("type")
        docstring = (
            _python_docstring(body_node)
            if parsed.language == "python"
            else _leading_doc_comment(def_node)
        )

        fqn_name = f"{parent_class}.{name}" if parent_class else name
        return Symbol(
            kind="method" if is_method else "function",
            name=name,
            fqn=f"{parsed.relpath}::{fqn_name}",
            language=parsed.language,
            relpath=parsed.relpath,
            start_line=def_node.start_point[0] + 1,
            end_line=def_node.end_point[0] + 1,
            start_byte=def_node.start_byte,
            end_byte=def_node.end_byte,
            body_start_byte=body_node.start_byte if body_node is not None else None,
            params=_param_names(params_node),
            return_type=node_text(return_type_node),
            docstring=docstring,
            parent_class=parent_class,
            is_method=is_method,
        )

    def extract_call_sites(self, parsed: ParsedFile) -> list[tuple[Node, str]]:
        """Returns (call_node, unqualified_callee_name) pairs for name-based CALLS resolution."""
        query = self._query(parsed.language)
        if query is None or parsed.language == "sql":
            return []
        cursor = QueryCursor(query)
        sites: list[tuple[Node, str]] = []
        for _, captures in cursor.matches(parsed.tree.root_node):
            if "call.site" in captures:
                site_node = captures["call.site"][0]
                name = node_text(captures["call.name"][0])
                if name:
                    sites.append((site_node, name))
        return sites

    def _extract_sql_symbols(self, parsed: ParsedFile, query: Query) -> list[Symbol]:
        cursor = QueryCursor(query)
        symbols: list[Symbol] = []
        for _, captures in cursor.matches(parsed.tree.root_node):
            def_node = captures["type.def"][0]
            name = node_text(captures["type.name"][0]) or "<anonymous>"
            symbols.append(
                Symbol(
                    kind="type",
                    name=name,
                    fqn=f"{parsed.relpath}::{name}",
                    language=parsed.language,
                    relpath=parsed.relpath,
                    start_line=def_node.start_point[0] + 1,
                    end_line=def_node.end_point[0] + 1,
                    start_byte=def_node.start_byte,
                    end_byte=def_node.end_byte,
                    body_start_byte=None,
                )
            )
        return symbols
