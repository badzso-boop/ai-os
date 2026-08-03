import json

from click.testing import CliRunner

from ai_os.cli import main

_NOISE_PREFIXES = ("node_modules/", "target/", "build/", "venv/", "__pycache__/", ".git/")


def test_project_add_list_and_scan_json(isolated_home, fixture_root):
    runner = CliRunner()

    add_result = runner.invoke(main, ["project", "add", "sample", str(fixture_root)])
    assert add_result.exit_code == 0, add_result.output

    list_result = runner.invoke(main, ["project", "list"])
    assert list_result.exit_code == 0
    assert "sample" in list_result.output

    scan_result = runner.invoke(main, ["scan", "sample", "--json"])
    assert scan_result.exit_code == 0, scan_result.output
    summary = json.loads(scan_result.output)
    assert summary["files_total"] > 0
    assert summary["files_other"] == 0
    assert summary["graph"]["nodes"] > 0
    assert summary["call_edges_ambiguous"] == 0


def test_scan_writes_graph_json_without_noise(isolated_home, fixture_root, tmp_path):
    runner = CliRunner()
    out_path = tmp_path / "graph.json"

    result = runner.invoke(main, ["scan", str(fixture_root), "--out", str(out_path)])
    assert result.exit_code == 0, result.output

    graph_data = json.loads(out_path.read_text(encoding="utf-8"))
    node_ids = [node["id"] for node in graph_data["nodes"]]
    assert node_ids, "expected the written graph to contain nodes"
    for node_id in node_ids:
        assert not str(node_id).startswith(_NOISE_PREFIXES)


def test_scan_rejects_unregistered_and_nonexistent_path(isolated_home, tmp_path):
    runner = CliRunner()
    result = runner.invoke(main, ["scan", str(tmp_path / "does-not-exist")])
    assert result.exit_code != 0


def test_scan_skeleton_debug_option(isolated_home, fixture_root):
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "scan",
            str(fixture_root),
            "--skeleton",
            "src/com/example/other/Helper.java::Helper.compute",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "SKELETON STUB FOR" in result.output
