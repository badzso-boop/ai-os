"""Unit tests for static UI bug detectors in ai_os.ui.detectors."""

from __future__ import annotations

import unittest

from ai_os.ui.detectors import (
    Suspicion,
    dead_handler,
    duplicate_id,
    no_accessible_name,
    no_handler,
    run_detectors,
    submit_outside_form,
)
from ai_os.ui.ui_graph import (
    HANDLED_BY,
    ElementNode,
    HandlerNode,
    UIGraph,
)


class TestSuspicionDataclass(unittest.TestCase):
    """Test Suspicion dataclass fields and serialization."""

    def test_suspicion_serialization(self):
        s = Suspicion(
            element_ref="btn_1",
            kind="no_handler",
            evidence="Interactive button missing handler.",
            weight=0.8,
        )
        self.assertEqual(s.element_ref, "btn_1")
        self.assertEqual(s.kind, "no_handler")
        self.assertEqual(s.evidence, "Interactive button missing handler.")
        self.assertEqual(s.weight, 0.8)

        d = s.to_dict()
        recreated = Suspicion.from_dict(d)
        self.assertEqual(s, recreated)


class TestNoHandlerDetector(unittest.TestCase):
    """Test no_handler detector logic."""

    def test_detects_unhandled_button(self):
        graph = UIGraph()
        btn = ElementNode(id="btn_save", tag_name="button", is_interactive=True, text_content="Save")
        graph.add_element_node(btn)

        suspicions = no_handler(graph)
        self.assertEqual(len(suspicions), 1)
        self.assertEqual(suspicions[0].element_ref, "btn_save")
        self.assertEqual(suspicions[0].kind, "no_handler")

    def test_ignores_handled_button(self):
        graph = UIGraph()
        btn = ElementNode(id="btn_save", tag_name="button", is_interactive=True, text_content="Save")
        graph.add_element_node(btn)
        h = HandlerNode(id="h_save", handler_name="onSave", js_symbol_fqn="src.Save.onSave")
        graph.add_handler_node(h)
        graph.add_edge("btn_save", "h_save", kind=HANDLED_BY)

        suspicions = no_handler(graph)
        self.assertEqual(len(suspicions), 0)

    def test_ignores_anchor_with_href(self):
        graph = UIGraph()
        link = ElementNode(
            id="nav_link",
            tag_name="a",
            attributes={"href": "/dashboard"},
            text_content="Dashboard",
        )
        graph.add_element_node(link)

        suspicions = no_handler(graph)
        self.assertEqual(len(suspicions), 0)

    def test_ignores_non_interactive_div(self):
        graph = UIGraph()
        div = ElementNode(id="header_div", tag_name="div", is_interactive=False, text_content="Header")
        graph.add_element_node(div)

        suspicions = no_handler(graph)
        self.assertEqual(len(suspicions), 0)


class TestDeadHandlerDetector(unittest.TestCase):
    """Test dead_handler detector logic."""

    def test_detects_empty_handler_node(self):
        graph = UIGraph()
        btn = ElementNode(id="btn_cancel", tag_name="button", text_content="Cancel")
        graph.add_element_node(btn)
        h = HandlerNode(id="h_empty", handler_name="noop", js_symbol_fqn="")
        graph.add_handler_node(h)
        graph.add_edge("btn_cancel", "h_empty", kind=HANDLED_BY)

        suspicions = dead_handler(graph)
        self.assertTrue(any(s.kind == "dead_handler" and s.element_ref == "btn_cancel" for s in suspicions))

    def test_detects_dead_inline_attribute(self):
        graph = UIGraph()
        btn = ElementNode(
            id="btn_noop",
            tag_name="button",
            attributes={"onclick": "javascript:void(0)"},
            text_content="Click",
        )
        graph.add_element_node(btn)

        suspicions = dead_handler(graph)
        self.assertTrue(any(s.kind == "dead_handler" and s.element_ref == "btn_noop" for s in suspicions))

    def test_detects_unattached_handler_node(self):
        graph = UIGraph()
        h = HandlerNode(id="orphan_h", handler_name="onUnused", js_symbol_fqn="src.Unused.onUnused")
        graph.add_handler_node(h)

        suspicions = dead_handler(graph)
        self.assertTrue(any(s.kind == "dead_handler" and s.element_ref == "orphan_h" for s in suspicions))

    def test_ignores_valid_handler(self):
        graph = UIGraph()
        btn = ElementNode(id="btn_submit", tag_name="button", text_content="Submit")
        graph.add_element_node(btn)
        h = HandlerNode(id="h_valid", handler_name="handleSubmit", js_symbol_fqn="src.Form.handleSubmit")
        graph.add_handler_node(h)
        graph.add_edge("btn_submit", "h_valid", kind=HANDLED_BY)

        suspicions = dead_handler(graph)
        self.assertEqual(len(suspicions), 0)


class TestDuplicateIdDetector(unittest.TestCase):
    """Test duplicate_id detector logic."""

    def test_detects_duplicate_html_ids(self):
        graph = UIGraph()
        elem1 = ElementNode(id="elem_1", tag_name="input", attributes={"id": "username_field"})
        elem2 = ElementNode(id="elem_2", tag_name="input", attributes={"id": "username_field"})
        graph.add_element_node(elem1)
        graph.add_element_node(elem2)

        suspicions = duplicate_id(graph)
        self.assertEqual(len(suspicions), 2)
        refs = {s.element_ref for s in suspicions}
        self.assertEqual(refs, {"elem_1", "elem_2"})
        self.assertTrue(all(s.kind == "duplicate_id" for s in suspicions))

    def test_ignores_unique_ids(self):
        graph = UIGraph()
        elem1 = ElementNode(id="elem_1", tag_name="input", attributes={"id": "user_id"})
        elem2 = ElementNode(id="elem_2", tag_name="input", attributes={"id": "email_id"})
        graph.add_element_node(elem1)
        graph.add_element_node(elem2)

        suspicions = duplicate_id(graph)
        self.assertEqual(len(suspicions), 0)


class TestSubmitOutsideFormDetector(unittest.TestCase):
    """Test submit_outside_form detector logic."""

    def test_detects_submit_outside_form(self):
        graph = UIGraph()
        btn = ElementNode(id="standalone_submit", tag_name="button", attributes={"type": "submit"}, text_content="Send")
        graph.add_element_node(btn)

        suspicions = submit_outside_form(graph)
        self.assertEqual(len(suspicions), 1)
        self.assertEqual(suspicions[0].element_ref, "standalone_submit")
        self.assertEqual(suspicions[0].kind, "submit_outside_form")

    def test_ignores_submit_inside_form(self):
        graph = UIGraph()
        form = ElementNode(id="login_form", tag_name="form")
        graph.add_element_node(form)
        btn = ElementNode(
            id="form_submit",
            tag_name="button",
            attributes={"type": "submit"},
            text_content="Log In",
            parent_id="login_form",
        )
        graph.add_element_node(btn)

        suspicions = submit_outside_form(graph)
        self.assertEqual(len(suspicions), 0)

    def test_ignores_submit_with_form_attribute(self):
        graph = UIGraph()
        btn = ElementNode(
            id="external_submit",
            tag_name="button",
            attributes={"type": "submit", "form": "my_form"},
            text_content="Submit",
        )
        graph.add_element_node(btn)

        suspicions = submit_outside_form(graph)
        self.assertEqual(len(suspicions), 0)


class TestNoAccessibleNameDetector(unittest.TestCase):
    """Test no_accessible_name detector logic."""

    def test_detects_missing_accessible_name(self):
        graph = UIGraph()
        btn = ElementNode(id="icon_only_btn", tag_name="button", is_interactive=True, text_content="")
        graph.add_element_node(btn)

        suspicions = no_accessible_name(graph)
        self.assertEqual(len(suspicions), 1)
        self.assertEqual(suspicions[0].element_ref, "icon_only_btn")
        self.assertEqual(suspicions[0].kind, "no_accessible_name")

    def test_accepts_text_content(self):
        graph = UIGraph()
        btn = ElementNode(id="text_btn", tag_name="button", is_interactive=True, text_content="OK")
        graph.add_element_node(btn)

        suspicions = no_accessible_name(graph)
        self.assertEqual(len(suspicions), 0)

    def test_accepts_aria_label(self):
        graph = UIGraph()
        btn = ElementNode(
            id="aria_btn",
            tag_name="button",
            is_interactive=True,
            attributes={"aria-label": "Close modal"},
        )
        graph.add_element_node(btn)

        suspicions = no_accessible_name(graph)
        self.assertEqual(len(suspicions), 0)

    def test_accepts_associated_label_element(self):
        graph = UIGraph()
        inp = ElementNode(id="email_field", tag_name="input", attributes={"id": "email_input"})
        graph.add_element_node(inp)
        lbl = ElementNode(
            id="email_label",
            tag_name="label",
            attributes={"for": "email_input"},
            text_content="Email Address",
        )
        graph.add_element_node(lbl)

        suspicions = no_accessible_name(graph)
        self.assertEqual(len(suspicions), 0)


class TestRunDetectors(unittest.TestCase):
    """Test run_detectors aggregate runner and sorting."""

    def test_aggregates_and_sorts_by_weight_descending(self):
        graph = UIGraph()
        # Creates duplicate HTML id (weight 0.9), submit outside form (weight 0.85),
        # no_handler (weight 0.8), no_accessible_name (weight 0.6)
        btn1 = ElementNode(
            id="btn_1",
            tag_name="button",
            is_interactive=True,
            attributes={"id": "dup_btn", "type": "submit"},
            text_content="",
        )
        btn2 = ElementNode(
            id="btn_2",
            tag_name="button",
            is_interactive=True,
            attributes={"id": "dup_btn", "type": "submit"},
            text_content="",
        )
        graph.add_element_node(btn1)
        graph.add_element_node(btn2)

        suspicions = run_detectors(graph)
        self.assertTrue(len(suspicions) > 0)

        # Verify weights are strictly sorted in descending order
        weights = [s.weight for s in suspicions]
        self.assertEqual(weights, sorted(weights, reverse=True))


if __name__ == "__main__":
    unittest.main()