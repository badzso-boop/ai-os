"""Unit tests for static UI graph builder in ai_os.ui.static_graph."""

from __future__ import annotations

import pytest
from pathlib import Path

from ai_os.ui.static_graph import build_static_ui_graph
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
)


def test_build_static_ui_graph_html_elements(tmp_path: Path) -> None:
    """Test HTML parsing extracts interactive elements, selectors, and parent hierarchy."""
    html_content = """<!DOCTYPE html>
<html>
<body>
  <div id="container" class="main flex">
    <button id="save-btn" class="btn primary" aria-label="Save Document">Save</button>
    <a href="/link" id="nav-link">Go Link</a>
    <input id="username" type="text" name="user" data-testid="user-input" />
    <form id="login-form">
      <button type="submit" id="submit-btn">Submit</button>
    </form>
    <div role="button" tabindex="0" id="custom-btn">Custom</div>
  </div>
</body>
</html>
"""
    (tmp_path / "index.html").write_text(html_content, encoding="utf-8")

    graph = build_static_ui_graph(tmp_path)
    assert isinstance(graph, UIGraph)

    # Check ElementNodes
    elements = [n for n in graph.nodes.values() if isinstance(n, ElementNode)]
    assert len(elements) >= 5

    save_btn = graph.get_node("save-btn")
    assert save_btn is not None
    assert save_btn.tag_name == "button"
    assert save_btn.is_interactive is True
    assert save_btn.text_content == "Save"
    assert "#save-btn" in save_btn.selectors
    assert ".btn" in save_btn.selectors
    assert ".primary" in save_btn.selectors
    assert "[aria-label='Save Document']" in save_btn.selectors

    nav_link = graph.get_node("nav-link")
    assert nav_link is not None
    assert nav_link.tag_name == "a"
    assert nav_link.is_interactive is True
    assert nav_link.attributes["href"] == "/link"

    username_input = graph.get_node("username")
    assert username_input is not None
    assert username_input.tag_name == "input"
    assert username_input.is_interactive is True
    assert "[data-testid='user-input']" in username_input.selectors

    login_form = graph.get_node("login-form")
    assert login_form is not None
    assert login_form.tag_name == "form"
    assert login_form.is_interactive is True

    submit_btn = graph.get_node("submit-btn")
    assert submit_btn is not None
    assert submit_btn.parent_id == "login-form"

    custom_btn = graph.get_node("custom-btn")
    assert custom_btn is not None
    assert custom_btn.is_interactive is True

    # Check SelectorNodes & MATCHES edges
    matches_edges = graph.get_edges(kind=MATCHES)
    assert len(matches_edges) > 0
    save_btn_matches = [e for e in matches_edges if e.target_id == "save-btn"]
    assert len(save_btn_matches) >= 3


def test_build_static_ui_graph_css_rules(tmp_path: Path) -> None:
    """Test CSS parsing extracts behavior properties and matches elements via STYLED_BY edges."""
    html_content = """<!DOCTYPE html>
<html>
<body>
  <button id="save-btn" class="btn primary">Save</button>
  <button id="cancel-btn" class="btn">Cancel</button>
</body>
</html>
"""
    css_content = """
#save-btn {
  display: inline-block;
  cursor: pointer;
  color: red;
}
.btn.primary {
  opacity: 0.8;
  pointer-events: none;
}
button {
  position: relative;
  z-index: 10;
  margin: 10px;
}
"""
    (tmp_path / "index.html").write_text(html_content, encoding="utf-8")
    (tmp_path / "styles.css").write_text(css_content, encoding="utf-8")

    graph = build_static_ui_graph(tmp_path)

    style_nodes = [n for n in graph.nodes.values() if isinstance(n, StyleRuleNode)]
    assert len(style_nodes) == 3

    # Ensure non-behavior properties (color, margin) are excluded
    for style in style_nodes:
        assert "color" not in style.declarations
        assert "margin" not in style.declarations

    save_btn_styles = graph.get_edges(source_id="save-btn", kind=STYLED_BY)
    assert len(save_btn_styles) == 3

    cancel_btn_styles = graph.get_edges(source_id="cancel-btn", kind=STYLED_BY)
    assert len(cancel_btn_styles) == 1  # Only matches button selector


def test_build_static_ui_graph_js_handlers(tmp_path: Path) -> None:
    """Test JS event handlers extraction (inline, addEventListener) and FQN linking."""
    html_content = """<!DOCTYPE html>
<html>
<body>
  <button id="save-btn" onclick="handleSave()">Save</button>
  <input id="username-input" type="text" />
</body>
</html>
"""
    js_content = """
function handleSave() {
  console.log('saved');
}

function onUserChange(e) {
  console.log(e.target.value);
}

document.getElementById('username-input').addEventListener('change', onUserChange);
"""
    (tmp_path / "index.html").write_text(html_content, encoding="utf-8")
    (tmp_path / "app.js").write_text(js_content, encoding="utf-8")

    graph = build_static_ui_graph(tmp_path)

    handler_nodes = [n for n in graph.nodes.values() if isinstance(n, HandlerNode)]
    assert len(handler_nodes) >= 2

    # Check inline handler on save-btn
    save_btn_handlers = graph.get_edges(source_id="save-btn", kind=HANDLED_BY)
    assert len(save_btn_handlers) == 1
    h_save = graph.get_node(save_btn_handlers[0].target_id)
    assert isinstance(h_save, HandlerNode)
    assert h_save.event_type == "click"
    assert h_save.handler_name == "handleSave"
    assert h_save.js_symbol_fqn == "app.js::handleSave"

    # Check IMPLEMENTED_BY edge
    impl_edges = graph.get_edges(source_id=h_save.id, kind=IMPLEMENTED_BY)
    assert len(impl_edges) == 1
    assert impl_edges[0].target_id == "app.js::handleSave"

    # Check addEventListener handler on username-input
    username_handlers = graph.get_edges(source_id="username-input", kind=HANDLED_BY)
    assert len(username_handlers) == 1
    h_user = graph.get_node(username_handlers[0].target_id)
    assert isinstance(h_user, HandlerNode)
    assert h_user.event_type == "change"
    assert h_user.handler_name == "onUserChange"
    assert h_user.js_symbol_fqn == "app.js::onUserChange"


def test_build_static_ui_graph_empty_project(tmp_path: Path) -> None:
    """Test static graph builder on an empty directory returns an empty UIGraph."""
    graph = build_static_ui_graph(tmp_path)
    assert isinstance(graph, UIGraph)
    assert len(graph.nodes) == 0
    assert len(graph.edges) == 0