"""Comprehensive unit tests for UIGraph and build_ui_context_cache."""

from __future__ import annotations

import json
import unittest

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
    UIGraphEdge,
    UIGraphNode,
    build_ui_context_cache,
)


class TestTypedNodes(unittest.TestCase):
    def test_element_node(self):
        node = ElementNode(
            id="elem_btn_save",
            tag_name="button",
            selectors=["#btn-save", ".btn-primary"],
            file="src/SaveButton.tsx",
            line=42,
            attributes={"type": "submit"},
            text_content="Mentés",
            is_interactive=True,
        )
        self.assertEqual(node.id, "elem_btn_save")
        self.assertEqual(node.tag_name, "button")
        self.assertEqual(node.selectors, ["#btn-save", ".btn-primary"])
        self.assertEqual(node.node_type, "ElementNode")

        d = node.to_dict()
        recreated = ElementNode.from_dict(d)
        self.assertEqual(node, recreated)

    def test_selector_node(self):
        node = SelectorNode(
            id="sel_btn_save",
            selector="#btn-save",
            selector_type="id",
        )
        self.assertEqual(node.id, "sel_btn_save")
        self.assertEqual(node.selector, "#btn-save")
        self.assertEqual(node.selector_type, "id")
        self.assertEqual(node.node_type, "SelectorNode")

        d = node.to_dict()
        recreated = SelectorNode.from_dict(d)
        self.assertEqual(node, recreated)

    def test_style_rule_node(self):
        node = StyleRuleNode(
            id="style_primary",
            selector=".btn-primary",
            file="src/styles.css",
            line=10,
            declarations={"display": "inline-block", "pointer-events": "none"},
        )
        self.assertEqual(node.id, "style_primary")
        self.assertEqual(node.declarations["pointer-events"], "none")
        self.assertEqual(node.node_type, "StyleRuleNode")

        d = node.to_dict()
        recreated = StyleRuleNode.from_dict(d)
        self.assertEqual(node, recreated)

    def test_handler_node(self):
        node = HandlerNode(
            id="hand_click_save",
            event_type="click",
            handler_name="handleSave",
            js_symbol_fqn="src/SaveButton.handleSave",
            file="src/SaveButton.tsx",
            line=50,
        )
        self.assertEqual(node.id, "hand_click_save")
        self.assertEqual(node.event_type, "click")
        self.assertEqual(node.handler_name, "handleSave")
        self.assertEqual(node.node_type, "HandlerNode")

        d = node.to_dict()
        recreated = HandlerNode.from_dict(d)
        self.assertEqual(node, recreated)


class TestUIGraphNode(unittest.TestCase):
    def test_node_creation_defaults(self):
        node = UIGraphNode(id="btn_submit")
        self.assertEqual(node.id, "btn_submit")
        self.assertEqual(node.node_type, "element")
        self.assertEqual(node.label, "")
        self.assertEqual(node.properties, {})
        self.assertIsNone(node.parent_id)
        self.assertEqual(node.children_ids, [])

    def test_node_to_from_dict(self):
        node = UIGraphNode(
            id="panel_1",
            node_type="container",
            label="Main Panel",
            properties={"width": 100, "visible": True},
            parent_id="root",
            children_ids=["btn_1", "btn_2"],
        )
        data = node.to_dict()
        recreated = UIGraphNode.from_dict(data)
        self.assertEqual(node, recreated)


class TestUIGraphEdge(unittest.TestCase):
    def test_edge_to_from_dict(self):
        edge = UIGraphEdge(
            source_id="screen_1",
            target_id="screen_2",
            edge_type="navigates_to",
            properties={"trigger": "click"},
        )
        data = edge.to_dict()
        recreated = UIGraphEdge.from_dict(data)
        self.assertEqual(edge.source_id, recreated.source_id)
        self.assertEqual(edge.target_id, recreated.target_id)
        self.assertEqual(edge.edge_type, recreated.edge_type)


class TestUIGraphOperations(unittest.TestCase):
    def setUp(self):
        self.graph = UIGraph()

    def test_networkx_graph_backing(self):
        self.assertIsNotNone(self.graph.graph)

    def test_add_and_retrieve_typed_nodes(self):
        elem = ElementNode(id="elem1", tag_name="button", selectors=["#btn1"])
        sel = SelectorNode(id="sel1", selector="#btn1")
        style = StyleRuleNode(id="style1", selector="#btn1", declarations={"color": "blue"})
        handler = HandlerNode(id="hand1", event_type="click", handler_name="onClick")

        self.graph.add_element_node(elem)
        self.graph.add_selector_node(sel)
        self.graph.add_style_rule_node(style)
        self.graph.add_handler_node(handler)

        self.assertTrue(self.graph.has_node("elem1"))
        self.assertTrue(self.graph.has_node("sel1"))
        self.assertTrue(self.graph.has_node("style1"))
        self.assertTrue(self.graph.has_node("hand1"))

        retrieved_elem = self.graph.get_node("elem1")
        self.assertIsInstance(retrieved_elem, ElementNode)
        self.assertEqual(retrieved_elem.tag_name, "button")

        retrieved_style = self.graph.get_node("style1")
        self.assertIsInstance(retrieved_style, StyleRuleNode)
        self.assertEqual(retrieved_style.declarations["color"], "blue")

    def test_add_node_with_parent(self):
        parent = self.graph.add_node("main_view", node_type="view")
        child = self.graph.add_node("sub_button", node_type="button", parent_id="main_view")

        self.assertEqual(child.parent_id, "main_view")
        self.assertIn("sub_button", getattr(parent, "children_ids", []))
        self.assertEqual(self.graph.get_parent("sub_button").id, "main_view")

    def test_add_node_invalid_parent(self):
        with self.assertRaises(ValueError):
            self.graph.add_node("orphan", parent_id="non_existent")

    def test_typed_edges(self):
        elem = self.graph.add_element_node("elem1", tag_name="button")
        sel = self.graph.add_selector_node("sel1", selector="#elem1")
        style = self.graph.add_style_rule_node("style1", selector="#elem1")
        handler = self.graph.add_handler_node("hand1", event_type="click")

        edge_match = self.graph.add_edge("sel1", "elem1", kind=MATCHES)
        edge_style = self.graph.add_edge("elem1", "style1", kind=STYLED_BY)
        edge_handler = self.graph.add_edge("elem1", "hand1", kind=HANDLED_BY)

        self.assertEqual(edge_match.kind, MATCHES)
        self.assertEqual(edge_style.kind, STYLED_BY)
        self.assertEqual(edge_handler.kind, HANDLED_BY)

        matched_styles = self.graph.get_edges(source_id="elem1", kind=STYLED_BY)
        self.assertEqual(len(matched_styles), 1)
        self.assertEqual(matched_styles[0].target_id, "style1")

    def test_node_deletion_and_cleanup(self):
        self.graph.add_element_node("parent")
        self.graph.add_element_node("child", parent_id="parent")
        self.graph.add_edge("parent", "child", kind="CONTAINS")

        self.assertTrue(self.graph.remove_node("parent"))
        self.assertFalse(self.graph.has_node("parent"))
        self.assertIsNone(self.graph.get_node("child").parent_id)

    def test_ancestors_and_descendants(self):
        self.graph.add_node("root")
        self.graph.add_node("level1", parent_id="root")
        self.graph.add_node("level2", parent_id="level1")

        ancestors = self.graph.get_ancestors("level2")
        self.assertEqual([a.id for a in ancestors], ["level1", "root"])

        descendants = self.graph.get_descendants("root")
        self.assertEqual([d.id for d in descendants], ["level1", "level2"])

    def test_find_nodes(self):
        self.graph.add_node("btn1", node_type="button", properties={"active": True})
        self.graph.add_node("btn2", node_type="button", properties={"active": False})
        self.graph.add_node("txt1", node_type="text", properties={"active": True})

        buttons = self.graph.find_nodes_by_type("button")
        self.assertEqual({b.id for b in buttons}, {"btn1", "btn2"})

        active_nodes = self.graph.find_nodes_by_property("active", True)
        self.assertEqual({n.id for n in active_nodes}, {"btn1", "txt1"})

    def test_find_path(self):
        self.graph.add_node("page1")
        self.graph.add_node("page2")
        self.graph.add_node("page3")
        self.graph.add_node("page4")

        self.graph.add_edge("page1", "page2", kind="link")
        self.graph.add_edge("page2", "page3", kind="link")
        self.graph.add_edge("page1", "page4", kind="other")

        path = self.graph.find_path("page1", "page3", kind="link")
        self.assertEqual(path, ["page1", "page2", "page3"])

    def test_k_hop_subgraph(self):
        self.graph.add_element_node("root")
        self.graph.add_element_node("hop1", parent_id="root")
        self.graph.add_element_node("hop2", parent_id="hop1")
        self.graph.add_element_node("hop3", parent_id="hop2")

        self.graph.add_edge("root", "hop1", kind="REL")
        self.graph.add_edge("hop1", "hop2", kind="REL")
        self.graph.add_edge("hop2", "hop3", kind="REL")

        sub = self.graph.k_hop_subgraph(["root"], max_hops=2)
        self.assertTrue(sub.has_node("root"))
        self.assertTrue(sub.has_node("hop1"))
        self.assertTrue(sub.has_node("hop2"))
        self.assertFalse(sub.has_node("hop3"))

    def test_serialization(self):
        elem = ElementNode(id="btn1", tag_name="button", selectors=["#btn1"])
        style = StyleRuleNode(id="s1", selector="#btn1", declarations={"display": "none"})
        self.graph.add_element_node(elem)
        self.graph.add_style_rule_node(style)
        self.graph.add_edge("btn1", "s1", kind=STYLED_BY)

        data = self.graph.to_dict()
        recreated = UIGraph.from_dict(data)

        self.assertTrue(recreated.has_node("btn1"))
        self.assertTrue(recreated.has_node("s1"))
        self.assertIsInstance(recreated.get_node("btn1"), ElementNode)
        edges = recreated.get_edges(source_id="btn1", kind=STYLED_BY)
        self.assertEqual(len(edges), 1)
        self.assertEqual(edges[0].target_id, "s1")

        json_str = self.graph.to_json(indent=2)
        from_json_graph = UIGraph.from_json(json_str)
        self.assertTrue(from_json_graph.has_node("btn1"))


class TestBuildUIContextCache(unittest.TestCase):
    def test_build_context_cache_full(self):
        graph = UIGraph()
        elem = ElementNode(
            id="btn_save",
            tag_name="button",
            selectors=["#save-btn", ".btn-primary"],
            file="src/Save.tsx",
            line=30,
            text_content="Mentés",
            attributes={"type": "submit", "disabled": False},
        )
        style = StyleRuleNode(
            id="style_btn",
            selector=".btn-primary",
            file="src/styles.css",
            line=15,
            declarations={"pointer-events": "none", "opacity": "0.5"},
        )
        handler = HandlerNode(
            id="hand_save",
            event_type="click",
            handler_name="onSave",
            js_symbol_fqn="src/Save.onSave",
            file="src/Save.tsx",
            line=45,
        )

        graph.add_element_node(elem)
        graph.add_style_rule_node(style)
        graph.add_handler_node(handler)

        graph.add_edge("btn_save", "style_btn", kind=STYLED_BY)
        graph.add_edge("btn_save", "hand_save", kind=HANDLED_BY)

        cache = build_ui_context_cache(graph)

        self.assertIn("=== COMPRESSED UI CONTEXT CACHE (1 SYMBOLS, focus=None) ===", cache)
        self.assertIn("// Element: btn_save (<button>)", cache)
        self.assertIn('Text: "Mentés"', cache)
        self.assertIn("Selectors: #save-btn, .btn-primary", cache)
        self.assertIn("Matched Styles:", cache)
        self.assertIn("pointer-events: none", cache)
        self.assertIn("Handlers:", cache)
        self.assertIn("hand_save [click]: onSave (FQN: src/Save.onSave)", cache)

    def test_build_context_cache_focus_selector(self):
        graph = UIGraph()
        elem1 = ElementNode(id="btn1", tag_name="button", selectors=["#btn1"])
        elem2 = ElementNode(id="btn2", tag_name="button", selectors=["#btn2"])

        graph.add_element_node(elem1)
        graph.add_element_node(elem2)

        cache = build_ui_context_cache(graph, focus_selector="#btn1")
        self.assertIn("focus=#btn1", cache)
        self.assertIn("btn1", cache)
        self.assertNotIn("btn2", cache)

    def test_build_context_cache_empty(self):
        graph = UIGraph()
        cache = build_ui_context_cache(graph)
        self.assertIn("(empty)", cache)


if __name__ == "__main__":
    unittest.main()