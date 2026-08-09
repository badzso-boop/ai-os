"""Unit tests for UI bug report matcher and diagnosis assembler in ai_os.ui.report."""

from __future__ import annotations

import unittest

from ai_os.ui.detectors import Suspicion
from ai_os.ui.report import (
    ReportMatch,
    assemble_ui_diagnosis,
    match_bug_report,
)
from ai_os.ui.ui_graph import (
    HANDLED_BY,
    ElementNode,
    HandlerNode,
    SelectorNode,
    StyleRuleNode,
    UIGraph,
)


class TestReportMatchDataclass(unittest.TestCase):
    """Test ReportMatch dataclass fields and serialization."""

    def test_report_match_serialization(self):
        match = ReportMatch(
            target_element_id="btn_save",
            score=1.5,
            matched_terms=["save", "button"],
            matched_selectors=["#btn_save"],
            scores_by_element={"btn_save": 1.5, "input_name": 0.2},
        )
        self.assertEqual(match.target_element_id, "btn_save")
        self.assertEqual(match.score, 1.5)
        self.assertIn("save", match.matched_terms)
        self.assertEqual(match.matched_selectors, ["#btn_save"])

        serialized = match.to_dict()
        self.assertEqual(serialized["target_element_id"], "btn_save")
        self.assertEqual(serialized["score"], 1.5)

        recreated = ReportMatch.from_dict(serialized)
        self.assertEqual(recreated.target_element_id, "btn_save")
        self.assertEqual(recreated.score, 1.5)
        self.assertEqual(recreated.matched_terms, ["save", "button"])
        self.assertEqual(recreated.matched_selectors, ["#btn_save"])


class TestMatchBugReport(unittest.TestCase):
    """Test match_bug_report fuzzy matching functionality."""

    def test_empty_inputs(self):
        graph = UIGraph()
        match = match_bug_report(graph, "")
        self.assertIsNone(match.target_element_id)
        self.assertEqual(match.score, 0.0)

        btn = ElementNode(id="btn_save", tag_name="button", text_content="Save")
        graph.add_element_node(btn)
        match_empty_str = match_bug_report(graph, "   ")
        self.assertIsNone(match_empty_str.target_element_id)
        self.assertEqual(match_empty_str.score, 0.0)

    def test_exact_id_selector_match(self):
        graph = UIGraph()
        btn1 = ElementNode(
            id="submit-btn",
            tag_name="button",
            selectors=["#submit-btn", ".btn-primary"],
            text_content="Submit",
        )
        btn2 = ElementNode(
            id="cancel-btn",
            tag_name="button",
            selectors=["#cancel-btn"],
            text_content="Cancel",
        )
        graph.add_element_node(btn1)
        graph.add_element_node(btn2)

        match = match_bug_report(graph, "The #submit-btn is not working")
        self.assertEqual(match.target_element_id, "submit-btn")
        self.assertGreater(match.score, 0.8)
        self.assertIn("#submit-btn", match.matched_selectors)

    def test_hungarian_ui_synonyms_and_text_match(self):
        graph = UIGraph()
        btn = ElementNode(
            id="btn_mentes",
            tag_name="button",
            selectors=["#btn_mentes"],
            text_content="Mentés",
            file="src/Form.jsx",
            line=12,
        )
        graph.add_element_node(btn)

        match = match_bug_report(graph, "a Mentés gomb nem működik")
        self.assertEqual(match.target_element_id, "btn_mentes")
        self.assertGreater(match.score, 0.5)
        self.assertIn("Mentés", match.matched_terms)

    def test_attribute_matching(self):
        graph = UIGraph()
        inp = ElementNode(
            id="search_box",
            tag_name="input",
            attributes={"placeholder": "Search items...", "name": "query"},
        )
        graph.add_element_node(inp)

        match = match_bug_report(graph, "the search box placeholder fails")
        self.assertEqual(match.target_element_id, "search_box")
        self.assertGreater(match.score, 0.4)

    def test_unrelated_report_text(self):
        graph = UIGraph()
        btn = ElementNode(id="btn1", tag_name="button", text_content="OK")
        graph.add_element_node(btn)

        match = match_bug_report(graph, "xyz12345 nonmatching random text")
        self.assertIsNone(match.target_element_id)
        self.assertEqual(match.score, 0.0)


class TestAssembleUIDiagnosis(unittest.TestCase):
    """Test assemble_ui_diagnosis report generation and suspicion ranking."""

    def test_assemble_diagnosis_with_target_and_suspicions(self):
        graph = UIGraph()
        btn = ElementNode(
            id="btn_submit",
            tag_name="button",
            selectors=["#btn_submit"],
            text_content="Submit",
            file="src/Form.jsx",
            line=30,
        )
        graph.add_element_node(btn)

        other_elem = ElementNode(
            id="inp_email",
            tag_name="input",
            selectors=["#inp_email"],
            text_content="",
        )
        graph.add_element_node(other_elem)

        s1 = Suspicion(
            element_ref="btn_submit",
            kind="no_handler",
            evidence="Button 'btn_submit' has no bound click handler.",
            weight=0.8,
        )
        s2 = Suspicion(
            element_ref="inp_email",
            kind="no_accessible_name",
            evidence="Input 'inp_email' lacks label.",
            weight=0.5,
        )

        diagnosis = assemble_ui_diagnosis(
            graph,
            suspicions=[s1, s2],
            report_text="Submit button is broken",
        )

        self.assertIn("=== UI DIAGNOSIS REPORT ===", diagnosis)
        self.assertIn('Report Query: "Submit button is broken"', diagnosis)
        self.assertIn("Target Element: btn_submit (<button>) [src/Form.jsx:30]", diagnosis)
        self.assertIn("Ranked Suspicions (2 total):", diagnosis)

        # Target element suspicion (s1) should be ranked higher due to 0-hop proximity boost
        pos_s1 = diagnosis.find("no_handler")
        pos_s2 = diagnosis.find("no_accessible_name")
        self.assertGreater(pos_s2, pos_s1)
        self.assertIn("0 hops - target element", diagnosis)

    def test_assemble_diagnosis_graph_proximity_ranking(self):
        graph = UIGraph()
        btn = ElementNode(id="target_btn", tag_name="button", selectors=["#target_btn"], text_content="Click Me")
        handler = HandlerNode(id="btn_handler", handler_name="onClick", js_symbol_fqn="")
        unconnected_elem = ElementNode(id="remote_div", tag_name="div", text_content="Remote")

        graph.add_element_node(btn)
        graph.add_handler_node(handler)
        graph.add_element_node(unconnected_elem)

        # Connect target_btn -> btn_handler (1 hop)
        graph.add_edge("target_btn", "btn_handler", kind=HANDLED_BY)

        s_target = Suspicion(
            element_ref="target_btn",
            kind="submit_outside_form",
            evidence="Submit outside form.",
            weight=0.6,
        )
        s_handler = Suspicion(
            element_ref="btn_handler",
            kind="dead_handler",
            evidence="Handler is dead/empty.",
            weight=0.7,
        )
        s_remote = Suspicion(
            element_ref="remote_div",
            kind="duplicate_id",
            evidence="Duplicate ID found.",
            weight=0.7,
        )

        diagnosis = assemble_ui_diagnosis(
            graph,
            suspicions=[s_remote, s_handler, s_target],
            report_text="#target_btn",
        )

        self.assertIn("Target Element: target_btn", diagnosis)
        # target_btn has 0 hops (weight 0.6 * 2.0 = 1.2)
        # btn_handler has 1 hop (weight 0.7 * 1.5 = 1.05)
        # remote_div has no path (weight 0.7 * 1.0 = 0.7)
        pos_target = diagnosis.find("submit_outside_form")
        pos_handler = diagnosis.find("dead_handler")
        pos_remote = diagnosis.find("duplicate_id")

        self.assertLess(pos_target, pos_handler)
        self.assertLess(pos_handler, pos_remote)

    def test_assemble_diagnosis_without_report_text(self):
        graph = UIGraph()
        div = ElementNode(id="div_main", tag_name="div")
        graph.add_element_node(div)

        s = Suspicion(
            element_ref="div_main",
            kind="duplicate_id",
            evidence="Duplicate ID 'div_main'.",
            weight=0.7,
        )

        diagnosis = assemble_ui_diagnosis(graph, suspicions=[s])
        self.assertIn("Report Query: None", diagnosis)
        self.assertIn("Target Element: None (General Analysis)", diagnosis)
        self.assertIn("duplicate_id", diagnosis)

    def test_assemble_diagnosis_empty_suspicions(self):
        graph = UIGraph()
        btn = ElementNode(id="btn_ok", tag_name="button", text_content="OK")
        graph.add_element_node(btn)

        diagnosis = assemble_ui_diagnosis(graph, suspicions=[], report_text="OK button")
        self.assertIn("Target Element: btn_ok", diagnosis)
        self.assertIn("(No suspicions detected)", diagnosis)

    def test_assemble_diagnosis_accepts_dict_suspicions(self):
        graph = UIGraph()
        btn = ElementNode(id="btn_cancel", tag_name="button", text_content="Cancel")
        graph.add_element_node(btn)

        dict_suspicion = {
            "element_ref": "btn_cancel",
            "kind": "no_handler",
            "evidence": "Cancel button lacks handler.",
            "weight": 0.8,
        }

        diagnosis = assemble_ui_diagnosis(graph, suspicions=[dict_suspicion], report_text="Cancel button")
        self.assertIn("Target Element: btn_cancel", diagnosis)
        self.assertIn("no_handler", diagnosis)
        self.assertIn("Cancel button lacks handler.", diagnosis)


if __name__ == "__main__":
    unittest.main()