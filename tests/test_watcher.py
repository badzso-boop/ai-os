"""Tests for `ai_os.knowledge.watcher.ProjectWatcher` (Scope A file-watcher).

Deterministic, no real event timing: drive `start()`/`poll()` directly against
a real tmp project dir, and the blocking `run()` loop via injected
`sleep`/`should_stop`. Real `CallGraphBuilder` + `KnowledgeEngine` (this
project's real-behavior convention) — no LLM, no network.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from ai_os.knowledge.watcher import ProjectWatcher, WatchEvent


def _project(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    root.mkdir()
    (root / "a.py").write_text("def a():\n    return 1\n")
    return root


def test_start_builds_engine_and_baseline(tmp_path):
    root = _project(tmp_path)
    watcher = ProjectWatcher(root)
    engine = watcher.start()
    assert engine.graph.number_of_nodes() > 0
    # baseline snapshot covers the one python file
    assert "a.py" in watcher._snapshot


def test_poll_detects_added_file(tmp_path):
    root = _project(tmp_path)
    watcher = ProjectWatcher(root)
    watcher.start()
    nodes_before = watcher.engine.graph.number_of_nodes()

    (root / "b.py").write_text("def b():\n    return 2\n")
    event = watcher.poll()

    assert event.added == ["b.py"]
    assert not event.modified and not event.removed
    # a real rescan happened: the new symbol node is in the graph
    assert watcher.engine.graph.number_of_nodes() > nodes_before
    assert any("b.py" in n for n in watcher.engine.graph.nodes)


def test_poll_detects_modified_file(tmp_path):
    root = _project(tmp_path)
    watcher = ProjectWatcher(root)
    watcher.start()

    # Rewrite with different content (and bump mtime to be robust on
    # coarse-resolution clocks).
    target = root / "a.py"
    target.write_text("def a():\n    return 1\n\ndef a2():\n    return 2\n")
    os.utime(target, (target.stat().st_atime, target.stat().st_mtime + 5))

    event = watcher.poll()
    assert event.modified == ["a.py"]
    assert any("a.py::a2" in n for n in watcher.engine.graph.nodes)


def test_poll_detects_removed_file(tmp_path):
    root = _project(tmp_path)
    (root / "gone.py").write_text("def gone():\n    return 0\n")
    watcher = ProjectWatcher(root)
    watcher.start()
    assert any("gone.py" in n for n in watcher.engine.graph.nodes)

    (root / "gone.py").unlink()
    event = watcher.poll()

    assert event.removed == ["gone.py"]
    assert not any("gone.py" in n for n in watcher.engine.graph.nodes)


def test_poll_no_change_is_empty_and_no_rebuild(tmp_path):
    root = _project(tmp_path)
    watcher = ProjectWatcher(root)
    watcher.start()
    engine_before = watcher.engine

    event = watcher.poll()
    assert not event
    assert event.total == 0
    # unchanged -> the same engine object is kept (no needless rebuild)
    assert watcher.engine is engine_before


def test_language_filter_ignores_other_files(tmp_path):
    root = _project(tmp_path)
    watcher = ProjectWatcher(root, languages={"python"})
    watcher.start()

    # A JS file appears, but we're only watching python -> no event.
    (root / "ignore.js").write_text("function x() { return 1; }\n")
    assert not watcher.poll()

    # A python file appears -> event.
    (root / "c.py").write_text("def c():\n    return 3\n")
    assert watcher.poll().added == ["c.py"]


def test_excluded_dirs_are_not_watched(tmp_path):
    root = _project(tmp_path)
    watcher = ProjectWatcher(root)
    watcher.start()

    node_modules = root / "node_modules"
    node_modules.mkdir()
    (node_modules / "dep.js").write_text("module.exports = {};\n")
    assert not watcher.poll()  # excluded dir -> ignored


def test_out_written_via_on_change_in_run_loop(tmp_path):
    root = _project(tmp_path)
    watcher = ProjectWatcher(root)

    calls: list[WatchEvent] = []

    def on_change(engine, event):
        calls.append(event)

    # A fake clock loop: on the 1st sleep, create a file so the following poll
    # sees it; stop after 2 cycles. No real waiting.
    state = {"cycle": 0}

    def fake_sleep(_interval):
        if state["cycle"] == 0:
            (root / "new.py").write_text("def new():\n    return 9\n")
        state["cycle"] += 1

    watcher.run(interval=0.0, on_change=on_change, sleep=fake_sleep, max_cycles=2)

    assert len(calls) == 1
    assert calls[0].added == ["new.py"]


def test_diff_is_sorted_and_categorized():
    old = {"keep.py": (1, 1), "mod.py": (1, 1), "del.py": (1, 1)}
    new = {"keep.py": (1, 1), "mod.py": (2, 2), "add.py": (5, 5)}
    event = ProjectWatcher._diff(old, new)
    assert event.added == ["add.py"]
    assert event.modified == ["mod.py"]
    assert event.removed == ["del.py"]
