from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest


def _launcher_api():
    from scripts.latest_verified_launcher import LAUNCHER_NAME, create_latest_launcher

    return LAUNCHER_NAME, create_latest_launcher


def _write_build_identity(exe: Path, *, expected_hash: str | None = None) -> None:
    (exe.parent / "BUILD-IDENTITY.json").write_text(
        json.dumps({
            "source_tree_sha256": "A" * 64,
            "executables": {
                "desktop": {"sha256": expected_hash or hashlib.sha256(exe.read_bytes()).hexdigest()},
            },
        }),
        encoding="utf-8",
    )


def test_latest_launcher_targets_the_verified_exe_relative_to_dist(tmp_path):
    launcher_name, create_latest_launcher = _launcher_api()
    dist_root = tmp_path / "release files"
    exe = dist_root / "Fluke8845A_v1.1.0_9365716" / "Fluke8845A.dist" / "Fluke8845A.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"verified desktop executable")
    _write_build_identity(exe)

    launcher = create_latest_launcher(dist_root, exe)

    assert launcher == dist_root / launcher_name
    text = launcher.read_text(encoding="ascii")
    assert 'set "APP_EXE=%~dp0Fluke8845A_v1.1.0_9365716\\Fluke8845A.dist\\Fluke8845A.exe"' in text
    assert 'start "" /D "%APP_DIR%" "%APP_EXE%"' in text
    assert "%*" not in text
    assert str(dist_root) not in text


def test_latest_launcher_supports_the_standard_distribution_layout(tmp_path):
    launcher_name, create_latest_launcher = _launcher_api()
    dist_root = tmp_path / "dist"
    exe = dist_root / "Fluke8845A.dist" / "Fluke8845A.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"verified standard executable")
    _write_build_identity(exe)

    launcher = create_latest_launcher(dist_root, exe)

    text = launcher.read_text(encoding="ascii")
    assert launcher == dist_root / launcher_name
    assert 'set "APP_EXE=%~dp0Fluke8845A.dist\\Fluke8845A.exe"' in text
    assert 'set "APP_DIR=%~dp0Fluke8845A.dist"' in text


def test_latest_launcher_refuses_targets_outside_dist_without_replacing_previous(tmp_path):
    launcher_name, create_latest_launcher = _launcher_api()
    dist_root = tmp_path / "dist"
    dist_root.mkdir()
    launcher = dist_root / launcher_name
    launcher.write_text("previous verified launcher", encoding="ascii")
    outside_exe = tmp_path / "other" / "Fluke8845A.exe"
    outside_exe.parent.mkdir()
    outside_exe.write_bytes(b"unrelated executable")

    with pytest.raises(ValueError, match="inside the distribution directory"):
        create_latest_launcher(dist_root, outside_exe)

    assert launcher.read_text(encoding="ascii") == "previous verified launcher"


def test_latest_launcher_requires_the_expected_packaged_executable(tmp_path):
    launcher_name, create_latest_launcher = _launcher_api()
    dist_root = tmp_path / "dist"
    wrong_exe = dist_root / "Fluke8845A.dist" / "Other.exe"
    wrong_exe.parent.mkdir(parents=True)
    wrong_exe.write_bytes(b"not the desktop app")

    with pytest.raises(ValueError, match="Fluke8845A.exe"):
        create_latest_launcher(dist_root, wrong_exe)

    assert not (dist_root / launcher_name).exists()


def test_latest_launcher_refuses_an_executable_that_does_not_match_its_build_identity(tmp_path):
    launcher_name, create_latest_launcher = _launcher_api()
    dist_root = tmp_path / "dist"
    exe = dist_root / "Fluke8845A.dist" / "Fluke8845A.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"changed executable")
    _write_build_identity(exe, expected_hash="0" * 64)

    with pytest.raises(ValueError, match="SHA-256"):
        create_latest_launcher(dist_root, exe)

    assert not (dist_root / launcher_name).exists()


def test_windows_build_updates_latest_launcher_only_after_both_packages_succeed():
    build_script = (Path(__file__).resolve().parents[1] / "build_windows.ps1").read_text(encoding="utf-8")
    portable_success = build_script.index('if ($LASTEXITCODE -ne 0) { throw "Portable Windows package creation or verification failed." }')
    source_success = build_script.index('if ($LASTEXITCODE -ne 0) { throw "Source project archive creation or verification failed." }')
    assert 'scripts\\latest_verified_launcher.py' in build_script
    launcher_update = build_script.index('scripts\\latest_verified_launcher.py')
    assert portable_success < source_success < launcher_update
    assert "if ($SideBySide) {" in build_script[source_success:launcher_update]
    assert "--dist-root $distRoot" in build_script[launcher_update:launcher_update + 500]
    assert "--executable $exe" in build_script[launcher_update:launcher_update + 500]
