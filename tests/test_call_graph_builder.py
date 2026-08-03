from ai_os.analyzer.call_graph_builder import CallGraphBuilder

_NOISE_PREFIXES = ("node_modules/", "target/", "build/", "venv/", "__pycache__/", ".git/")


def _scan(fixture_root):
    return CallGraphBuilder().scan(fixture_root)


def test_noise_directories_are_never_scanned(fixture_root):
    result = _scan(fixture_root)
    relpaths = [fr.parsed.relpath for fr in result.files]
    assert relpaths, "expected at least some files to be scanned"
    for relpath in relpaths:
        assert not relpath.startswith(_NOISE_PREFIXES), f"noise file leaked into scan: {relpath}"


def test_expected_files_scanned_by_language(fixture_root):
    result = _scan(fixture_root)
    assert result.file_count_by_language == {
        "java": 4,
        "javascript": 2,
        "html": 1,
        "css": 2,
        "sql": 1,
        "python": 1,
    }
    assert result.other_file_count == 0


def test_import_edges_resolved_across_languages(fixture_root):
    result = _scan(fixture_root)
    resolved = {
        (e.source_relpath, e.target_relpath) for e in result.import_edges if e.resolved
    }
    assert ("src/com/example/Foo.java", "src/com/example/other/Helper.java") in resolved
    assert ("web/js/app.js", "web/js/helper.js") in resolved
    assert ("web/index.html", "web/js/app.js") in resolved
    assert ("web/index.html", "web/css/style.css") in resolved
    assert ("web/css/style.css", "web/css/base.css") in resolved


def test_call_edges_resolved(fixture_root):
    result = _scan(fixture_root)
    calls = {(e.caller_fqn, tuple(e.callee_fqns)) for e in result.call_edges}
    assert (
        "src/com/example/Foo.java::Foo.getX",
        ("src/com/example/other/Helper.java::Helper.compute",),
    ) in calls
    assert ("web/js/app.js::main", ("web/js/helper.js::helper",)) in calls
    assert all(not e.ambiguous for e in result.call_edges)


def test_extends_and_implements_edges(fixture_root):
    result = _scan(fixture_root)
    edges = {(e.source_fqn, tuple(e.target_fqns), e.via) for e in result.extends_edges}
    assert (
        "src/com/example/Foo.java::Foo",
        ("src/com/example/Base.java::Base",),
        "extends",
    ) in edges
    assert (
        "src/com/example/Foo.java::Foo",
        ("src/com/example/Bar.java::Bar",),
        "implements",
    ) in edges


def test_languages_filter(fixture_root):
    result = CallGraphBuilder().scan(fixture_root, languages={"java"})
    assert set(result.file_count_by_language) == {"java"}


def test_extra_excluded_dirs(fixture_root):
    result = CallGraphBuilder().scan(fixture_root, extra_excluded_dirs={"web"})
    relpaths = [fr.parsed.relpath for fr in result.files]
    assert not any(r.startswith("web/") for r in relpaths)
