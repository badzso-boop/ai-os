import json

from ai_os.analyzer.call_graph_builder import CallGraphBuilder


def _unresolved_by_specifier(root):
    result = CallGraphBuilder().scan(root)
    return {e.raw_specifier: e for e in result.import_edges if not e.resolved}


def test_js_bare_import_classified_external_when_declared_in_package_json(tmp_path):
    (tmp_path / "package.json").write_text(
        json.dumps({"dependencies": {"react": "^18.0.0"}}), encoding="utf-8"
    )
    (tmp_path / "app.js").write_text(
        "import React from 'react';\n"
        "import Foo from 'totally-made-up-package';\n"
        "import path from 'path';\n"
        "import Missing from './missing';\n",
        encoding="utf-8",
    )
    unresolved = _unresolved_by_specifier(tmp_path)

    assert unresolved["react"].external is True
    assert unresolved["path"].external is True  # Node builtin, no manifest entry needed
    assert unresolved["totally-made-up-package"].external is False
    assert unresolved["./missing"].external is False


def test_java_unresolved_import_is_always_classified_external(tmp_path):
    (tmp_path / "Foo.java").write_text(
        "package com.example;\n"
        "import org.springframework.stereotype.Service;\n"
        "public class Foo {}\n",
        encoding="utf-8",
    )
    unresolved = _unresolved_by_specifier(tmp_path)
    assert unresolved["import org.springframework.stereotype.Service;"].external is True


def test_python_stdlib_import_classified_external_others_are_not(tmp_path):
    (tmp_path / "foo.py").write_text("import os\nimport totally_made_up_module\n", encoding="utf-8")
    unresolved = _unresolved_by_specifier(tmp_path)
    assert unresolved["import os"].external is True
    assert unresolved["import totally_made_up_module"].external is False
