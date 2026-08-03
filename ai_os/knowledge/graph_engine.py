"""NetworkX-backed Knowledge Graph and k-hop context extraction (doc 04 / doc 08).

Node types: FileNode, ClassNode (covers interfaces via a `kind` attribute),
FunctionNode (covers methods via `is_method`), TypeNode (SQL tables/views).
Edge kinds: CONTAINS, IMPORTS, CALLS, EXTENDS (Java `implements` recorded as
EXTENDS with `via="implements"` rather than inventing a fifth edge type).
"""
from __future__ import annotations

import json
from pathlib import Path

import networkx as nx
from networkx.readwrite import json_graph

from ai_os.analyzer.call_graph_builder import ProjectScanResult
from ai_os.analyzer.languages import LANGUAGES
from ai_os.analyzer.tree_sitter_engine import Symbol
from ai_os.knowledge.skeleton_extractor import extract_skeleton

# k-hop traversal directions per edge kind (doc 08 §2): outgoing for structural/dependency
# edges, incoming for CALLS (who calls this symbol), CONTAINS both ways so a seed symbol
# always pulls in its file and vice versa.
_OUTGOING_HOP_KINDS = {"IMPORTS", "EXTENDS", "CONTAINS"}
_INCOMING_HOP_KINDS = {"CALLS", "CONTAINS"}

_KIND_TO_KEY = {"class": "ClassNode", "interface": "ClassNode", "function": "FunctionNode", "method": "FunctionNode", "type": "TypeNode"}


class KnowledgeEngine:
    """Wraps a networkx.DiGraph of source-code entities and their relationships."""

    def __init__(self) -> None:
        self.graph: nx.DiGraph = nx.DiGraph()

    # -- construction -----------------------------------------------------------------

    def add_file_node(self, relpath: str, language: str) -> None:
        self.graph.add_node(relpath, node_type="FileNode", language=language)

    def add_symbol_node(self, symbol: Symbol, stub: str | None = None) -> None:
        node_type = _KIND_TO_KEY[symbol.kind]
        self.graph.add_node(
            symbol.fqn,
            node_type=node_type,
            kind=symbol.kind,
            name=symbol.name,
            file=symbol.relpath,
            start_line=symbol.start_line,
            end_line=symbol.end_line,
            params=list(symbol.params),
            return_type=symbol.return_type,
            docstring=symbol.docstring,
            parent_class=symbol.parent_class,
            is_method=symbol.is_method,
            stub=stub,
        )
        self.graph.add_edge(symbol.relpath, symbol.fqn, kind="CONTAINS")

    def add_edge(self, source: str, target: str, kind: str, **attrs: object) -> None:
        if source not in self.graph or target not in self.graph:
            return
        self.graph.add_edge(source, target, kind=kind, **attrs)

    def build_from_scan(self, scan: ProjectScanResult) -> None:
        """Populates the graph from a CallGraphBuilder.scan() result, including
        signature-only skeleton stubs for every extracted symbol."""
        for file_result in scan.files:
            self.add_file_node(file_result.parsed.relpath, file_result.parsed.language)
            profile = LANGUAGES[file_result.parsed.language]
            for symbol in file_result.symbols:
                stub = extract_skeleton(symbol, file_result.parsed.source, profile)
                self.add_symbol_node(symbol, stub=stub)

        for edge in scan.import_edges:
            if edge.resolved and edge.target_relpath:
                self.add_edge(
                    edge.source_relpath,
                    edge.target_relpath,
                    "IMPORTS",
                    raw_specifier=edge.raw_specifier,
                )

        for edge in scan.call_edges:
            for callee_fqn in edge.callee_fqns:
                self.add_edge(
                    edge.caller_fqn,
                    callee_fqn,
                    "CALLS",
                    ambiguous=edge.ambiguous,
                    line=edge.line,
                )

        for edge in scan.extends_edges:
            for target_fqn in edge.target_fqns:
                self.add_edge(
                    edge.source_fqn,
                    target_fqn,
                    "EXTENDS",
                    via=edge.via,
                    ambiguous=edge.ambiguous,
                )

    # -- queries -----------------------------------------------------------------

    def k_hop_subgraph(self, seeds: list[str], max_hops: int = 2) -> nx.DiGraph:
        """Mixed-direction BFS: outgoing IMPORTS/EXTENDS/CONTAINS, incoming CALLS/CONTAINS."""
        visited = {s for s in seeds if s in self.graph}
        frontier = set(visited)
        for _ in range(max_hops):
            next_frontier: set[str] = set()
            for node in frontier:
                for _, target, data in self.graph.out_edges(node, data=True):
                    if data.get("kind") in _OUTGOING_HOP_KINDS and target not in visited:
                        next_frontier.add(target)
                for source, _, data in self.graph.in_edges(node, data=True):
                    if data.get("kind") in _INCOMING_HOP_KINDS and source not in visited:
                        next_frontier.add(source)
            if not next_frontier:
                break
            visited |= next_frontier
            frontier = next_frontier
        return self.graph.subgraph(visited).copy()

    def build_context_cache(self, seeds: list[str], max_hops: int = 2) -> str:
        subgraph = self.k_hop_subgraph(seeds, max_hops=max_hops)
        blocks: list[str] = []
        for node_id, data in subgraph.nodes(data=True):
            stub = data.get("stub")
            if stub:
                blocks.append(f"// Symbol: {node_id}\n{stub}\n")
        header = f"=== COMPRESSED CONTEXT CACHE ({len(blocks)} SYMBOLS, k={max_hops}) ===\n"
        return header + "\n".join(blocks)

    def stats(self) -> dict:
        node_counts: dict[str, int] = {}
        for _, data in self.graph.nodes(data=True):
            node_type = data.get("node_type", "Unknown")
            node_counts[node_type] = node_counts.get(node_type, 0) + 1
        edge_counts: dict[str, int] = {}
        for _, _, data in self.graph.edges(data=True):
            kind = data.get("kind", "Unknown")
            edge_counts[kind] = edge_counts.get(kind, 0) + 1
        return {
            "nodes": self.graph.number_of_nodes(),
            "edges": self.graph.number_of_edges(),
            "nodes_by_type": node_counts,
            "edges_by_kind": edge_counts,
        }

    # -- invalidation / persistence -----------------------------------------------------------------

    def invalidate_file(self, relpath: str) -> None:
        if relpath not in self.graph:
            return
        contained = [
            target
            for _, target, data in self.graph.out_edges(relpath, data=True)
            if data.get("kind") == "CONTAINS"
        ]
        for node in contained:
            self.graph.remove_node(node)
        self.graph.remove_node(relpath)

    def to_json(self, path: Path) -> None:
        data = json_graph.node_link_data(self.graph, edges="edges")
        Path(path).write_text(json.dumps(data, indent=2), encoding="utf-8")

    @classmethod
    def from_json(cls, path: Path) -> "KnowledgeEngine":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        engine = cls()
        engine.graph = json_graph.node_link_graph(data, directed=True, edges="edges")
        return engine
