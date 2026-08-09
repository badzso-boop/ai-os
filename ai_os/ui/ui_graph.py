"""UI Graph representation and context cache builder for AI OS interface analysis.

This module provides a NetworkX-backed directed graph (UIGraph) connecting DOM elements,
CSS selectors, style rules, event handlers, and JS implementation symbols. It enables
deterministic 0-token analysis of UI state, interactive relationships, and k-hop context
extraction for model triage.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

import networkx as nx

# Typed Edge Constants
MATCHES = "MATCHES"
STYLED_BY = "STYLED_BY"
HANDLED_BY = "HANDLED_BY"
IMPLEMENTED_BY = "IMPLEMENTED_BY"


@dataclass
class ElementNode:
    """Represents an interactive DOM element node."""

    id: str
    tag_name: str = "div"
    selectors: List[str] = field(default_factory=list)
    file: Optional[str] = None
    line: Optional[int] = None
    attributes: Dict[str, Any] = field(default_factory=dict)
    text_content: str = ""
    is_interactive: bool = True
    properties: Dict[str, Any] = field(default_factory=dict)
    parent_id: Optional[str] = None
    children_ids: List[str] = field(default_factory=list)
    node_type: str = "ElementNode"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "node_type": self.node_type,
            "tag_name": self.tag_name,
            "selectors": list(self.selectors),
            "file": self.file,
            "line": self.line,
            "attributes": dict(self.attributes),
            "text_content": self.text_content,
            "is_interactive": self.is_interactive,
            "properties": dict(self.properties),
            "parent_id": self.parent_id,
            "children_ids": list(self.children_ids),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> ElementNode:
        return cls(
            id=data["id"],
            tag_name=data.get("tag_name", "div"),
            selectors=list(data.get("selectors", [])),
            file=data.get("file"),
            line=data.get("line"),
            attributes=dict(data.get("attributes", {})),
            text_content=data.get("text_content", ""),
            is_interactive=data.get("is_interactive", True),
            properties=dict(data.get("properties", {})),
            parent_id=data.get("parent_id"),
            children_ids=list(data.get("children_ids", [])),
            node_type=data.get("node_type", "ElementNode"),
        )


@dataclass
class SelectorNode:
    """Represents a CSS selector or ID anchor node."""

    id: str
    selector: str = ""
    selector_type: str = "css"
    properties: Dict[str, Any] = field(default_factory=dict)
    parent_id: Optional[str] = None
    children_ids: List[str] = field(default_factory=list)
    node_type: str = "SelectorNode"

    def __post_init__(self) -> None:
        if not self.selector:
            self.selector = self.id

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "node_type": self.node_type,
            "selector": self.selector,
            "selector_type": self.selector_type,
            "properties": dict(self.properties),
            "parent_id": self.parent_id,
            "children_ids": list(self.children_ids),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> SelectorNode:
        return cls(
            id=data["id"],
            selector=data.get("selector", data["id"]),
            selector_type=data.get("selector_type", "css"),
            properties=dict(data.get("properties", {})),
            parent_id=data.get("parent_id"),
            children_ids=list(data.get("children_ids", [])),
            node_type=data.get("node_type", "SelectorNode"),
        )


@dataclass
class StyleRuleNode:
    """Represents a CSS style rule node."""

    id: str
    selector: str = ""
    file: Optional[str] = None
    line: Optional[int] = None
    declarations: Dict[str, str] = field(default_factory=dict)
    specificity: Optional[int] = None
    properties: Dict[str, Any] = field(default_factory=dict)
    parent_id: Optional[str] = None
    children_ids: List[str] = field(default_factory=list)
    node_type: str = "StyleRuleNode"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "node_type": self.node_type,
            "selector": self.selector,
            "file": self.file,
            "line": self.line,
            "declarations": dict(self.declarations),
            "specificity": self.specificity,
            "properties": dict(self.properties),
            "parent_id": self.parent_id,
            "children_ids": list(self.children_ids),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> StyleRuleNode:
        return cls(
            id=data["id"],
            selector=data.get("selector", ""),
            file=data.get("file"),
            line=data.get("line"),
            declarations=dict(data.get("declarations", {})),
            specificity=data.get("specificity"),
            properties=dict(data.get("properties", {})),
            parent_id=data.get("parent_id"),
            children_ids=list(data.get("children_ids", [])),
            node_type=data.get("node_type", "StyleRuleNode"),
        )


@dataclass
class HandlerNode:
    """Represents an event handler node."""

    id: str
    event_type: str = "click"
    handler_name: str = ""
    js_symbol_fqn: Optional[str] = None
    file: Optional[str] = None
    line: Optional[int] = None
    properties: Dict[str, Any] = field(default_factory=dict)
    parent_id: Optional[str] = None
    children_ids: List[str] = field(default_factory=list)
    node_type: str = "HandlerNode"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "node_type": self.node_type,
            "event_type": self.event_type,
            "handler_name": self.handler_name,
            "js_symbol_fqn": self.js_symbol_fqn,
            "file": self.file,
            "line": self.line,
            "properties": dict(self.properties),
            "parent_id": self.parent_id,
            "children_ids": list(self.children_ids),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> HandlerNode:
        return cls(
            id=data["id"],
            event_type=data.get("event_type", "click"),
            handler_name=data.get("handler_name", ""),
            js_symbol_fqn=data.get("js_symbol_fqn"),
            file=data.get("file"),
            line=data.get("line"),
            properties=dict(data.get("properties", {})),
            parent_id=data.get("parent_id"),
            children_ids=list(data.get("children_ids", [])),
            node_type=data.get("node_type", "HandlerNode"),
        )


@dataclass
class UIGraphNode:
    """Generic UI graph node for backwards compatibility and generic node storage."""

    id: str
    node_type: str = "element"
    label: str = ""
    properties: Dict[str, Any] = field(default_factory=dict)
    parent_id: Optional[str] = None
    children_ids: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "node_type": self.node_type,
            "label": self.label,
            "properties": dict(self.properties),
            "parent_id": self.parent_id,
            "children_ids": list(self.children_ids),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> UIGraphNode:
        return cls(
            id=data["id"],
            node_type=data.get("node_type", "element"),
            label=data.get("label", ""),
            properties=dict(data.get("properties", {})),
            parent_id=data.get("parent_id"),
            children_ids=list(data.get("children_ids", [])),
        )


@dataclass
class UIGraphEdge:
    """Represents a directed edge between two graph nodes."""

    source_id: str
    target_id: str
    edge_type: str = "relates_to"
    properties: Dict[str, Any] = field(default_factory=dict)

    @property
    def kind(self) -> str:
        return self.edge_type

    def to_dict(self) -> Dict[str, Any]:
        return {
            "source_id": self.source_id,
            "target_id": self.target_id,
            "edge_type": self.edge_type,
            "kind": self.edge_type,
            "properties": dict(self.properties),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> UIGraphEdge:
        return cls(
            source_id=data["source_id"],
            target_id=data["target_id"],
            edge_type=data.get("edge_type") or data.get("kind", "relates_to"),
            properties=dict(data.get("properties", {})),
        )


def _instantiate_typed_node(data: Dict[str, Any]) -> Any:
    ntype = data.get("node_type", "element")
    if ntype == "ElementNode":
        return ElementNode.from_dict(data)
    elif ntype == "SelectorNode":
        return SelectorNode.from_dict(data)
    elif ntype == "StyleRuleNode":
        return StyleRuleNode.from_dict(data)
    elif ntype == "HandlerNode":
        return HandlerNode.from_dict(data)
    else:
        return UIGraphNode.from_dict(data)


class UIGraph:
    """NetworkX-backed UI Graph representation connecting DOM elements, selectors, styles, and handlers."""

    def __init__(self) -> None:
        self.graph: nx.DiGraph = nx.DiGraph()
        self._node_objects: Dict[str, Any] = {}

    @property
    def nodes(self) -> Dict[str, Any]:
        """Return dictionary mapping node IDs to their typed node representations."""
        result = {}
        for nid, data in self.graph.nodes(data=True):
            if nid in self._node_objects:
                result[nid] = self._node_objects[nid]
            else:
                obj = _instantiate_typed_node(dict(data))
                self._node_objects[nid] = obj
                result[nid] = obj
        return result

    @property
    def edges(self) -> List[UIGraphEdge]:
        """Return list of all edges in graph as UIGraphEdge objects."""
        return self.get_edges()

    def add_node(
        self,
        node_or_id: Any,
        node_type: str = "element",
        label: str = "",
        properties: Optional[Dict[str, Any]] = None,
        parent_id: Optional[str] = None,
        **kwargs,
    ) -> Any:
        """Add a node object or raw parameters to the graph."""
        if isinstance(node_or_id, (ElementNode, SelectorNode, StyleRuleNode, HandlerNode, UIGraphNode)):
            node = node_or_id
            node_id = node.id
            node_dict = node.to_dict()
        elif isinstance(node_or_id, dict):
            node_dict = dict(node_or_id)
            node_id = node_dict["id"]
            node = _instantiate_typed_node(node_dict)
        else:
            node_id = str(node_or_id)
            node = UIGraphNode(
                id=node_id,
                node_type=node_type,
                label=label,
                properties=properties or {},
                parent_id=parent_id,
            )
            node_dict = node.to_dict()

        if parent_id and getattr(node, "parent_id", None) != parent_id:
            if hasattr(node, "parent_id"):
                node.parent_id = parent_id
            node_dict["parent_id"] = parent_id

        curr_parent_id = getattr(node, "parent_id", None) or node_dict.get("parent_id")
        if curr_parent_id:
            if curr_parent_id not in self.graph:
                raise ValueError(f"Parent node with ID '{curr_parent_id}' does not exist in graph.")
            parent_data = self.graph.nodes[curr_parent_id]
            children = parent_data.get("children_ids", [])
            if node_id not in children:
                children.append(node_id)
                parent_data["children_ids"] = children

            # Also update python object if present in _node_objects
            if curr_parent_id in self._node_objects:
                p_obj = self._node_objects[curr_parent_id]
                p_children = getattr(p_obj, "children_ids", None)
                if p_children is not None and node_id not in p_children:
                    p_children.append(node_id)

        self._node_objects[node_id] = node
        self.graph.add_node(node_id, **node_dict)

        children_ids = getattr(node, "children_ids", []) or node_dict.get("children_ids", [])
        for child_id in children_ids:
            if child_id in self.graph:
                self.graph.nodes[child_id]["parent_id"] = node_id
                if child_id in self._node_objects:
                    setattr(self._node_objects[child_id], "parent_id", node_id)

        return node

    def add_element_node(self, node_or_id: ElementNode | str, **kwargs) -> ElementNode:
        """Add an ElementNode to the graph."""
        if isinstance(node_or_id, ElementNode):
            node = node_or_id
        else:
            node = ElementNode(id=node_or_id, **kwargs)
        self.add_node(node)
        return node

    def add_selector_node(self, node_or_id: SelectorNode | str, selector: str = "", **kwargs) -> SelectorNode:
        """Add a SelectorNode to the graph."""
        if isinstance(node_or_id, SelectorNode):
            node = node_or_id
        else:
            node = SelectorNode(id=node_or_id, selector=selector or node_or_id, **kwargs)
        self.add_node(node)
        return node

    def add_style_rule_node(self, node_or_id: StyleRuleNode | str, selector: str = "", **kwargs) -> StyleRuleNode:
        """Add a StyleRuleNode to the graph."""
        if isinstance(node_or_id, StyleRuleNode):
            node = node_or_id
        else:
            node = StyleRuleNode(id=node_or_id, selector=selector, **kwargs)
        self.add_node(node)
        return node

    def add_handler_node(self, node_or_id: HandlerNode | str, **kwargs) -> HandlerNode:
        """Add a HandlerNode to the graph."""
        if isinstance(node_or_id, HandlerNode):
            node = node_or_id
        else:
            node = HandlerNode(id=node_or_id, **kwargs)
        self.add_node(node)
        return node

    def get_node(self, node_id: str) -> Optional[Any]:
        """Retrieve a node by its ID converted to its typed representation."""
        if node_id not in self.graph:
            return None
        if node_id in self._node_objects:
            return self._node_objects[node_id]
        obj = _instantiate_typed_node(dict(self.graph.nodes[node_id]))
        self._node_objects[node_id] = obj
        return obj

    def has_node(self, node_id: str) -> bool:
        """Check if node exists in graph."""
        return node_id in self.graph

    def remove_node(self, node_id: str) -> bool:
        """Remove a node and clean up parent/child references."""
        if node_id not in self.graph:
            return False

        data = dict(self.graph.nodes[node_id])
        parent_id = data.get("parent_id")
        children_ids = data.get("children_ids", [])

        if parent_id and parent_id in self.graph:
            parent_children = self.graph.nodes[parent_id].get("children_ids", [])
            if node_id in parent_children:
                parent_children.remove(node_id)
            if parent_id in self._node_objects:
                p_children = getattr(self._node_objects[parent_id], "children_ids", None)
                if p_children is not None and node_id in p_children:
                    p_children.remove(node_id)

        for child_id in children_ids:
            if child_id in self.graph:
                self.graph.nodes[child_id]["parent_id"] = None
                if child_id in self._node_objects:
                    setattr(self._node_objects[child_id], "parent_id", None)

        self._node_objects.pop(node_id, None)
        self.graph.remove_node(node_id)
        return True

    def add_edge(
        self,
        source_id: str,
        target_id: str,
        edge_type: Optional[str] = None,
        kind: Optional[str] = None,
        properties: Optional[Dict[str, Any]] = None,
        **kwargs,
    ) -> UIGraphEdge:
        """Add a directed edge between two existing nodes."""
        if source_id not in self.graph:
            raise ValueError(f"Source node '{source_id}' does not exist.")
        if target_id not in self.graph:
            raise ValueError(f"Target node '{target_id}' does not exist.")

        effective_kind = kind or edge_type or kwargs.pop("kind", None) or kwargs.pop("edge_type", None) or "relates_to"
        edge_properties = dict(properties or {})

        edge_attrs = {
            "edge_type": effective_kind,
            "kind": effective_kind,
            "properties": edge_properties,
        }
        edge_attrs.update(kwargs)

        self.graph.add_edge(
            source_id,
            target_id,
            **edge_attrs,
        )
        return UIGraphEdge(
            source_id=source_id,
            target_id=target_id,
            edge_type=effective_kind,
            properties=edge_properties,
        )

    def remove_edge(
        self, source_id: str, target_id: str, edge_type: Optional[str] = None, kind: Optional[str] = None
    ) -> bool:
        """Remove directed edge between source and target nodes."""
        if not self.graph.has_edge(source_id, target_id):
            return False

        target_kind = kind or edge_type
        if target_kind is not None:
            edata = self.graph.get_edge_data(source_id, target_id) or {}
            ekind = edata.get("kind") or edata.get("edge_type")
            if ekind != target_kind:
                return False

        self.graph.remove_edge(source_id, target_id)
        return True

    def get_edges(
        self,
        source_id: Optional[str] = None,
        target_id: Optional[str] = None,
        edge_type: Optional[str] = None,
        kind: Optional[str] = None,
    ) -> List[UIGraphEdge]:
        """Filter and return edges matching criteria."""
        target_kind = kind or edge_type
        results = []
        for u, v, data in self.graph.edges(data=True):
            if source_id is not None and u != source_id:
                continue
            if target_id is not None and v != target_id:
                continue
            ekind = data.get("kind") or data.get("edge_type")
            if target_kind is not None and ekind != target_kind:
                continue
            props = data.get("properties", {})
            results.append(UIGraphEdge(source_id=u, target_id=v, edge_type=ekind or "relates_to", properties=dict(props)))
        return results

    def get_children(self, node_id: str) -> List[Any]:
        """Get list of immediate child nodes."""
        if node_id not in self.graph:
            raise ValueError(f"Node '{node_id}' does not exist.")
        children_ids = self.graph.nodes[node_id].get("children_ids", [])
        return [self.get_node(cid) for cid in children_ids if cid in self.graph]

    def get_parent(self, node_id: str) -> Optional[Any]:
        """Get parent node if it exists."""
        if node_id not in self.graph:
            raise ValueError(f"Node '{node_id}' does not exist.")
        parent_id = self.graph.nodes[node_id].get("parent_id")
        if parent_id and parent_id in self.graph:
            return self.get_node(parent_id)
        return None

    def get_ancestors(self, node_id: str) -> List[Any]:
        """Get ancestor nodes from parent up to root."""
        ancestors = []
        curr = self.get_parent(node_id)
        visited = set()
        while curr:
            if curr.id in visited:
                break
            visited.add(curr.id)
            ancestors.append(curr)
            curr = self.get_parent(curr.id)
        return ancestors

    def get_descendants(self, node_id: str) -> List[Any]:
        """Get all descendant nodes using BFS."""
        if node_id not in self.graph:
            raise ValueError(f"Node '{node_id}' does not exist.")

        descendants = []
        queue = list(self.get_children(node_id))
        visited = {n.id for n in queue}

        while queue:
            curr = queue.pop(0)
            descendants.append(curr)
            for child in self.get_children(curr.id):
                if child.id not in visited:
                    visited.add(child.id)
                    queue.append(child)

        return descendants

    def get_root_nodes(self) -> List[Any]:
        """Get all root nodes without a parent."""
        return [self.get_node(nid) for nid, data in self.graph.nodes(data=True) if not data.get("parent_id")]

    def find_nodes_by_type(self, node_type: str) -> List[Any]:
        """Find nodes with a given node_type."""
        results = []
        for nid, data in self.graph.nodes(data=True):
            if data.get("node_type") == node_type:
                results.append(self.get_node(nid))
        return results

    def find_nodes_by_property(self, key: str, value: Any) -> List[Any]:
        """Find nodes containing a specific property or attribute key-value pair."""
        results = []
        for nid, data in self.graph.nodes(data=True):
            props = data.get("properties", {})
            if data.get(key) == value or props.get(key) == value:
                results.append(self.get_node(nid))
        return results

    def query_nodes(self, predicate: Callable[[Any], bool]) -> List[Any]:
        """Filter nodes using a custom predicate."""
        results = []
        for nid in self.graph.nodes:
            node = self.get_node(nid)
            if node and predicate(node):
                results.append(node)
        return results

    def find_path(
        self, source_id: str, target_id: str, edge_type: Optional[str] = None, kind: Optional[str] = None
    ) -> Optional[List[str]]:
        """Find shortest path of node IDs from source_id to target_id."""
        if source_id not in self.graph or target_id not in self.graph:
            return None

        target_kind = kind or edge_type
        if source_id == target_id:
            return [source_id]

        queue: List[List[str]] = [[source_id]]
        visited: Set[str] = {source_id}

        while queue:
            path = queue.pop(0)
            curr = path[-1]

            outbound = self.get_edges(source_id=curr, kind=target_kind)
            for edge in outbound:
                nxt = edge.target_id
                if nxt == target_id:
                    return path + [nxt]
                if nxt not in visited:
                    visited.add(nxt)
                    queue.append(path + [nxt])

        return None

    def k_hop_subgraph(self, seeds: List[str], max_hops: int = 2) -> UIGraph:
        """Extract a k-hop subgraph around seed nodes using mixed-direction traversal."""
        visited = {s for s in seeds if s in self.graph}
        frontier = set(visited)
        for _ in range(max_hops):
            next_frontier: Set[str] = set()
            for node in frontier:
                for _, target in self.graph.out_edges(node):
                    if target not in visited:
                        next_frontier.add(target)
                for source, _ in self.graph.in_edges(node):
                    if source not in visited:
                        next_frontier.add(source)
            if not next_frontier:
                break
            visited |= next_frontier
            frontier = next_frontier

        sub = UIGraph()
        sub.graph = self.graph.subgraph(visited).copy()
        return sub

    def validate_hierarchy(self) -> List[str]:
        """Validate parent-child structure and return error descriptions."""
        errors: List[str] = []
        for nid, data in self.graph.nodes(data=True):
            parent_id = data.get("parent_id")
            children_ids = data.get("children_ids", [])

            if parent_id and parent_id not in self.graph:
                errors.append(f"Node '{nid}' references non-existent parent '{parent_id}'.")

            for child_id in children_ids:
                if child_id not in self.graph:
                    errors.append(f"Node '{nid}' references non-existent child '{child_id}'.")
                else:
                    child_parent = self.graph.nodes[child_id].get("parent_id")
                    if child_parent != nid:
                        errors.append(
                            f"Node '{nid}' claims child '{child_id}', but child's parent_id is '{child_parent}'."
                        )
        return errors

    def to_dict(self) -> Dict[str, Any]:
        """Serialize graph to dictionary structure."""
        nodes_list = []
        for nid in self.graph.nodes:
            node = self.get_node(nid)
            if hasattr(node, "to_dict"):
                nodes_list.append(node.to_dict())
            else:
                nodes_list.append(dict(self.graph.nodes[nid]))
        edges_list = [e.to_dict() for e in self.edges]
        return {
            "nodes": nodes_list,
            "edges": edges_list,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> UIGraph:
        """Deserialize graph from dictionary structure."""
        graph = cls()
        for node_dict in data.get("nodes", []):
            graph.add_node(node_dict)
        for edge_dict in data.get("edges", []):
            graph.add_edge(
                source_id=edge_dict["source_id"],
                target_id=edge_dict["target_id"],
                kind=edge_dict.get("edge_type") or edge_dict.get("kind", "relates_to"),
                properties=edge_dict.get("properties", {}),
            )
        return graph

    def to_json(self, indent: Optional[int] = None) -> str:
        """Serialize graph to JSON string."""
        return json.dumps(self.to_dict(), indent=indent)

    @classmethod
    def from_json(cls, json_str: str) -> UIGraph:
        """Deserialize graph from JSON string."""
        data = json.loads(json_str)
        return cls.from_dict(data)


def build_ui_context_cache(graph: UIGraph, focus_selector: Optional[str] = None) -> str:
    """Build a compact, human/LLM-readable text summary of the UI graph.

    Mirrors KnowledgeEngine.build_context_cache. If focus_selector is specified,
    filters the context around nodes matching that selector using k-hop extraction.
    """
    subgraph = graph
    if focus_selector:
        seed_ids: List[str] = []
        for nid, node in graph.nodes.items():
            if nid == focus_selector:
                seed_ids.append(nid)
                continue
            if isinstance(node, SelectorNode) and node.selector == focus_selector:
                seed_ids.append(nid)
            elif isinstance(node, ElementNode) and focus_selector in node.selectors:
                seed_ids.append(nid)
            elif hasattr(node, "selector") and getattr(node, "selector") == focus_selector:
                seed_ids.append(nid)

        if not seed_ids:
            # Fallback substring match
            for nid, node in graph.nodes.items():
                sel_str = getattr(node, "selector", "") or getattr(node, "label", "") or nid
                if focus_selector in sel_str:
                    seed_ids.append(nid)

        if seed_ids:
            subgraph = graph.k_hop_subgraph(seed_ids, max_hops=2)

    blocks: List[str] = []
    processed_node_ids: Set[str] = set()

    # Process ElementNodes first
    elements = [n for n in subgraph.nodes.values() if isinstance(n, ElementNode)]

    for elem in elements:
        processed_node_ids.add(elem.id)
        lines = [f"// Element: {elem.id} (<{elem.tag_name}>)"]

        if elem.file:
            loc = f"{elem.file}:{elem.line}" if elem.line else elem.file
            lines.append(f"  File: {loc}")
        if elem.text_content:
            lines.append(f"  Text: \"{elem.text_content}\"")
        if elem.selectors:
            lines.append(f"  Selectors: {', '.join(elem.selectors)}")
        if elem.attributes:
            lines.append(f"  Attributes: {json.dumps(elem.attributes)}")
        lines.append(f"  Interactive: {elem.is_interactive}")

        # Find STYLED_BY edges
        style_edges = subgraph.get_edges(source_id=elem.id, kind=STYLED_BY)
        if style_edges:
            lines.append("  Matched Styles:")
            for s_edge in style_edges:
                style_node = subgraph.get_node(s_edge.target_id)
                if isinstance(style_node, StyleRuleNode):
                    processed_node_ids.add(style_node.id)
                    loc = f" [{style_node.file}:{style_node.line}]" if style_node.file else ""
                    decls = "; ".join(f"{k}: {v}" for k, v in style_node.declarations.items())
                    lines.append(f"    - {style_node.id} ({style_node.selector}){loc}: {decls}")

        # Find HANDLED_BY edges
        handler_edges = subgraph.get_edges(source_id=elem.id, kind=HANDLED_BY)
        if handler_edges:
            lines.append("  Handlers:")
            for h_edge in handler_edges:
                h_node = subgraph.get_node(h_edge.target_id)
                if isinstance(h_node, HandlerNode):
                    processed_node_ids.add(h_node.id)
                    loc = f" [{h_node.file}:{h_node.line}]" if h_node.file else ""
                    fqn_str = f" (FQN: {h_node.js_symbol_fqn})" if h_node.js_symbol_fqn else ""
                    lines.append(
                        f"    - {h_node.id} [{h_node.event_type}]: {h_node.handler_name}{fqn_str}{loc}"
                    )

        blocks.append("\n".join(lines))

    # Process remaining nodes (SelectorNodes, StyleRuleNodes, HandlerNodes, UIGraphNodes)
    remaining_nodes = [
        n for nid, n in subgraph.nodes.items() if nid not in processed_node_ids
    ]
    for node in remaining_nodes:
        if isinstance(node, SelectorNode):
            blocks.append(
                f"// Selector: {node.id}\n  Selector: {node.selector}\n  Type: {node.selector_type}"
            )
        elif isinstance(node, StyleRuleNode):
            loc = f" [{node.file}:{node.line}]" if node.file else ""
            decls = "; ".join(f"{k}: {v}" for k, v in node.declarations.items())
            blocks.append(f"// StyleRule: {node.id} ({node.selector}){loc}\n  Declarations: {decls}")
        elif isinstance(node, HandlerNode):
            loc = f" [{node.file}:{node.line}]" if node.file else ""
            fqn_str = f" (FQN: {node.js_symbol_fqn})" if node.js_symbol_fqn else ""
            blocks.append(
                f"// Handler: {node.id} [{node.event_type}] -> {node.handler_name}{fqn_str}{loc}"
            )
        elif isinstance(node, UIGraphNode):
            lines = [f"// Node: {node.id} ({node.node_type})"]
            if node.label:
                lines.append(f"  Label: {node.label}")
            if node.properties:
                lines.append(f"  Properties: {json.dumps(node.properties)}")
            blocks.append("\n".join(lines))

    total_symbols = len(blocks)
    focus_str = focus_selector if focus_selector else "None"
    header = f"=== COMPRESSED UI CONTEXT CACHE ({total_symbols} SYMBOLS, focus={focus_str}) ===\n"

    if not blocks:
        return header + "(empty)"

    return header + "\n\n".join(blocks)