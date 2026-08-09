"""UI Bug Report Matcher and Diagnosis Assembler.

This module provides deterministic, 0-token heuristics for matching free-text bug reports
(in English or Hungarian) against element nodes in a UIGraph, and assembling compact,
ranked UI diagnosis blocks for downstream triage and repair.

Key functions:
- match_bug_report(graph, report_text): Matches report terms, selectors, attributes, and visible
  text content against elements in the graph, computing a normalized match score per element.
- assemble_ui_diagnosis(graph, suspicions, report_text): Assembles a compact, deterministic text block
  ranking static suspicions around the identified target element using k-hop graph proximity.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import difflib

import re
from typing import Any, Dict, List, Optional, Set, Tuple
import unicodedata

import networkx as nx

from ai_os.ui.detectors import Suspicion
from ai_os.ui.ui_graph import ElementNode, UIGraph, build_ui_context_cache


@dataclass
class ReportMatch:
    """Represents the result of matching a free-text bug report against a UIGraph."""

    target_element_id: Optional[str]
    target_element: Optional[ElementNode] = None
    score: float = 0.0
    matched_terms: List[str] = field(default_factory=list)
    matched_selectors: List[str] = field(default_factory=list)
    scores_by_element: Dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """Serialize match result to dictionary format."""
        return {
            "target_element_id": self.target_element_id,
            "score": round(self.score, 4),
            "matched_terms": list(self.matched_terms),
            "matched_selectors": list(self.matched_selectors),
            "scores_by_element": {k: round(v, 4) for k, v in self.scores_by_element.items()},
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> ReportMatch:
        """Deserialize match result from dictionary format."""
        return cls(
            target_element_id=data.get("target_element_id"),
            score=float(data.get("score", 0.0)),
            matched_terms=list(data.get("matched_terms", [])),
            matched_selectors=list(data.get("matched_selectors", [])),
            scores_by_element=dict(data.get("scores_by_element", {})),
        )


def _normalize_string(text: str) -> str:
    """Normalize text for matching: NFD unaccented, lowercased, stripped."""
    if not text:
        return ""
    nfkd = unicodedata.normalize("NFD", text)
    unaccented = "".join(c for c in nfkd if unicodedata.category(c) != "Mn")
    return unaccented.lower().strip()


# Synonym mapping for common Hungarian and English UI terms
UI_SYNONYMS: Dict[str, Set[str]] = {
    "button": {"gomb", "btn", "button", "kattintható", "kattinthato", "submit", "mentés", "mentes"},
    "input": {"bevitel", "mező", "mezo", "input", "textarea", "textbox", "kitöltés", "kitoltes"},
    "a": {"hivatkozás", "hivatkozas", "link", "a", "anchor", "navigáció", "navigacio"},
    "form": {"űrlap", "urlap", "form"},
    "select": {"választó", "valaszto", "dropdown", "select", "menü", "menu"},
}


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


def match_bug_report(graph: UIGraph, report_text: str) -> ReportMatch:
    """Fuzzy match free-text bug report terms against UIGraph element selectors and visible text.

    Args:
        graph: The UIGraph containing UI elements, selectors, and handlers.
        report_text: Free-text bug report string (e.g. "a Mentés gomb nem működik").

    Returns:
        ReportMatch object containing the top target element, score, and breakdown.
    """
    if not report_text or not report_text.strip():
        return ReportMatch(target_element_id=None, score=0.0)

    elements = _get_element_nodes(graph)
    if not elements:
        return ReportMatch(target_element_id=None, score=0.0)

    norm_report = _normalize_string(report_text)
    words = [w for w in re.split(r"[\s,.;:!?\"'()\[\]{}]+", norm_report) if len(w) >= 2]
    selectors_in_report = [s for s in report_text.split() if s.startswith("#") or s.startswith(".")]

    scores_by_element: Dict[str, float] = {}
    matches_meta: Dict[str, Tuple[List[str], List[str]]] = {}

    for elem in elements:
        score = 0.0
        matched_terms: Set[str] = set()
        matched_selectors: Set[str] = set()

        elem_id_norm = _normalize_string(elem.id)
        tag_norm = _normalize_string(elem.tag_name)
        text_norm = _normalize_string(elem.text_content)
        selectors_norm = [_normalize_string(s) for s in elem.selectors]

        # 1. Explicit ID/Selector matching
        for sel in selectors_in_report:
            sel_norm = _normalize_string(sel)
            if sel_norm == f"#{elem_id_norm}" or sel_norm == elem_id_norm or sel in elem.selectors or sel_norm in selectors_norm:
                score += 1.0
                matched_selectors.add(sel)

        if elem_id_norm and elem_id_norm in norm_report:
            score += 0.9
            matched_terms.add(elem.id)

        # 2. Element Selectors matching
        for sel in elem.selectors:
            sel_clean = _normalize_string(sel.lstrip("#."))
            if sel_clean and sel_clean in norm_report:
                score += 0.7
                matched_selectors.add(sel)

        # 3. Visible Text Content matching
        if text_norm:
            if text_norm == norm_report or text_norm in words:
                score += 0.8
                matched_terms.add(elem.text_content)
            elif text_norm in norm_report or norm_report in text_norm:
                score += 0.6
                matched_terms.add(elem.text_content)
            else:
                # Word token overlap
                text_words = [w for w in re.split(r"\s+", text_norm) if len(w) >= 2]
                overlap = set(words).intersection(text_words)
                if overlap:
                    score += 0.5 * (len(overlap) / max(len(text_words), 1))
                    matched_terms.update(overlap)

                # Sequence matching ratio
                seq_ratio = difflib.SequenceMatcher(None, text_norm, norm_report).ratio()
                if seq_ratio > 0.6:
                    score += 0.5 * seq_ratio
                    matched_terms.add(elem.text_content)

        # 4. Attributes matching (aria-label, placeholder, title, value, name, data-testid)
        attrs = getattr(elem, "attributes", {}) or {}
        for attr_key in ("aria-label", "placeholder", "title", "value", "name", "data-testid"):
            val = attrs.get(attr_key)
            if val:
                val_norm = _normalize_string(str(val))
                if val_norm and (val_norm in norm_report or any(w in val_norm for w in words)):
                    score += 0.6
                    matched_terms.add(f"{attr_key}={val}")

        # 5. Tag / UI Synonym matching
        synonyms = UI_SYNONYMS.get(tag_norm, set())
        synonyms.add(tag_norm)
        for syn in synonyms:
            if syn in norm_report or syn in words:
                score += 0.3
                matched_terms.add(syn)
                break

        scores_by_element[elem.id] = round(score, 4)
        matches_meta[elem.id] = (sorted(matched_terms), sorted(matched_selectors))

    # Pick top element deterministically
    sorted_elements = sorted(
        elements,
        key=lambda e: (-scores_by_element[e.id], e.id),
    )

    top_elem = sorted_elements[0]
    top_score = scores_by_element[top_elem.id]

    if top_score <= 0.0:
        return ReportMatch(
            target_element_id=None,
            target_element=None,
            score=0.0,
            matched_terms=[],
            matched_selectors=[],
            scores_by_element=scores_by_element,
        )

    matched_terms, matched_selectors = matches_meta[top_elem.id]
    return ReportMatch(
        target_element_id=top_elem.id,
        target_element=top_elem,
        score=top_score,
        matched_terms=matched_terms,
        matched_selectors=matched_selectors,
        scores_by_element=scores_by_element,
    )


def assemble_ui_diagnosis(
    graph: UIGraph,
    suspicions: List[Suspicion | Dict[str, Any]],
    report_text: Optional[str] = None,
) -> str:
    """Format a compact, deterministic diagnosis text block ranking suspicions around the target element.

    Args:
        graph: The UIGraph representing the UI state.
        suspicions: List of static Suspicion objects or dictionaries.
        report_text: Optional free-text report string to match target element.

    Returns:
        Formatted diagnosis report string suitable for LLM triage or human inspection.
    """
    # Standardize suspicions list
    suspicion_objs: List[Suspicion] = []
    for item in suspicions:
        if isinstance(item, Suspicion):
            suspicion_objs.append(item)
        elif isinstance(item, dict):
            suspicion_objs.append(Suspicion.from_dict(item))

    # Perform report matching if report_text is provided
    match_result: Optional[ReportMatch] = None
    target_id: Optional[str] = None
    target_elem: Optional[ElementNode] = None

    if report_text:
        match_result = match_bug_report(graph, report_text)
        target_id = match_result.target_element_id
        target_elem = match_result.target_element

    # Compute undirected graph for proximity calculation if target_id exists
    undirected_graph: Optional[nx.Graph] = None
    if target_id and target_id in graph.graph:
        undirected_graph = graph.graph.to_undirected()

    # Rank suspicions based on target element proximity and base weight
    ranked_items: List[Tuple[float, float, int, str, Suspicion]] = []

    for susp in suspicion_objs:
        proximity_factor = 1.0
        hops: Optional[int] = None
        proximity_desc = "unconnected"

        if target_id and undirected_graph and susp.element_ref in undirected_graph:
            if susp.element_ref == target_id:
                hops = 0
                proximity_factor = 2.0
                proximity_desc = "0 hops - target element"
            elif nx.has_path(undirected_graph, target_id, susp.element_ref):
                dist = nx.shortest_path_length(undirected_graph, target_id, susp.element_ref)
                hops = dist
                if dist == 1:
                    proximity_factor = 1.5
                    proximity_desc = "1 hop - adjacent"
                elif dist == 2:
                    proximity_factor = 1.2
                    proximity_desc = "2 hops - near"
                else:
                    proximity_factor = 1.0
                    proximity_desc = f"{dist} hops"

        rank_score = round(susp.weight * proximity_factor, 4)
        hops_val = hops if hops is not None else 999
        ranked_items.append((rank_score, susp.weight, hops_val, proximity_desc, susp))

    # Sort deterministically:
    # 1. rank_score descending
    # 2. base_weight descending
    # 3. hops_val ascending
    # 4. kind ascending
    # 5. element_ref ascending
    # 6. evidence ascending
    ranked_items.sort(
        key=lambda item: (
            -item[0],
            -item[1],
            item[2],
            item[4].kind,
            item[4].element_ref,
            item[4].evidence,
        )
    )

    # Format header and target info
    lines: List[str] = ["=== UI DIAGNOSIS REPORT ==="]
    if report_text:
        lines.append(f'Report Query: "{report_text.strip()}"')
    else:
        lines.append("Report Query: None")

    if target_elem:
        loc = f" [{target_elem.file}:{target_elem.line}]" if target_elem.file else ""
        lines.append(f"Target Element: {target_elem.id} (<{target_elem.tag_name}>){loc}")
        if match_result:
            lines.append(f"  Match Score: {match_result.score:.2f}")
            if match_result.matched_selectors:
                lines.append(f"  Matched Selectors: {', '.join(match_result.matched_selectors)}")
            if match_result.matched_terms:
                lines.append(f"  Matched Terms: {', '.join(match_result.matched_terms)}")
    elif target_id:
        lines.append(f"Target Element: {target_id}")
    else:
        lines.append("Target Element: None (General Analysis)")

    lines.append("")
    lines.append(f"Ranked Suspicions ({len(ranked_items)} total):")

    if not ranked_items:
        lines.append("  (No suspicions detected)")
    else:
        for idx, (rank_score, base_weight, _, prox_desc, susp) in enumerate(ranked_items, start=1):
            lines.append(
                f"  {idx}. [{susp.kind}] rank_score={rank_score:.2f} (base_weight={base_weight:.2f}) on '{susp.element_ref}' (proximity: {prox_desc})"
            )
            lines.append(f"     Evidence: {susp.evidence}")

    lines.append("")
    lines.append("Target UI Context:")
    context_str = build_ui_context_cache(graph, focus_selector=target_id)
    lines.append(context_str)

    return "\n".join(lines)


__all__ = [
    "ReportMatch",
    "match_bug_report",
    "assemble_ui_diagnosis",
]