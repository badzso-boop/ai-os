"""Static UI graph builder for AI OS interface analysis.

This module constructs a queryable NetworkX-backed UIGraph from static project files
(HTML, CSS, JavaScript/TypeScript) using Tree-sitter AST parsing. It operates without
any LLM, network, or browser dependencies.

Key components and workflow:
1. CallGraph & Symbol Discovery: Reuses CallGraphBuilder and TreeSitterEngine to scan
   the project root for JS/TS symbols and FQNs.
2. HTML Element Parsing: Walks HTML ASTs to identify interactive elements (button,
   a[href], input, select, textarea, form, role='button', onclick, tabindex,
   contenteditable), builds parent-child element hierarchies, constructs selector sets,
   creates SelectorNodes, and adds MATCHES edges (Selector -> Element).
3. Inline Handler Extraction: Parses HTML element attributes for inline handlers
   (onclick, onchange, etc.), creates HandlerNodes, links them to elements via HANDLED_BY
   edges, and resolves handler names to JS symbol FQNs creating IMPLEMENTED_BY edges.
4. CSS Style Rule Extraction & Matching: Parses CSS ASTs, filters declarations for
   behavior-relevant properties (display, visibility, opacity, pointer-events, position,
   z-index, cursor), creates StyleRuleNodes, matches rules to elements by id, class, or tag,
   and adds STYLED_BY edges (Element -> StyleRule).
5. JS Event Handler Parsing: Scans JS/TS ASTs for addEventListener and onX assignments,
   extracts event type and handler function names, resolves FQNs, matches target elements,
   and wires HandlerNodes with HANDLED_BY and IMPLEMENTED_BY edges.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from tree_sitter import Node

from ai_os.analyzer.call_graph_builder import CallGraphBuilder, DEFAULT_EXCLUDED_DIRS
from ai_os.analyzer.languages import detect_language
from ai_os.analyzer.tree_sitter_engine import ParsedFile, Symbol, TreeSitterEngine, node_text
from ai_os.ui.ui_graph import (
    HANDLED_BY,
    IMPLEMENTED_BY,
    MATCHES,
    STYLED_BY,
    ElementNode,
    HandlerNode,
    SelectorNode,
    StyleRuleNode,
    UIGraph,
    UIGraphNode,
)

# CSS properties that impact UI behavior, layout, or interactivity
BEHAVIOR_PROPERTIES = {
    "display",
    "visibility",
    "opacity",
    "pointer-events",
    "position",
    "z-index",
    "cursor",
}

# HTML tags that are interactive by default
INTERACTIVE_TAGS = {"button", "input", "select", "textarea", "form"}


def build_static_ui_graph(project_root: str | Path) -> UIGraph:
    """Build a static UIGraph for the given project directory.

    Parses HTML, CSS, and JS/TS files in project_root using TreeSitterEngine and CallGraphBuilder.
    Constructs ElementNodes, SelectorNodes, StyleRuleNodes, HandlerNodes, and their directed edges.
    """
    root = Path(project_root).resolve()
    graph = UIGraph()

    engine = TreeSitterEngine()
    cg_builder = CallGraphBuilder(engine)
    scan_result = cg_builder.scan(root)

    # Index JS/TS symbols for FQN resolution
    js_symbol_index: Dict[str, list[Symbol]] = {}
    for fr in scan_result.files:
        for sym in fr.symbols:
            js_symbol_index.setdefault(sym.name, []).append(sym)
            js_symbol_index.setdefault(sym.fqn, []).append(sym)

    # Separate parsed files by language
    html_files: list[ParsedFile] = []
    css_files: list[ParsedFile] = []
    js_files: list[ParsedFile] = []

    for fr in scan_result.files:
        lang = fr.parsed.language
        if lang == "html":
            html_files.append(fr.parsed)
        elif lang == "css":
            css_files.append(fr.parsed)
        elif lang in ("javascript", "typescript"):
            js_files.append(fr.parsed)

    # 1. Extract HTML elements, selectors, parent-child links, and inline handlers
    for html_parsed in html_files:
        _process_html_file(html_parsed, graph, js_symbol_index)

    # 2. Extract CSS style rules and match them to HTML elements
    for css_parsed in css_files:
        _process_css_file(css_parsed, graph)

    # 3. Extract JS addEventListener and onX handlers
    for js_parsed in js_files:
        _process_js_file(js_parsed, graph, js_symbol_index)

    return graph


# -- HTML Parsing -------------------------------------------------------------


def _process_html_file(
    parsed: ParsedFile,
    graph: UIGraph,
    js_symbol_index: Dict[str, list[Symbol]],
) -> None:
    """Traverse HTML AST to extract elements, selector nodes, and inline handlers."""
    root_node = parsed.tree.root_node

    def _walk(node: Node, parent_elem_id: Optional[str] = None) -> None:
        nonlocal graph
        curr_parent_id = parent_elem_id

        if node.type in ("element", "self_closing_tag"):
            tag_name = _get_html_tag_name(node)
            if tag_name not in ("script", "style", "head", "html", "meta", "link", "title"):
                attrs = _extract_html_attributes(node)
                text_content = _extract_html_text(node)
                is_interactive = _check_is_interactive(tag_name, attrs)

                selectors = _build_element_selectors(tag_name, attrs, text_content)

                # Determine unique node ID
                attr_id = attrs.get("id")
                if attr_id and not graph.has_node(attr_id):
                    elem_id = attr_id
                else:
                    elem_id = f"elem_{parsed.relpath}_{node.start_point[0] + 1}_{node.start_point[1]}"

                line = node.start_point[0] + 1
                elem_node = ElementNode(
                    id=elem_id,
                    tag_name=tag_name,
                    selectors=selectors,
                    file=parsed.relpath,
                    line=line,
                    attributes=attrs,
                    text_content=text_content,
                    is_interactive=is_interactive,
                    parent_id=curr_parent_id,
                )
                graph.add_element_node(elem_node, parent_id=curr_parent_id)

                # Add SelectorNodes and MATCHES edges (Selector -> Element)
                for sel in selectors:
                    if not graph.has_node(sel):
                        graph.add_selector_node(sel, selector=sel)
                    graph.add_edge(sel, elem_id, kind=MATCHES)

                # Inline event handlers (onclick, onchange, etc.)
                for attr_name, attr_val in attrs.items():
                    if attr_name.startswith("on") and len(attr_name) > 2:
                        event_type = attr_name[2:].lower()
                        handler_name = _extract_handler_function_name(attr_val)
                        js_fqn = _resolve_symbol_fqn(handler_name, parsed.relpath, js_symbol_index)

                        h_id = f"handler_{elem_id}_{event_type}"
                        h_node = HandlerNode(
                            id=h_id,
                            event_type=event_type,
                            handler_name=handler_name,
                            js_symbol_fqn=js_fqn,
                            file=parsed.relpath,
                            line=line,
                        )
                        graph.add_handler_node(h_node)
                        graph.add_edge(elem_id, h_id, kind=HANDLED_BY)

                        if js_fqn:
                            if not graph.has_node(js_fqn):
                                graph.add_node(
                                    UIGraphNode(id=js_fqn, node_type="js_symbol", label=handler_name)
                                )
                            graph.add_edge(h_id, js_fqn, kind=IMPLEMENTED_BY)

                curr_parent_id = elem_id

        for child in node.children:
            _walk(child, curr_parent_id)

    _walk(root_node, None)


def _get_html_tag_name(node: Node) -> str:
    """Extract tag name from an HTML element or self_closing_tag AST node."""
    if node.type == "self_closing_tag":
        tag_node = node.child_by_field_name("tag_name")
        if not tag_node:
            for child in node.children:
                if child.type == "tag_name":
                    tag_node = child
                    break
        return node_text(tag_node).lower() if tag_node else "div"

    # Standard element -> check start_tag
    start_tag = None
    for child in node.children:
        if child.type == "start_tag":
            start_tag = child
            break

    if start_tag:
        tag_node = start_tag.child_by_field_name("tag_name")
        if not tag_node:
            for child in start_tag.children:
                if child.type == "tag_name":
                    tag_node = child
                    break
        return node_text(tag_node).lower() if tag_node else "div"

    return "div"


def _extract_html_attributes(node: Node) -> Dict[str, str]:
    """Extract HTML attributes as a dict mapping attribute name to value."""
    attrs: Dict[str, str] = {}
    target_node = node
    if node.type == "element":
        for child in node.children:
            if child.type == "start_tag":
                target_node = child
                break

    for child in target_node.children:
        if child.type == "attribute":
            name_node = child.child_by_field_name("name")
            if not name_node:
                for c in child.children:
                    if c.type == "attribute_name":
                        name_node = c
                        break
            if not name_node:
                continue
            name = node_text(name_node).lower()

            val_node = child.child_by_field_name("value")
            if not val_node:
                for c in child.children:
                    if c.type in ("attribute_value", "quoted_attribute_value"):
                        val_node = c
                        break

            if val_node:
                val_text = node_text(val_node) or ""
                # Strip wrapping quotes if quoted
                if len(val_text) >= 2 and val_text[0] in ('"', "'") and val_text[-1] == val_text[0]:
                    val_text = val_text[1:-1]
                attrs[name] = val_text
            else:
                attrs[name] = "true"

    return attrs


def _extract_html_text(node: Node) -> str:
    """Extract direct text content of an HTML element."""
    text_parts: list[str] = []
    for child in node.children:
        if child.type == "text":
            t = node_text(child)
            if t:
                text_parts.append(t.strip())
    return " ".join(p for p in text_parts if p)


def _check_is_interactive(tag_name: str, attrs: Dict[str, str]) -> bool:
    """Determine whether an element is interactive."""
    if tag_name in INTERACTIVE_TAGS:
        return True
    if tag_name == "a" and "href" in attrs:
        return True
    if attrs.get("role") == "button":
        return True
    if any(name.startswith("on") for name in attrs):
        return True
    if "tabindex" in attrs:
        return True
    if "contenteditable" in attrs:
        return True
    return False


def _build_element_selectors(tag_name: str, attrs: Dict[str, str], text_content: str) -> list[str]:
    """Build selector set for an element (tag, #id, .class, attributes, text)."""
    selectors: list[str] = [tag_name]

    if "id" in attrs:
        selectors.append(f"#{attrs['id']}")

    if "class" in attrs:
        classes = [c for c in attrs["class"].split() if c]
        for cls in classes:
            selectors.append(f".{cls}")
        if len(classes) > 1:
            selectors.append(f".{'.'.join(classes)}")

    if "data-testid" in attrs:
        selectors.append(f"[data-testid='{attrs['data-testid']}']")

    if "aria-label" in attrs:
        selectors.append(f"[aria-label='{attrs['aria-label']}']")

    if "name" in attrs:
        selectors.append(f"[name='{attrs['name']}']")

    if "type" in attrs:
        selectors.append(f"[type='{attrs['type']}']")

    if "role" in attrs:
        selectors.append(f"[role='{attrs['role']}']")

    if text_content:
        selectors.append(text_content)

    return selectors


def _extract_handler_function_name(expr: str) -> str:
    """Extract function identifier from an inline JS expression like 'handleClick(event)'."""
    expr = expr.strip()
    if not expr:
        return "noop"
    match = re.search(r"([a-zA-Z_$][a-zA-Z0-9_$.]*)\s*\(", expr)
    if match:
        fn_part = match.group(1)
        return fn_part.split(".")[-1]
    return expr


def _resolve_symbol_fqn(
    name: str,
    current_relpath: str,
    js_symbol_index: Dict[str, list[Symbol]],
) -> Optional[str]:
    """Resolve a handler function name to its fully qualified name (FQN)."""
    if not name or name in ("noop", "javascript:void(0)"):
        return None

    if name in js_symbol_index:
        symbols = js_symbol_index[name]
        # Prefer symbol in current file or same directory
        for sym in symbols:
            if sym.relpath == current_relpath:
                return sym.fqn
        return symbols[0].fqn

    return None


# -- CSS Parsing --------------------------------------------------------------


def _process_css_file(parsed: ParsedFile, graph: UIGraph) -> None:
    """Parse CSS rules and match behavior-relevant declarations to graph elements."""
    root_node = parsed.tree.root_node

    def _walk_css(node: Node) -> None:
        if node.type == "rule_set":
            selectors_node = node.child_by_field_name("selectors")
            if not selectors_node:
                for c in node.children:
                    if c.type in ("selectors", "selector"):
                        selectors_node = c
                        break
            selector_text = node_text(selectors_node).strip() if selectors_node else ""

            block_node = node.child_by_field_name("block")
            if not block_node:
                for c in node.children:
                    if c.type == "block":
                        block_node = c
                        break

            declarations: Dict[str, str] = {}
            if block_node:
                for child in block_node.children:
                    if child.type == "declaration":
                        prop_name_node = child.child_by_field_name("property_name")
                        if not prop_name_node:
                            for c in child.children:
                                if c.type == "property_name":
                                    prop_name_node = c
                                    break
                        if not prop_name_node:
                            continue

                        prop_name = node_text(prop_name_node).lower()
                        if prop_name in BEHAVIOR_PROPERTIES:
                            # Extract property value
                            val_parts = []
                            for c in child.children:
                                if c.type not in ("property_name", ":", ";"):
                                    val_parts.append(node_text(c))
                            val_text = " ".join(p for p in val_parts if p).strip()
                            if val_text.endswith(";"):
                                val_text = val_text[:-1].strip()
                            declarations[prop_name] = val_text

            if selector_text and declarations:
                line = node.start_point[0] + 1
                rule_id = f"style_{parsed.relpath}_{line}"
                style_node = StyleRuleNode(
                    id=rule_id,
                    selector=selector_text,
                    file=parsed.relpath,
                    line=line,
                    declarations=declarations,
                )
                graph.add_style_rule_node(style_node)

                # STYLED_BY edge (Element -> StyleRule)
                for nid, elem in list(graph.nodes.items()):
                    if isinstance(elem, ElementNode):
                        if _matches_css_selector(selector_text, elem):
                            graph.add_edge(elem.id, rule_id, kind=STYLED_BY)

        for child in node.children:
            _walk_css(child)

    _walk_css(root_node)


def _matches_css_selector(selector: str, elem: ElementNode) -> bool:
    """Check whether a CSS selector string matches an ElementNode."""
    if not selector:
        return False

    # Handle comma-separated selectors (e.g. "#save-btn, .primary")
    sub_selectors = [s.strip() for s in selector.split(",") if s.strip()]
    for sub in sub_selectors:
        if _matches_single_selector(sub, elem):
            return True
    return False


def _matches_single_selector(sel: str, elem: ElementNode) -> bool:
    """Check whether a single sub-selector matches an ElementNode."""
    if sel in elem.selectors:
        return True

    # #id
    if sel.startswith("#"):
        target_id = sel[1:]
        return elem.attributes.get("id") == target_id

    # .class or .class1.class2
    if sel.startswith("."):
        parts = [p for p in sel.split(".") if p]
        elem_classes = set(elem.attributes.get("class", "").split())
        return all(p in elem_classes for p in parts)

    # tag name
    if sel.lower() == elem.tag_name.lower():
        return True

    # Compound tag#id or tag.class
    if "#" in sel:
        tag_part, id_part = sel.split("#", 1)
        if tag_part and tag_part.lower() != elem.tag_name.lower():
            return False
        return elem.attributes.get("id") == id_part

    if "." in sel:
        tag_part = sel.split(".", 1)[0]
        if tag_part and tag_part.lower() != elem.tag_name.lower():
            return False
        classes = [p for p in sel.split(".")[1:] if p]
        elem_classes = set(elem.attributes.get("class", "").split())
        return all(c in elem_classes for c in classes)

    return False


# -- JS/TS Event Handler Parsing ----------------------------------------------


def _process_js_file(
    parsed: ParsedFile,
    graph: UIGraph,
    js_symbol_index: Dict[str, list[Symbol]],
) -> None:
    """Extract addEventListener calls and onX property assignments from JS/TS AST."""
    root_node = parsed.tree.root_node

    def _walk_js(node: Node) -> None:
        # Check addEventListener calls
        if node.type == "call_expression":
            callee = node.child_by_field_name("function")
            if not callee:
                for c in node.children:
                    if c.type == "member_expression":
                        callee = c
                        break

            if callee and callee.type == "member_expression":
                prop = callee.child_by_field_name("property")
                if not prop:
                    for c in callee.children:
                        if c.type == "property_identifier":
                            prop = c
                            break

                if prop and node_text(prop) == "addEventListener":
                    args = node.child_by_field_name("arguments")
                    if not args:
                        for c in node.children:
                            if c.type == "arguments":
                                args = c
                                break

                    if args and args.named_child_count >= 2:
                        event_arg = args.named_children[0]
                        handler_arg = args.named_children[1]

                        event_type = (node_text(event_arg) or "click").strip("'\"")
                        handler_name = node_text(handler_arg) or "<anonymous>"
                        if "(" in handler_name or "{" in handler_name:
                            handler_name = "<anonymous>"

                        js_fqn = _resolve_symbol_fqn(handler_name, parsed.relpath, js_symbol_index)
                        line = node.start_point[0] + 1
                        h_id = f"handler_{parsed.relpath}_{line}_{event_type}"

                        h_node = HandlerNode(
                            id=h_id,
                            event_type=event_type,
                            handler_name=handler_name,
                            js_symbol_fqn=js_fqn,
                            file=parsed.relpath,
                            line=line,
                        )
                        graph.add_handler_node(h_node)

                        # Match target element (e.g. document.getElementById('save-btn'))
                        target_expr = callee.child_by_field_name("object")
                        if not target_expr and callee.children:
                            target_expr = callee.children[0]
                        target_str = node_text(target_expr) if target_expr else ""

                        matched_elems = _find_matching_elements_for_js_target(target_str, graph)
                        for elem_id in matched_elems:
                            graph.add_edge(elem_id, h_id, kind=HANDLED_BY)

                        if js_fqn:
                            if not graph.has_node(js_fqn):
                                graph.add_node(
                                    UIGraphNode(id=js_fqn, node_type="js_symbol", label=handler_name)
                                )
                            graph.add_edge(h_id, js_fqn, kind=IMPLEMENTED_BY)

        # Check onX property assignments (e.g. element.onclick = handleSave)
        elif node.type == "assignment_expression":
            left = node.child_by_field_name("left")
            right = node.child_by_field_name("right")

            if left and left.type == "member_expression":
                prop = left.child_by_field_name("property")
                if not prop:
                    for c in left.children:
                        if c.type == "property_identifier":
                            prop = c
                            break
                prop_name = node_text(prop) or ""

                if prop_name.startswith("on") and len(prop_name) > 2:
                    event_type = prop_name[2:].lower()
                    handler_name = node_text(right) or "<anonymous>"
                    line = node.start_point[0] + 1

                    js_fqn = _resolve_symbol_fqn(handler_name, parsed.relpath, js_symbol_index)
                    h_id = f"handler_{parsed.relpath}_{line}_{event_type}"

                    h_node = HandlerNode(
                        id=h_id,
                        event_type=event_type,
                        handler_name=handler_name,
                        js_symbol_fqn=js_fqn,
                        file=parsed.relpath,
                        line=line,
                    )
                    graph.add_handler_node(h_node)

                    target_expr = left.child_by_field_name("object")
                    if not target_expr and left.children:
                        target_expr = left.children[0]
                    target_str = node_text(target_expr) if target_expr else ""

                    matched_elems = _find_matching_elements_for_js_target(target_str, graph)
                    for elem_id in matched_elems:
                        graph.add_edge(elem_id, h_id, kind=HANDLED_BY)

                    if js_fqn:
                        if not graph.has_node(js_fqn):
                            graph.add_node(
                                UIGraphNode(id=js_fqn, node_type="js_symbol", label=handler_name)
                            )
                        graph.add_edge(h_id, js_fqn, kind=IMPLEMENTED_BY)

        for child in node.children:
            _walk_js(child)

    _walk_js(root_node)


def _find_matching_elements_for_js_target(target_str: str, graph: UIGraph) -> list[str]:
    """Find ElementNode IDs in graph matching a JS target expression."""
    if not target_str:
        return []

    matched: list[str] = []

    # Check for getElementById('id')
    match_id = re.search(r"getElementById\s*\(\s*['\"]([^'\"]+)['\"]\s*\)", target_str)
    if match_id:
        target_id = match_id.group(1)
        for elem_id, elem in graph.nodes.items():
            if isinstance(elem, ElementNode) and (
                elem.id == target_id or elem.attributes.get("id") == target_id
            ):
                matched.append(elem.id)
        return matched

    # Check for querySelector('selector')
    match_qs = re.search(r"querySelector\s*\(\s*['\"]([^'\"]+)['\"]\s*\)", target_str)
    if match_qs:
        sel = match_qs.group(1)
        for elem_id, elem in graph.nodes.items():
            if isinstance(elem, ElementNode) and _matches_css_selector(sel, elem):
                matched.append(elem.id)
        return matched

    # Fallback: check if target_str matches an element ID or variable reference
    for elem_id, elem in graph.nodes.items():
        if isinstance(elem, ElementNode):
            if elem.id == target_str or elem.attributes.get("id") == target_str:
                matched.append(elem.id)

    return matched