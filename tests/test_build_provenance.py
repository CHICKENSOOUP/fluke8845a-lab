from __future__ import annotations

import hashlib
import importlib
import json
import platform
import struct
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest


def _provenance_module():
    try:
        return importlib.import_module("scripts.build_provenance")
    except ModuleNotFoundError:
        pytest.fail("build provenance generation and validation are not implemented")


def test_source_tree_digest_is_order_independent_and_tracks_path_and_bytes(tmp_path):
    provenance = _provenance_module()
    (tmp_path / "a.txt").write_bytes(b"a")
    (tmp_path / "dir").mkdir()
    (tmp_path / "dir" / "b.txt").write_bytes(b"bc")

    first = provenance.source_tree_digest(tmp_path, ["a.txt", "dir/b.txt"])
    reversed_order = provenance.source_tree_digest(tmp_path, ["dir/b.txt", "a.txt"])

    # Canonical records are sorted POSIX paths with content hashes, so the
    # digest is stable independent of traversal order.
    assert first == reversed_order
    assert first == "DF4F00B925B1D999F7B2C212B0A6D825A6B6010CDF22FC49A24974097CFF239F"

    (tmp_path / "a.txt").write_bytes(b"A")
    assert provenance.source_tree_digest(tmp_path, ["a.txt", "dir/b.txt"]) != first


def test_provenance_marks_dirty_build_as_working_tree_snapshot_and_binds_exes(tmp_path):
    provenance = _provenance_module()
    source_digest = "A" * 64
    desktop = tmp_path / "Fluke8845A.exe"
    web = tmp_path / "Fluke8845A-Web.exe"
    desktop.write_bytes(b"desktop binary")
    web.write_bytes(b"web binary")
    state = {"head_commit": "1" * 40, "worktree_dirty": True}

    document = provenance.make_build_provenance(
        candidate_build_name="Fluke8845A_test",
        built_at_utc="2026-10-16T00:00:00Z",
        source_tree_sha256=source_digest,
        git_state=state,
        desktop_exe=desktop,
        web_exe=web,
    )

    assert document["head_commit"] == "1" * 40
    assert document["worktree_dirty"] is True
    assert document["build_source_kind"] == "working_tree_snapshot"
    assert document["source_tree_sha256"] == source_digest
    assert document["candidate_build_name"] == "Fluke8845A_test"
    assert document["executables"] == {
        "desktop": {
            "archive_path": "Fluke8845A.exe",
            "sha256": hashlib.sha256(b"desktop binary").hexdigest().upper(),
        },
        "web": {
            "archive_path": "Fluke8845A-Web.dist/Fluke8845A-Web.exe",
            "sha256": hashlib.sha256(b"web binary").hexdigest().upper(),
        },
    }


def test_provenance_rejects_candidate_or_executable_hash_mismatch(tmp_path):
    provenance = _provenance_module()
    desktop = b"desktop binary"
    web = b"web binary"
    document = {
        "format_version": 1,
        "candidate_build_name": "Fluke8845A_test",
        "built_at_utc": "2026-10-16T00:00:00Z",
        "head_commit": "1" * 40,
        "worktree_dirty": True,
        "build_source_kind": "working_tree_snapshot",
        "source_tree_sha256": "A" * 64,
        "source_tree_digest_algorithm": provenance.SOURCE_TREE_DIGEST_ALGORITHM,
        "executables": {
            "desktop": {"archive_path": "Fluke8845A.exe", "sha256": hashlib.sha256(desktop).hexdigest().upper()},
            "web": {"archive_path": "Fluke8845A-Web.dist/Fluke8845A-Web.exe", "sha256": hashlib.sha256(web).hexdigest().upper()},
        },
    }

    with pytest.raises(ValueError, match="candidate build name"):
        provenance.validate_provenance(document, expected_candidate_build_name="another_candidate")
    with pytest.raises(ValueError, match="desktop executable SHA-256"):
        provenance.validate_provenance(
            document,
            expected_candidate_build_name="Fluke8845A_test",
            desktop_exe=b"stale desktop binary",
            web_exe=web,
        )
    with pytest.raises(ValueError, match="working_tree_snapshot"):
        invalid = dict(document, build_source_kind="commit_tree")
        provenance.validate_provenance(invalid, expected_candidate_build_name="Fluke8845A_test")


def test_source_archive_provenance_digest_covers_exact_archive_sources(tmp_path):
    provenance = _provenance_module()
    source_members = {"README.md": b"source readme", "src/app.py": b"print('ok')"}
    document = {
        "format_version": 1,
        "candidate_build_name": "Fluke8845A_test",
        "built_at_utc": "2026-10-16T00:00:00Z",
        "head_commit": "1" * 40,
        "worktree_dirty": True,
        "build_source_kind": "working_tree_snapshot",
        "source_tree_sha256": provenance.source_tree_digest_from_members(source_members.items()),
        "source_tree_digest_algorithm": provenance.SOURCE_TREE_DIGEST_ALGORITHM,
        "executables": {
            "desktop": {"archive_path": "Fluke8845A.exe", "sha256": "A" * 64},
            "web": {"archive_path": "Fluke8845A-Web.dist/Fluke8845A-Web.exe", "sha256": "B" * 64},
        },
    }

    provenance.validate_source_archive_provenance(
        document,
        source_members,
        expected_candidate_build_name="Fluke8845A_test",
    )

    changed = dict(source_members, **{"src/app.py": b"modified source"})
    with pytest.raises(ValueError, match="source tree SHA-256"):
        provenance.validate_source_archive_provenance(
            document,
            changed,
            expected_candidate_build_name="Fluke8845A_test",
        )


def test_source_archive_validation_rejects_a_stale_dependency_lock_hash():
    provenance = _provenance_module()
    lock_bytes = b"numpy==2.0.0\n"
    source_members = {
        "README.md": b"source archive",
        "requirements-lock.txt": lock_bytes,
    }
    document = {
        "format_version": 1,
        "artifact_scope": "source_only",
        "candidate_build_name": "Fluke8845A_lock_hash_test",
        "built_at_utc": "2026-10-16T00:00:00Z",
        "head_commit": "1" * 40,
        "worktree_dirty": False,
        "build_source_kind": "commit_tree",
        "source_tree_sha256": provenance.source_tree_digest_from_members(source_members.items()),
        "source_tree_digest_algorithm": provenance.SOURCE_TREE_DIGEST_ALGORITHM,
        "dependency_lock_sha256": {
            "requirements-lock.txt": hashlib.sha256(lock_bytes).hexdigest().upper(),
        },
    }
    provenance.validate_source_archive_provenance(
        document,
        source_members,
        expected_candidate_build_name="Fluke8845A_lock_hash_test",
    )

    changed_members = {**source_members, "requirements-lock.txt": b"numpy==2.1.0\n"}
    stale_hash_document = dict(
        document,
        source_tree_sha256=provenance.source_tree_digest_from_members(changed_members.items()),
    )
    with pytest.raises(ValueError, match="dependency lock SHA-256"):
        provenance.validate_source_archive_provenance(
            stale_hash_document,
            changed_members,
            expected_candidate_build_name="Fluke8845A_lock_hash_test",
        )


def test_source_only_provenance_validates_without_fabricated_executable_hashes(tmp_path):
    provenance = _provenance_module()
    document = {
        "format_version": 1,
        "artifact_scope": "source_only",
        "candidate_build_name": "Fluke8845A_source_test",
        "built_at_utc": "2026-10-16T00:00:00Z",
        "head_commit": "1" * 40,
        "worktree_dirty": False,
        "build_source_kind": "commit_tree",
        "source_tree_sha256": provenance.source_tree_digest_from_members([("README.md", b"source")]),
        "source_tree_digest_algorithm": provenance.SOURCE_TREE_DIGEST_ALGORITHM,
    }
    members = {"README.md": b"source"}
    provenance.validate_source_archive_provenance(
        document, members, expected_candidate_build_name="Fluke8845A_source_test"
    )
    with pytest.raises(ValueError, match="source-only provenance"):
        provenance.validate_windows_archive_provenance(
            document, {}, expected_candidate_build_name="Fluke8845A_source_test"
        )


def test_source_release_workflow_creates_and_passes_source_provenance():
    workflow = (Path(__file__).resolve().parents[1] / ".github/workflows/publish-release.yml").read_text(encoding="utf-8")
    assert "build_provenance.py source --root . --candidate-build-name Fluke8845A" in workflow
    assert "package_source.py --output dist/Fluke8845A_Source_Project.zip --provenance build/BUILD-PROVENANCE.json" in workflow


def test_source_packager_embeds_exact_provenance_and_checks_candidate_and_source_digest(tmp_path):
    from scripts.package_source import create_archive

    provenance = _provenance_module()
    root = Path(__file__).resolve().parents[1]
    desktop = b"companion desktop exe"
    web = b"companion web exe"
    document = {
        "format_version": 1,
        "candidate_build_name": "Fluke8845A_provenance_test",
        "built_at_utc": "2026-10-16T00:00:00Z",
        "head_commit": "1" * 40,
        "worktree_dirty": True,
        "build_source_kind": "working_tree_snapshot",
        "source_tree_sha256": provenance.source_tree_digest(root),
        "source_tree_digest_algorithm": provenance.SOURCE_TREE_DIGEST_ALGORITHM,
        "executables": {
            "desktop": {"archive_path": "Fluke8845A.exe", "sha256": hashlib.sha256(desktop).hexdigest().upper()},
            "web": {"archive_path": "Fluke8845A-Web.dist/Fluke8845A-Web.exe", "sha256": hashlib.sha256(web).hexdigest().upper()},
        },
    }
    provenance_path = tmp_path / "BUILD-PROVENANCE.json"
    provenance_bytes = (json.dumps(document, sort_keys=True) + "\n").encode("utf-8")
    provenance_path.write_bytes(provenance_bytes)
    archive = tmp_path / "Fluke8845A_provenance_test_Source_Project.zip"

    create_archive(archive, provenance_path=provenance_path)

    from zipfile import ZipFile

    with ZipFile(archive) as result:
        assert result.read("BUILD-PROVENANCE.json") == provenance_bytes
        members = {name: result.read(name) for name in result.namelist() if name != "BUILD-PROVENANCE.json"}
    provenance.validate_source_archive_provenance(
        json.loads(provenance_bytes),
        members,
        expected_candidate_build_name="Fluke8845A_provenance_test",
    )


def test_windows_archive_provenance_binds_both_executable_hashes():
    provenance = _provenance_module()
    desktop = b"desktop exe"
    web = b"web exe"
    document = {
        "format_version": 1,
        "candidate_build_name": "Fluke8845A_provenance_test",
        "built_at_utc": "2026-10-16T00:00:00Z",
        "head_commit": "1" * 40,
        "worktree_dirty": True,
        "build_source_kind": "working_tree_snapshot",
        "source_tree_sha256": "A" * 64,
        "source_tree_digest_algorithm": provenance.SOURCE_TREE_DIGEST_ALGORITHM,
        "executables": {
            "desktop": {"archive_path": "Fluke8845A.exe", "sha256": hashlib.sha256(desktop).hexdigest().upper()},
            "web": {"archive_path": "Fluke8845A-Web.dist/Fluke8845A-Web.exe", "sha256": hashlib.sha256(web).hexdigest().upper()},
        },
    }
    members = {"Fluke8845A.exe": desktop, "Fluke8845A-Web.dist/Fluke8845A-Web.exe": web}

    provenance.validate_windows_archive_provenance(
        document,
        members,
        expected_candidate_build_name="Fluke8845A_provenance_test",
    )

    with pytest.raises(ValueError, match="web executable SHA-256"):
        provenance.validate_windows_archive_provenance(
            document,
            {**members, "Fluke8845A-Web.dist/Fluke8845A-Web.exe": b"stale web exe"},
            expected_candidate_build_name="Fluke8845A_provenance_test",
        )


def test_portable_zip_requires_build_provenance_at_archive_root():
    from scripts.package_windows_release import REQUIRED_ENTRIES

    assert "BUILD-PROVENANCE.json" in REQUIRED_ENTRIES


def test_desktop_only_provenance_binds_desktop_without_claiming_a_web_binary(tmp_path):
    provenance = _provenance_module()
    desktop = tmp_path / "Fluke8845A.exe"
    desktop.write_bytes(b"desktop-only executable")
    document = provenance.make_build_provenance(
        candidate_build_name="Fluke8845A_desktop",
        built_at_utc="2026-10-22T12:00:00Z",
        source_tree_sha256="A" * 64,
        git_state={"head_commit": "1" * 40, "worktree_dirty": True},
        desktop_exe=desktop,
        web_exe=None,
        desktop_only=True,
    )

    assert document["artifact_scope"] == "desktop_only"
    assert set(document["executables"]) == {"desktop"}
    provenance.validate_windows_archive_provenance(
        document, {"Fluke8845A.exe": desktop.read_bytes()},
        expected_candidate_build_name="Fluke8845A_desktop",
    )
    with pytest.raises(ValueError, match="desktop executable"):
        provenance.validate_windows_archive_provenance(
            document, {"Fluke8845A.exe": b"wrong executable"},
            expected_candidate_build_name="Fluke8845A_desktop",
        )
    with pytest.raises(ValueError, match="desktop-only"):
        provenance.validate_windows_archive_provenance(
            document, {"Fluke8845A.exe": desktop.read_bytes(), provenance.WEB_ARCHIVE_PATH: b"web"},
            expected_candidate_build_name="Fluke8845A_desktop",
        )


def test_git_snapshot_distinguishes_clean_commit_from_dirty_worktree(tmp_path):
    provenance = _provenance_module()
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.email", "test@example.invalid"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "Test"], check=True)
    source = tmp_path / "source.txt"
    source.write_text("committed", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", "source.txt"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-qm", "initial"], check=True)

    clean = provenance.capture_git_state(tmp_path)
    assert clean["head_commit"] == subprocess.check_output(
        ["git", "-C", str(tmp_path), "rev-parse", "HEAD"], text=True
    ).strip()
    assert clean["worktree_dirty"] is False

    source.write_text("modified", encoding="utf-8")
    dirty = provenance.capture_git_state(tmp_path)
    assert dirty["head_commit"] == clean["head_commit"]
    assert dirty["worktree_dirty"] is True


def test_git_snapshot_marks_ignored_source_archive_file_as_dirty(tmp_path):
    provenance = _provenance_module()
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.email", "test@example.invalid"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "Test"], check=True)
    (tmp_path / ".gitignore").write_text("README.md\n", encoding="utf-8")
    (tmp_path / "tracked.txt").write_text("committed", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", ".gitignore", "tracked.txt"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-qm", "initial"], check=True)
    (tmp_path / "README.md").write_text("included but ignored", encoding="utf-8")
    assert subprocess.check_output(
        ["git", "-C", str(tmp_path), "status", "--porcelain=v1", "--untracked-files=all"], text=True
    ).strip() == ""

    assert provenance.capture_git_state(tmp_path)["worktree_dirty"] is True


def test_git_snapshot_ignores_generated_egg_info_metadata(tmp_path):
    provenance = _provenance_module()
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.email", "test@example.invalid"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "Test"], check=True)
    (tmp_path / ".gitignore").write_text("*.egg-info/\n", encoding="utf-8")
    package = tmp_path / "src" / "fluke8845a"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("__version__ = 'test'\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", ".gitignore", "src"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-qm", "initial"], check=True)
    metadata = tmp_path / "src" / "fluke8845a_lab.egg-info"
    metadata.mkdir()
    (metadata / "PKG-INFO").write_text("generated install metadata", encoding="utf-8")

    assert subprocess.check_output(
        ["git", "-C", str(tmp_path), "status", "--porcelain=v1", "--untracked-files=all"], text=True
    ).strip() == ""
    assert provenance.capture_git_state(tmp_path)["worktree_dirty"] is False


def test_provenance_creation_rejects_source_changes_after_build_start(tmp_path):
    provenance = _provenance_module()
    (tmp_path / "README.md").write_text("before build", encoding="utf-8")
    initial_digest = provenance.source_tree_digest(tmp_path)
    desktop = tmp_path / "desktop.exe"
    web = tmp_path / "web.exe"
    desktop.write_bytes(b"desktop")
    web.write_bytes(b"web")
    (tmp_path / "README.md").write_text("changed during build", encoding="utf-8")

    with pytest.raises(ValueError, match="source tree changed between build start"):
        provenance.create_build_provenance(
            root=tmp_path,
            candidate_build_name="candidate",
            expected_source_tree_sha256=initial_digest,
            desktop_exe=desktop,
            web_exe=web,
        )


def test_build_provenance_records_source_version_not_stale_self_package_metadata(tmp_path, monkeypatch):
    provenance = _provenance_module()
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "fluke8845a-lab"\nversion = "1.1.0"\n',
        encoding="utf-8",
    )
    desktop = tmp_path / "Fluke8845A.exe"
    web = tmp_path / "Fluke8845A-Web.exe"
    desktop.write_bytes(b"desktop executable")
    web.write_bytes(b"web executable")
    monkeypatch.setattr(provenance, "source_tree_digest", lambda root: "A" * 64)
    monkeypatch.setattr(
        provenance,
        "capture_git_state",
        lambda root: {"head_commit": "1" * 40, "worktree_dirty": False},
    )
    monkeypatch.setattr(
        provenance,
        "capture_toolchain",
        lambda: {
            "python_version": "3.12.10",
            "python_implementation": "CPython",
            "python_architecture": "AMD64",
            "python_bits": 64,
            "nuitka_version": "4.1.1",
            "node_version": "v24.19.0",
            "compiler_version": "MSVC",
        },
    )
    monkeypatch.setattr(provenance, "dependency_lock_hashes", lambda root: {})
    monkeypatch.setattr(
        provenance,
        "installed_distribution_inventory",
        lambda: [
            {"name": "fluke8845a-lab", "version": "1.0.7"},
            {"name": "PySide6_Essentials", "version": "6.12.0"},
        ],
    )

    document = provenance.create_build_provenance(
        root=tmp_path,
        candidate_build_name="Fluke8845A_source_version",
        expected_source_tree_sha256="A" * 64,
        desktop_exe=desktop,
        web_exe=web,
    )

    assert document["project_version"] == "1.1.0"
    assert document["installed_distributions"] == [
        {"name": "PySide6_Essentials", "version": "6.12.0"},
    ]
    provenance.validate_provenance(
        document,
        expected_candidate_build_name="Fluke8845A_source_version",
        desktop_exe=desktop.read_bytes(),
        web_exe=web.read_bytes(),
    )

    source_document = provenance.create_source_provenance(
        root=tmp_path,
        candidate_build_name="Fluke8845A_source_version_source",
    )
    assert source_document["project_version"] == "1.1.0"


def test_build_provenance_records_toolchain_and_lockfile_hashes(tmp_path, monkeypatch):
    provenance = _provenance_module()
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "fluke8845a-lab"\nversion = "1.1.0"\n',
        encoding="utf-8",
    )
    (tmp_path / "requirements-lock.txt").write_bytes(b"desktop dependencies\n")
    (tmp_path / "requirements-lock-python311.txt").write_bytes(b"Python 3.11 dependencies\n")
    desktop = tmp_path / "Fluke8845A.exe"
    web = tmp_path / "Fluke8845A-Web.exe"
    desktop.write_bytes(b"desktop executable")
    web.write_bytes(b"web executable")
    monkeypatch.setattr(provenance, "source_tree_digest", lambda root: "A" * 64)
    monkeypatch.setattr(
        provenance,
        "capture_git_state",
        lambda root: {"head_commit": "1" * 40, "worktree_dirty": False},
    )
    machine = "AMD64"
    monkeypatch.setattr(provenance.platform, "machine", lambda: machine)
    installed_distributions = [
        {"name": "numpy", "version": "2.5.4"},
        {"name": "pyserial", "version": "3.5"},
    ]
    monkeypatch.setattr(
        provenance,
        "installed_distribution_inventory",
        lambda: installed_distributions,
    )

    def run_tool(command, **kwargs):
        if command[-3:] == ["-m", "nuitka", "--version"]:
            return SimpleNamespace(
                stdout=(
                    "Nuitka-Version: 4.1.1\n"
                    "Version C compiler: gcc (GCC) 15.2.0.\n"
                )
            )
        if Path(command[0]).name.lower().startswith("node"):
            return SimpleNamespace(stdout="v24.19.0\n")
        raise AssertionError(f"unexpected toolchain command: {command}")

    monkeypatch.setattr(provenance.subprocess, "run", run_tool)

    document = provenance.create_build_provenance(
        root=tmp_path,
        candidate_build_name="Fluke8845A_build_metadata",
        expected_source_tree_sha256="A" * 64,
        desktop_exe=desktop,
        web_exe=web,
    )

    assert document["toolchain"] == {
        "python_version": platform.python_version(),
        "python_implementation": platform.python_implementation(),
        "python_architecture": machine,
        "python_bits": struct.calcsize("P") * 8,
        "nuitka_version": "4.1.1",
        "node_version": "v24.19.0",
        "compiler_version": "gcc (GCC) 15.2.0.",
    }
    assert document["dependency_lock_sha256"] == {
        "requirements-lock-python311.txt": hashlib.sha256(
            b"Python 3.11 dependencies\n"
        ).hexdigest().upper(),
        "requirements-lock.txt": hashlib.sha256(
            b"desktop dependencies\n"
        ).hexdigest().upper(),
    }
    assert document["installed_distributions"] == installed_distributions
    assert document["project_version"] == "1.1.0"

    source_document = provenance.create_source_provenance(
        root=tmp_path, candidate_build_name="Fluke8845A_source_build_metadata"
    )
    assert source_document["toolchain"] == document["toolchain"]
    assert source_document["dependency_lock_sha256"] == document["dependency_lock_sha256"]
    assert source_document["project_version"] == "1.1.0"
    provenance.validate_provenance(
        source_document,
        expected_candidate_build_name="Fluke8845A_source_build_metadata",
    )


@pytest.mark.parametrize(
    "version",
    ["abc", "1!!", "1.1", "1.1.0+", "1.1.0-", "01.1.0", "1.1.0-..", "1.1.0-01", "1.1.0+.."],
)
def test_source_project_metadata_rejects_invalid_semver(tmp_path, version):
    provenance = _provenance_module()
    (tmp_path / "pyproject.toml").write_text(
        f'[project]\nname = "fluke8845a-lab"\nversion = "{version}"\n',
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="valid static project version"):
        provenance.source_project_metadata(tmp_path)


@pytest.mark.parametrize(
    "version",
    ["abc", "1!!", "1.1", "1.1.0+", "1.1.0-", "01.1.0", "1.1.0-..", "1.1.0-01", "1.1.0+.."],
)
def test_provenance_rejects_malformed_project_version(version):
    provenance = _provenance_module()
    document = {
        "format_version": 1,
        "candidate_build_name": "Fluke8845A_invalid_version",
        "built_at_utc": "2026-10-16T00:00:00Z",
        "head_commit": "1" * 40,
        "worktree_dirty": False,
        "build_source_kind": "commit_tree",
        "source_tree_sha256": "A" * 64,
        "source_tree_digest_algorithm": provenance.SOURCE_TREE_DIGEST_ALGORITHM,
        "executables": {
            "desktop": {"archive_path": "Fluke8845A.exe", "sha256": "B" * 64},
            "web": {
                "archive_path": "Fluke8845A-Web.dist/Fluke8845A-Web.exe",
                "sha256": "C" * 64,
            },
        },
        "project_version": version,
    }

    with pytest.raises(ValueError, match="project version is malformed"):
        provenance.validate_provenance(
            document,
            expected_candidate_build_name="Fluke8845A_invalid_version",
        )


def test_toolchain_provenance_keeps_unavailable_optional_tool_versions_null(monkeypatch):
    provenance = _provenance_module()

    def run_tool(command, **kwargs):
        raise FileNotFoundError(command[0])

    monkeypatch.setattr(provenance.subprocess, "run", run_tool)

    def missing_distribution(name):
        raise provenance.importlib.metadata.PackageNotFoundError(name)

    monkeypatch.setattr(provenance.importlib.metadata, "version", missing_distribution)

    toolchain = provenance.capture_toolchain()

    assert toolchain["python_version"] == platform.python_version()
    assert toolchain["python_architecture"] == platform.machine()
    assert toolchain["nuitka_version"] is None
    assert toolchain["node_version"] is None
    assert toolchain["compiler_version"] is None


def test_legacy_provenance_without_toolchain_or_lock_hashes_remains_valid():
    provenance = _provenance_module()
    document = {
        "format_version": 1,
        "candidate_build_name": "Fluke8845A_legacy",
        "built_at_utc": "2026-10-16T00:00:00Z",
        "head_commit": "1" * 40,
        "worktree_dirty": False,
        "build_source_kind": "commit_tree",
        "source_tree_sha256": "A" * 64,
        "source_tree_digest_algorithm": provenance.SOURCE_TREE_DIGEST_ALGORITHM,
        "executables": {
            "desktop": {"archive_path": "Fluke8845A.exe", "sha256": "B" * 64},
            "web": {
                "archive_path": "Fluke8845A-Web.dist/Fluke8845A-Web.exe",
                "sha256": "C" * 64,
            },
        },
    }

    provenance.validate_provenance(
        document, expected_candidate_build_name="Fluke8845A_legacy"
    )


def test_provenance_rejects_malformed_optional_toolchain_and_lock_hashes():
    provenance = _provenance_module()
    document = {
        "format_version": 1,
        "candidate_build_name": "Fluke8845A_invalid_metadata",
        "built_at_utc": "2026-10-16T00:00:00Z",
        "head_commit": "1" * 40,
        "worktree_dirty": False,
        "build_source_kind": "commit_tree",
        "source_tree_sha256": "A" * 64,
        "source_tree_digest_algorithm": provenance.SOURCE_TREE_DIGEST_ALGORITHM,
        "executables": {
            "desktop": {"archive_path": "Fluke8845A.exe", "sha256": "B" * 64},
            "web": {
                "archive_path": "Fluke8845A-Web.dist/Fluke8845A-Web.exe",
                "sha256": "C" * 64,
            },
        },
        "toolchain": {"python_version": "3.12.10"},
        "dependency_lock_sha256": {"requirements-lock.txt": "not-a-sha256"},
    }

    with pytest.raises(ValueError, match="toolchain metadata"):
        provenance.validate_provenance(
            document, expected_candidate_build_name="Fluke8845A_invalid_metadata"
        )

    document["toolchain"] = {
        "python_version": "3.12.10",
        "python_implementation": "CPython",
        "python_architecture": "AMD64",
        "python_bits": 64,
        "nuitka_version": None,
        "node_version": None,
        "compiler_version": None,
    }
    with pytest.raises(ValueError, match="dependency lock SHA-256"):
        provenance.validate_provenance(
            document, expected_candidate_build_name="Fluke8845A_invalid_metadata"
        )
