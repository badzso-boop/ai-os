from pathlib import Path

from ai_os.analyzer.tree_sitter_engine import TreeSitterEngine


def _scan_one(tmp_path: Path, filename: str, content: str):
    path = tmp_path / filename
    path.write_text(content, encoding="utf-8")
    engine = TreeSitterEngine()
    parsed = engine.parse_file(path, tmp_path)
    symbols = engine.extract_symbols(parsed)
    return parsed, symbols


def test_python_class_and_function(tmp_path):
    _, symbols = _scan_one(
        tmp_path,
        "foo.py",
        'class Animal:\n    """An animal."""\n\n    def speak(self):\n        """Make a sound."""\n        return helper(self.x)\n\n\ndef helper(x):\n    return x\n',
    )
    by_name = {s.name: s for s in symbols}
    assert by_name["Animal"].kind == "class"
    assert by_name["Animal"].docstring == '"""An animal."""'
    assert by_name["speak"].kind == "method"
    assert by_name["speak"].is_method is True
    assert by_name["speak"].parent_class == "Animal"
    assert by_name["speak"].fqn == "foo.py::Animal.speak"
    assert by_name["helper"].kind == "function"
    assert by_name["helper"].is_method is False
    assert by_name["helper"].params == ["x"]


def test_java_class_method_and_javadoc(tmp_path):
    _, symbols = _scan_one(
        tmp_path,
        "Foo.java",
        "package com.example;\n\n"
        "/**\n * Foo class.\n */\n"
        "public class Foo {\n"
        "    /**\n     * Gets x.\n     */\n"
        "    public int getX() {\n        return 1;\n    }\n"
        "}\n",
    )
    by_name = {s.name: s for s in symbols}
    assert by_name["Foo"].kind == "class"
    assert by_name["Foo"].docstring is not None and "Foo class." in by_name["Foo"].docstring
    assert by_name["getX"].kind == "method"
    assert by_name["getX"].is_method is True
    assert by_name["getX"].return_type == "int"
    assert by_name["getX"].docstring is not None and "Gets x." in by_name["getX"].docstring


def test_java_extends_and_implements(tmp_path):
    _, symbols = _scan_one(
        tmp_path,
        "Foo.java",
        "package p;\npublic class Foo extends Base implements Bar {}\n",
    )
    foo = next(s for s in symbols if s.name == "Foo")
    assert ("Base", "extends") in foo.extends
    assert ("Bar", "implements") in foo.extends


def test_javascript_function_and_method(tmp_path):
    _, symbols = _scan_one(
        tmp_path,
        "app.js",
        "function main() {\n    return helper(1);\n}\n\nclass Widget {\n    render() {\n        return 1;\n    }\n}\n",
    )
    by_name = {s.name: s for s in symbols}
    assert by_name["main"].kind == "function"
    assert by_name["render"].kind == "method"
    assert by_name["render"].parent_class == "Widget"


def test_sql_create_table_and_view(tmp_path):
    _, symbols = _scan_one(
        tmp_path,
        "init.sql",
        "CREATE TABLE users (id SERIAL PRIMARY KEY);\n"
        "CREATE VIEW active_users AS SELECT * FROM users;\n",
    )
    names = {s.name for s in symbols}
    assert names == {"users", "active_users"}
    assert all(s.kind == "type" for s in symbols)


def test_html_and_css_have_no_code_symbols(tmp_path):
    _, html_symbols = _scan_one(tmp_path, "index.html", "<html></html>\n")
    _, css_symbols = _scan_one(tmp_path, "style.css", ".a { color: red; }\n")
    assert html_symbols == []
    assert css_symbols == []


def test_unsupported_extension_returns_none(tmp_path):
    path = tmp_path / "readme.md"
    path.write_text("# hello\n", encoding="utf-8")
    engine = TreeSitterEngine()
    assert engine.parse_file(path, tmp_path) is None
