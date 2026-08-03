from ai_os.analyzer.call_graph_builder import CallGraphBuilder
from ai_os.knowledge.graph_engine import KnowledgeEngine


def _build_engine(fixture_root):
    result = CallGraphBuilder().scan(fixture_root)
    engine = KnowledgeEngine()
    engine.build_from_scan(result)
    return engine


def test_stats_counts_nodes_and_edges_by_type(fixture_root):
    engine = _build_engine(fixture_root)
    stats = engine.stats()
    assert stats["nodes"] > 0
    assert stats["edges"] > 0
    assert stats["nodes_by_type"]["FileNode"] == 11
    assert "CONTAINS" in stats["edges_by_kind"]
    assert "CALLS" in stats["edges_by_kind"]
    assert "IMPORTS" in stats["edges_by_kind"]
    assert "EXTENDS" in stats["edges_by_kind"]


def test_k_hop_subgraph_pulls_in_incoming_calls_and_outgoing_imports(fixture_root):
    engine = _build_engine(fixture_root)
    subgraph = engine.k_hop_subgraph(["src/com/example/Foo.java"], max_hops=2)
    node_ids = set(subgraph.nodes)
    # Outgoing IMPORTS from Foo.java's file should pull in Helper.java and its method.
    assert "src/com/example/other/Helper.java" in node_ids
    assert "src/com/example/other/Helper.java::Helper.compute" in node_ids
    # Outgoing EXTENDS/IMPLEMENTS from Foo should pull in Base and Bar.
    assert "src/com/example/Base.java::Base" in node_ids
    assert "src/com/example/Bar.java::Bar" in node_ids


def test_k_hop_subgraph_respects_max_hops_zero(fixture_root):
    engine = _build_engine(fixture_root)
    subgraph = engine.k_hop_subgraph(["src/com/example/Foo.java"], max_hops=0)
    assert set(subgraph.nodes) == {"src/com/example/Foo.java"}


def test_build_context_cache_includes_stubs(fixture_root):
    engine = _build_engine(fixture_root)
    cache = engine.build_context_cache(["src/com/example/Foo.java"], max_hops=2)
    assert "COMPRESSED CONTEXT CACHE" in cache
    assert "Foo.getX" in cache
    assert "Helper.compute" in cache


def test_invalidate_file_removes_file_and_its_symbols(fixture_root):
    engine = _build_engine(fixture_root)
    assert "src/com/example/Foo.java::Foo.getX" in engine.graph
    engine.invalidate_file("src/com/example/Foo.java")
    assert "src/com/example/Foo.java" not in engine.graph
    assert "src/com/example/Foo.java::Foo.getX" not in engine.graph
    assert "src/com/example/Foo.java::Foo" not in engine.graph


def test_to_json_and_from_json_round_trip(tmp_path, fixture_root):
    engine = _build_engine(fixture_root)
    out_path = tmp_path / "graph.json"
    engine.to_json(out_path)

    reloaded = KnowledgeEngine.from_json(out_path)
    assert reloaded.stats() == engine.stats()
    assert "src/com/example/Foo.java::Foo.getX" in reloaded.graph
