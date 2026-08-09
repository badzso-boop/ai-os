"""Static UI bug detectors for analyzing UI graph representations.

This module provides deterministic, 0-token heuristics for identifying common UI flaws
and accessibility bugs within a NetworkX-backed UIGraph. It defines the Suspicion
dataclass for recording issue findings and individual pure-function detectors:

- no_handler: Identifies interactive elements (buttons, links, etc.) lacking attached handlers.
- dead_handler: Identifies bound handlers that are empty, no-op, or unattached.
- duplicate_id: Identifies elements sharing duplicate HTML 'id' attributes.
- submit_outside_form: Identifies submit buttons not enclosed within a <form> element.
- no_accessible_name: Identifies interactive elements lacking accessible names for screen readers.

The main entry point run_detectors aggregates suspicions across all detectors and returns
them sorted by numeric weight in descending order.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Set

from ai_os.ui.ui_graph import HANDLED_BY, ElementNode, HandlerNode, UIGraph


@dataclass
class Suspicion:
    """Represents a potential UI bug or flaw detected in a UIGraph."""

    element_ref: str
    kind: str
    evidence: str
    weight: float = 1.0

    def to_dict(self) -> Dict[str, Any]:
        """Serialize suspicion to dictionary format."""
        return {
            "element_ref": self.element_ref,
            "kind": self.kind,
            "evidence": self.evidence,
            "weight": self.weight,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> Suspicion:
        """Deserialize suspicion from dictionary format."""
        return cls(
            element_ref=data["element_ref"],
            kind=data["kind"],
            evidence=data["evidence"],
            weight=float(data.get("weight", 1.0)),
        )


def _get_element_nodes(graph: UIGraph) -> List[ElementNode]:
    """Helper to extract all ElementNode instances from graph."""
    elements: List[ElementNode] = []
    for nid in graph.graph.nodes:
        node = graph.get_node(nid)
        if isinstance(node, ElementNode):
            elements.append(node)
        elif getattr(node, "node_type", None) == "ElementNode":
            elements.append(node)
    return elements


def _is_interactive(elem: ElementNode) -> bool:
    """Check if an element is interactive (button, link, input, or explicit role/flag)."""
    if getattr(elem, "is_interactive", False):
        return True
    tag = str(getattr(elem, "tag_name", "")).lower()
    if tag in ("button", "a", "input", "select", "textarea"):
        return True
    attrs = getattr(elem, "attributes", {}) or {}
    role = str(attrs.get("role", "")).lower()
    if role in ("button", "link", "checkbox", "radio", "menuitem", "tab", "textbox", "combobox"):
        return True
    return False


def no_handler(graph: UIGraph) -> List[Suspicion]:
    """Detect interactive elements that lack any bound event handler or action target."""
    suspicions: List[Suspicion] = []

    for elem in _get_element_nodes(graph):
        if not _is_interactive(elem):
            continue

        # Check for HANDLED_BY edges in graph
        handler_edges = graph.get_edges(source_id=elem.id, kind=HANDLED_BY)
        if handler_edges:
            continue

        attrs = getattr(elem, "attributes", {}) or {}

        # Check for inline handler attributes
        has_inline = False
        for key, val in attrs.items():
            key_lower = str(key).lower()
            if (
                key_lower.startswith("on")
                or key_lower.startswith("@")
                or key_lower.startswith("v-on:")
                or key_lower.startswith("(")
            ):
                if val and str(val).strip() not in ("", "javascript:void(0)", "javascript:;", "noop()"):
                    has_inline = True
                    break
        if has_inline:
            continue

        tag = str(getattr(elem, "tag_name", "")).lower()

        # Check for <a> tag with valid href
        if tag == "a":
            href = attrs.get("href")
            if href and str(href).strip() not in ("", "#", "javascript:void(0)", "javascript:;"):
                continue

        # Check for submit button inside form or referencing form
        btn_type = str(attrs.get("type", "")).lower()
        if btn_type == "submit" or (tag == "button" and btn_type != "button"):
            ancestors = graph.get_ancestors(elem.id)
            if any(str(getattr(anc, "tag_name", "")).lower() == "form" for anc in ancestors):
                continue
            if attrs.get("form"):
                continue

        suspicions.append(
            Suspicion(
                element_ref=elem.id,
                kind="no_handler",
                evidence=f"Interactive element '{elem.id}' (<{elem.tag_name}>) has no bound event handler or navigation target.",
                weight=0.8,
            )
        )

    return suspicions


def dead_handler(graph: UIGraph) -> List[Suspicion]:
    """Detect handlers that are empty, no-ops, or unattached to any element."""
    suspicions: List[Suspicion] = []
    dead_values = {"", "noop", "noop()", "undefined", "null", "none", "javascript:void(0)", "javascript:;"}

    # 1. Check HANDLED_BY edges from elements
    for elem in _get_element_nodes(graph):
        handler_edges = graph.get_edges(source_id=elem.id, kind=HANDLED_BY)
        for edge in handler_edges:
            h_node = graph.get_node(edge.target_id)
            if h_node:
                h_name = str(getattr(h_node, "handler_name", "") or "").strip()
                fqn = str(getattr(h_node, "js_symbol_fqn", "") or "").strip()
                is_dead_prop = getattr(h_node, "properties", {}).get("is_dead", False)

                if is_dead_prop or (not fqn and (not h_name or h_name.lower() in dead_values)):
                    suspicions.append(
                        Suspicion(
                            element_ref=elem.id,
                            kind="dead_handler",
                            evidence=f"Element '{elem.id}' is bound to dead/empty handler '{h_node.id}' ({h_name or 'unnamed'}).",
                            weight=0.7,
                        )
                    )

        # Check inline attributes
        attrs = getattr(elem, "attributes", {}) or {}
        for key, val in attrs.items():
            key_lower = str(key).lower()
            if key_lower.startswith("on"):
                val_str = str(val or "").strip().lower()
                if val_str in dead_values:
                    suspicions.append(
                        Suspicion(
                            element_ref=elem.id,
                            kind="dead_handler",
                            evidence=f"Element '{elem.id}' has inline handler attribute '{key}' with dead value '{val}'.",
                            weight=0.7,
                        )
                    )

    # 2. Check orphaned HandlerNodes in graph
    for nid in graph.graph.nodes:
        node = graph.get_node(nid)
        if isinstance(node, HandlerNode) or getattr(node, "node_type", None) == "HandlerNode":
            in_edges = graph.get_edges(target_id=node.id, kind=HANDLED_BY)
            if not in_edges:
                h_name = getattr(node, "handler_name", "") or node.id
                suspicions.append(
                    Suspicion(
                        element_ref=node.id,
                        kind="dead_handler",
                        evidence=f"Handler node '{node.id}' ({h_name}) is not bound to any DOM element.",
                        weight=0.7,
                    )
                )

    return suspicions


def duplicate_id(graph: UIGraph) -> List[Suspicion]:
    """Detect elements sharing duplicate HTML 'id' attributes."""
    suspicions: List[Suspicion] = []
    id_to_elements: Dict[str, List[str]] = {}

    for elem in _get_element_nodes(graph):
        attrs = getattr(elem, "attributes", {}) or {}
        html_id = attrs.get("id")
        if html_id and isinstance(html_id, str) and html_id.strip():
            id_to_elements.setdefault(html_id.strip(), []).append(elem.id)

    for html_id, elem_ids in id_to_elements.items():
        if len(elem_ids) > 1:
            for elem_id in elem_ids:
                suspicions.append(
                    Suspicion(
                        element_ref=elem_id,
                        kind="duplicate_id",
                        evidence=f"Duplicate HTML id '{html_id}' found across elements: {', '.join(elem_ids)}.",
                        weight=0.9,
                    )
                )

    return suspicions


def submit_outside_form(graph: UIGraph) -> List[Suspicion]:
    """Detect submit buttons that are not enclosed within a <form> element."""
    suspicions: List[Suspicion] = []

    for elem in _get_element_nodes(graph):
        tag = str(getattr(elem, "tag_name", "")).lower()
        attrs = getattr(elem, "attributes", {}) or {}
        btn_type = str(attrs.get("type", "")).lower()

        is_submit = (btn_type == "submit") or (tag == "button" and btn_type in ("submit", ""))
        if not is_submit:
            continue

        # Check if inside a form
        ancestors = graph.get_ancestors(elem.id)
        inside_form = any(str(getattr(anc, "tag_name", "")).lower() == "form" for anc in ancestors)
        has_form_attr = bool(attrs.get("form"))

        if not inside_form and not has_form_attr:
            suspicions.append(
                Suspicion(
                    element_ref=elem.id,
                    kind="submit_outside_form",
                    evidence=f"Submit button '{elem.id}' (<{elem.tag_name}>) is not inside a <form> element and lacks a 'form' attribute.",
                    weight=0.85,
                )
            )

    return suspicions


def no_accessible_name(graph: UIGraph) -> List[Suspicion]:
    """Detect interactive elements that lack an accessible name for screen readers."""
    suspicions: List[Suspicion] = []

    for elem in _get_element_nodes(graph):
        if not _is_interactive(elem):
            continue

        attrs = getattr(elem, "attributes", {}) or {}
        tag = str(getattr(elem, "tag_name", "")).lower()

        # Check for direct accessible name sources
        text = str(getattr(elem, "text_content", "") or "").strip()
        aria_label = str(attrs.get("aria-label", "") or "").strip()
        aria_labelledby = str(attrs.get("aria-labelledby", "") or "").strip()
        title = str(attrs.get("title", "") or "").strip()
        alt = str(attrs.get("alt", "") or "").strip()
        placeholder = str(attrs.get("placeholder", "") or "").strip()
        val = str(attrs.get("value", "") or "").strip() if tag == "input" else ""

        if text or aria_label or aria_labelledby or title or alt or placeholder or val:
            continue

        # Check for associated label element in graph (by target ID or parent)
        html_id = attrs.get("id") or elem.id
        has_label = False

        for nid in graph.graph.nodes:
            other = graph.get_node(nid)
            if isinstance(other, ElementNode) or getattr(other, "node_type", None) == "ElementNode":
                if str(getattr(other, "tag_name", "")).lower() == "label":
                    other_attrs = getattr(other, "attributes", {}) or {}
                    if str(other_attrs.get("for", "")).strip() in (elem.id, html_id):
                        has_label = True
                        break

        if has_label:
            continue

        # Check if parent is a <label>
        ancestors = graph.get_ancestors(elem.id)
        if any(str(getattr(anc, "tag_name", "")).lower() == "label" for anc in ancestors):
            continue

        suspicions.append(
            Suspicion(
                element_ref=elem.id,
                kind="no_accessible_name",
                evidence=f"Interactive element '{elem.id}' (<{elem.tag_name}>) lacks an accessible name (no text content, aria-label, title, or associated label).",
                weight=0.6,
            )
        )

    return suspicions


# Aliases for function names with detect_ prefix
detect_no_handler = no_handler
detect_dead_handler = dead_handler
detect_duplicate_id = duplicate_id
detect_submit_outside_form = submit_outside_form
detect_no_accessible_name = no_accessible_name

DETECTORS = [
    no_handler,
    dead_handler,
    duplicate_id,
    submit_outside_form,
    no_accessible_name,
]


def run_detectors(graph: UIGraph) -> List[Suspicion]:
    """Run all static UI bug detectors against graph and return suspicions sorted by weight."""
    results: List[Suspicion] = []
    for detector in DETECTORS:
        results.extend(detector(graph))

    # Sort suspicions by weight descending, then element_ref and kind for deterministic output
    results.sort(key=lambda s: (-s.weight, s.element_ref, s.kind))
    return results


__all__ = [
    "Suspicion",
    "run_detectors",
    "no_handler",
    "dead_handler",
    "duplicate_id",
    "submit_outside_form",
    "no_accessible_name",
    "detect_no_handler",
    "detect_dead_handler",
    "detect_duplicate_id",
    "detect_submit_outside_form",
    "detect_no_accessible_name",
]