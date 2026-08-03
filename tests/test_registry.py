import pytest

from ai_os import registry


def test_add_list_resolve_remove(tmp_path, isolated_home, fixture_root):
    entry = registry.add("sample", str(fixture_root))
    assert entry.path == str(fixture_root.resolve())

    entries = registry.list_projects()
    assert [e.name for e in entries] == ["sample"]
    assert entries[0].exists is True

    resolved = registry.resolve("sample")
    assert resolved == fixture_root.resolve()

    registry.remove("sample")
    assert registry.list_projects() == []


def test_resolve_falls_back_to_literal_path(isolated_home, fixture_root):
    resolved = registry.resolve(str(fixture_root))
    assert resolved == fixture_root.resolve()


def test_add_rejects_invalid_name(isolated_home, fixture_root):
    with pytest.raises(registry.InvalidProjectNameError):
        registry.add("bad name!", str(fixture_root))


def test_add_rejects_missing_path(isolated_home, tmp_path):
    with pytest.raises(registry.ProjectPathError):
        registry.add("ghost", str(tmp_path / "does-not-exist"))


def test_add_rejects_duplicate_unless_forced(isolated_home, fixture_root):
    registry.add("sample", str(fixture_root))
    with pytest.raises(ValueError):
        registry.add("sample", str(fixture_root))
    registry.add("sample", str(fixture_root), force=True)  # should not raise


def test_remove_missing_raises(isolated_home):
    with pytest.raises(registry.ProjectNotFoundError):
        registry.remove("nope")


def test_resolve_missing_path_raises(isolated_home):
    with pytest.raises(registry.ProjectPathError):
        registry.resolve("nope-not-registered-and-not-a-real-path")
