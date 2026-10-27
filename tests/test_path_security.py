from __future__ import annotations

from pathlib import Path

import fluke8845a.path_security as path_security


def test_resolve_safe_child_file_accepts_only_a_direct_regular_child(tmp_path):
    experiment = tmp_path / "experiment"
    experiment.mkdir()
    source = experiment / "measurements.csv"
    source.write_text("sample_index,value_si\n1,2.0\n", encoding="utf-8")

    assert path_security.resolve_safe_child_file(experiment, "measurements.csv") == source.resolve()
    assert path_security.resolve_safe_child_file(experiment, "..") is None
    assert path_security.resolve_safe_child_file(experiment, "nested/measurements.csv") is None


def test_resolve_safe_child_file_rejects_linked_parent_and_child(tmp_path, monkeypatch):
    experiment = tmp_path / "experiment"
    experiment.mkdir()
    source = experiment / "events.csv"
    source.write_text("event_index,event\n", encoding="utf-8")
    original_check = path_security.is_symlink_or_junction

    def linked(path):
        candidate = Path(path)
        return candidate in {experiment, source} or original_check(candidate)

    monkeypatch.setattr(path_security, "is_symlink_or_junction", linked)

    assert path_security.resolve_safe_child_file(experiment, "events.csv") is None
    assert path_security.resolve_safe_child_file(tmp_path / "experiment", "events.csv") is None


def test_resolve_safe_child_file_rejects_resolved_file_outside_parent(tmp_path, monkeypatch):
    experiment = tmp_path / "experiment"
    experiment.mkdir()
    source = experiment / "measurements.csv"
    source.write_text("sample_index,value_si\n", encoding="utf-8")
    external = tmp_path / "measurements.csv"
    external.write_text("private\n", encoding="utf-8")
    original_resolve = Path.resolve

    def resolve(path, *args, **kwargs):
        if path == source:
            return external
        return original_resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", resolve)

    assert path_security.resolve_safe_child_file(experiment, "measurements.csv") is None
