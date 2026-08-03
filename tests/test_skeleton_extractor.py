from ai_os.analyzer.languages import LANGUAGES
from ai_os.analyzer.tree_sitter_engine import TreeSitterEngine
from ai_os.knowledge.skeleton_extractor import extract_skeleton


def _first_symbol(tmp_path, filename, content, name):
    path = tmp_path / filename
    path.write_text(content, encoding="utf-8")
    engine = TreeSitterEngine()
    parsed = engine.parse_file(path, tmp_path)
    symbols = engine.extract_symbols(parsed)
    symbol = next(s for s in symbols if s.name == name)
    return symbol, parsed.source


def test_java_method_body_is_stripped(tmp_path):
    symbol, source = _first_symbol(
        tmp_path,
        "Foo.java",
        "public class Foo {\n    public int getX() {\n        return 42;\n    }\n}\n",
        "getX",
    )
    stub = extract_skeleton(symbol, source, LANGUAGES["java"])
    assert "SKELETON STUB FOR" in stub
    assert "public int getX()" in stub
    assert "42" not in stub


def test_python_function_body_is_stripped(tmp_path):
    symbol, source = _first_symbol(
        tmp_path, "foo.py", "def helper(x):\n    return x * 2\n", "helper"
    )
    stub = extract_skeleton(symbol, source, LANGUAGES["python"])
    assert "def helper(x):" in stub
    assert "* 2" not in stub


def test_sql_symbol_passes_through_unchanged(tmp_path):
    symbol, source = _first_symbol(
        tmp_path, "init.sql", "CREATE TABLE users (id SERIAL PRIMARY KEY);\n", "users"
    )
    stub = extract_skeleton(symbol, source, LANGUAGES["sql"])
    assert "CREATE TABLE users" in stub
