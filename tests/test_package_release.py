import hashlib
import json
import logging
import re
from pathlib import Path
from zipfile import ZipFile

import pytest

from scripts import atomic_archive, package_source, package_windows_release
from scripts.archive_paths import ensure_regular_file_within
from scripts.package_source import REQUIRED_ARCHIVE_PATHS, files_for_archive, validate_web_archive_resources
from scripts.package_windows_release import (
    REQUIRED_ENTRIES,
    validate_mock_e2e_report,
    validate_packaged_mock_e2e_report,
    validate_runtime_build_identity,
    validate_portable_runtime_components,
    validate_runtime_license_manifest,
)
from scripts.release_metadata import release_identity
from scripts.release_publication import (
    main as release_publication_main,
    validate_github_release,
    validate_oci_manifest,
)


def test_windows_package_software_smokes_allow_more_than_one_minute():
    assert package_windows_release.SOFTWARE_SMOKE_TIMEOUT_SECONDS >= 60
    assert package_windows_release.WEB_STARTUP_TIMEOUT_SECONDS >= 60
    build_script = (package_windows_release.ROOT / "build_windows.ps1").read_text(encoding="utf-8")
    timeout_match = re.search(r"Wait-Process -Id \$Process\.Id -Timeout (\d+)", build_script)
    assert timeout_match is not None
    assert int(timeout_match.group(1)) >= 60


def test_build_script_transports_unicode_python_runtime_path_as_utf8_hex():
    build_script = (package_windows_release.ROOT / "build_windows.ps1").read_text(encoding="utf-8")
    assert "sys.base_prefix.encode('utf-8').hex()" in build_script
    assert "[System.Text.Encoding]::UTF8.GetString($pythonBaseBytes)" in build_script


def test_build_script_generates_archived_mock_report_after_build_identity():
    build_script = (package_windows_release.ROOT / "build_windows.ps1").read_text(encoding="utf-8")
    identity_marker = build_script.index('"BUILD-IDENTITY.json"')
    mock_report = build_script.index('$mockE2E = Start-Process')
    archived_report = build_script.index('Copy-Item -LiteralPath $mockE2EReport -Destination $releaseMockE2EReport')

    assert identity_marker < mock_report < archived_report


def test_windows_build_runs_web_tests_before_nuitka_compilation():
    build_script = (package_windows_release.ROOT / "build_windows.ps1").read_text(encoding="utf-8")
    web_gate = build_script.index("scripts\\run_web_tests.py")
    nuitka_compile = build_script.index("-m nuitka $entryPoint")
    assert web_gate < nuitka_compile
    assert 'if ($LASTEXITCODE -ne 0) { throw "Web frontend JavaScript tests failed' in build_script


def test_windows_desktop_build_excludes_optional_qttest_module():
    build_script = (package_windows_release.ROOT / "build_windows.ps1").read_text(encoding="utf-8")
    desktop_compile = build_script.index("-m nuitka $entryPoint")
    web_entry = build_script.index("$webEntryPoint =", desktop_compile)

    assert "--nofollow-import-to=PySide6.QtTest" in build_script[desktop_compile:web_entry]


@pytest.mark.parametrize("component", ["PySide6/QtTest.pyd", "qt6test.dll"])
def test_portable_release_rejects_optional_qttest_runtime_files(component):
    with pytest.raises(ValueError, match="unused Qt"):
        validate_portable_runtime_components({component})


def test_portable_archives_require_current_candidate_review_record():
    current_review = "docs/licenses/windows-portable-review-2026-10-20.md"

    assert current_review in REQUIRED_ARCHIVE_PATHS
    assert current_review in REQUIRED_ENTRIES


def test_release_workflow_uses_the_shared_web_test_gate():
    workflow = (package_windows_release.ROOT / ".github" / "workflows" / "publish-release.yml").read_text(
        encoding="utf-8"
    )
    assert "python scripts/run_web_tests.py" in workflow
    assert "node --test tests/web_frontend.test.cjs" not in workflow


def test_source_archive_validation_failure_preserves_previous_archive(tmp_path, monkeypatch):
    source_root = tmp_path / "source"
    source_root.mkdir()
    readme = source_root / "README.md"
    readme.write_text("current source", encoding="utf-8")
    provenance = tmp_path / "provenance.json"
    provenance.write_text("{}", encoding="utf-8")
    destination = source_root / "dist" / "Fluke8845A_Test_Source_Project.zip"
    destination.parent.mkdir()
    old_archive = b"previously verified package"
    destination.write_bytes(old_archive)

    monkeypatch.setattr(package_source, "ROOT", source_root)
    monkeypatch.setattr(package_source, "files_for_archive", lambda *_args, **_kwargs: [readme])
    monkeypatch.setattr(package_source, "candidate_name_from_archive", lambda *_args, **_kwargs: "Fluke8845A_Test")
    monkeypatch.setattr(package_source, "validate_provenance", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(package_source, "validate_source_archive_provenance", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(package_source, "REQUIRED_ARCHIVE_PATHS", ("README.md",))

    def reject_archive(*_args, **_kwargs):
        raise RuntimeError("source package validation failed")

    monkeypatch.setattr(package_source, "validate_web_archive_resources", reject_archive)

    with pytest.raises(RuntimeError, match="source package validation failed"):
        package_source.create_archive(destination, provenance_path=provenance)

    assert destination.read_bytes() == old_archive
    assert not list(destination.parent.glob("*.tmp.zip"))


def test_atomic_archive_replace_failure_preserves_existing_archive_and_cleans_candidate(tmp_path, monkeypatch):
    destination = tmp_path / "release.zip"
    previous_archive = b"previous archive"
    destination.write_bytes(previous_archive)

    def fail_replace(_source, _destination):
        raise PermissionError("archive is locked")

    monkeypatch.setattr(atomic_archive.os, "replace", fail_replace)

    with pytest.raises(PermissionError, match="archive is locked"):
        with atomic_archive.atomic_archive_path(destination) as candidate:
            candidate.write_bytes(b"new archive")

    assert destination.read_bytes() == previous_archive
    assert not list(destination.parent.glob("*.tmp.zip"))


def test_atomic_archive_cleanup_failure_is_reported(tmp_path, monkeypatch, caplog):
    destination = tmp_path / "release.zip"
    previous_archive = b"previous archive"
    destination.write_bytes(previous_archive)
    original_unlink = Path.unlink
    candidate = None

    def fail_candidate_cleanup(path, *args, **kwargs):
        if path.name.endswith(".tmp.zip"):
            raise PermissionError("temporary archive is locked")
        return original_unlink(path, *args, **kwargs)

    with caplog.at_level(logging.WARNING, logger="scripts.atomic_archive"):
        with monkeypatch.context() as patcher:
            patcher.setattr(Path, "unlink", fail_candidate_cleanup)
            with pytest.raises(RuntimeError, match="archive validation failed"):
                with atomic_archive.atomic_archive_path(destination) as candidate:
                    candidate.write_bytes(b"invalid archive")
                    raise RuntimeError("archive validation failed")

    assert destination.read_bytes() == previous_archive
    assert candidate is not None and candidate.is_file()
    assert str(candidate) in caplog.text
    assert "temporary archive is locked" in caplog.text
    original_unlink(candidate, missing_ok=True)


def test_windows_archive_smoke_failure_preserves_previous_archive(tmp_path, monkeypatch, capsys):
    root = tmp_path / "checkout"
    dist = root / "dist"
    app_dist = dist / "candidate" / "Fluke8845A.dist"
    app_dist.mkdir(parents=True)
    desktop_exe = app_dist / "Fluke8845A.exe"
    desktop_exe.write_bytes(b"desktop")
    web_exe = app_dist / "web.exe"
    web_exe.write_bytes(b"web")
    license_manifest = app_dist / "license-manifest.json"
    license_manifest.write_text("{}", encoding="utf-8")
    qt_inventory = app_dist / "qt-runtime-inventory.csv"
    qt_inventory.write_text("path,family,bytes,sha256\n", encoding="utf-8")
    build_identity = app_dist / "BUILD-IDENTITY.json"
    identity_payload = {"source_tree_sha256": "A" * 64}
    build_identity.write_text(json.dumps(identity_payload), encoding="utf-8")
    docs = root / "docs"
    docs.mkdir()
    mock_report = root / "output" / "mock_validation" / "report.json"
    mock_report.parent.mkdir(parents=True)
    mock_report.write_text("{}", encoding="utf-8")
    provenance = root / "build" / "provenance.json"
    provenance.parent.mkdir()
    provenance.write_text(json.dumps(identity_payload), encoding="utf-8")
    archive = dist / "Fluke8845A_Test_Windows_x64.zip"
    old_archive = b"previously verified portable package"
    archive.write_bytes(old_archive)

    monkeypatch.setattr(package_windows_release, "ROOT", root)
    monkeypatch.setattr(package_windows_release, "DIST", dist)
    monkeypatch.setattr(package_windows_release, "candidate_name_from_archive", lambda *_args, **_kwargs: "Fluke8845A_Test")
    monkeypatch.setattr(package_windows_release, "validate_provenance", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(package_windows_release, "_copy_release_docs", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        package_windows_release,
        "_release_files",
        lambda _app_dist: [
            (desktop_exe, "Fluke8845A.exe"),
            (web_exe, "Fluke8845A-Web.dist/Fluke8845A-Web.exe"),
            (build_identity, "BUILD-IDENTITY.json"),
            (license_manifest, "docs/licenses/windows-runtime-license-manifest.json"),
            (qt_inventory, "docs/licenses/windows-qt-runtime-inventory.csv"),
        ],
    )
    monkeypatch.setattr(
        package_windows_release,
        "REQUIRED_ENTRIES",
        {
            "Fluke8845A.exe",
            "Fluke8845A-Web.dist/Fluke8845A-Web.exe",
            "BUILD-IDENTITY.json",
            "docs/licenses/windows-runtime-license-manifest.json",
            "docs/licenses/windows-qt-runtime-inventory.csv",
            package_windows_release.BUILD_PROVENANCE_NAME,
            package_windows_release.MOCK_E2E_REPORT,
        },
    )
    monkeypatch.setattr(package_windows_release, "validate_web_archive_resources", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(package_windows_release, "validate_portable_runtime_components", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(package_windows_release, "validate_runtime_license_manifest", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(package_windows_release, "validate_qt_runtime_inventory", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(package_windows_release, "validate_windows_archive_provenance", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(package_windows_release, "validate_mock_e2e_report", lambda *_args, **_kwargs: None)

    def fail_extracted_smoke(*_args, **_kwargs):
        raise RuntimeError("extracted package smoke failed")

    monkeypatch.setattr(package_windows_release, "verify_extracted_portable_release", fail_extracted_smoke)

    result = package_windows_release.main([
        "--app-dist", str(app_dist), "--archive", str(archive), "--mock-report", str(mock_report),
        "--provenance", str(provenance),
    ])

    assert result == 2
    assert archive.read_bytes() == old_archive
    assert not list(archive.parent.glob("*.tmp.zip"))
    assert "extracted package smoke failed" in capsys.readouterr().err


def test_main_release_identity_contains_project_version_channel_and_commit():
    identity = release_identity("1.0.1", "branch", "main", "af1db2104318abcd")
    assert identity == {
        "tag": "main-v1.0.1-build-af1db2104318",
        "title": "Fluke 8845A Lab v1.0.1 (main build af1db2104318)",
        "version": "1.0.1",
        "prerelease": True,
    }


def test_archive_path_validator_rejects_files_outside_the_package_root(tmp_path):
    root = tmp_path / "package"
    root.mkdir()
    outside = tmp_path / "private.txt"
    outside.write_text("secret", encoding="utf-8")

    with pytest.raises(ValueError, match="outside its archive root"):
        ensure_regular_file_within(outside, root)


def test_source_archive_file_collection_rejects_symbolic_links(tmp_path, monkeypatch):
    source_root = tmp_path / "source"
    (source_root / "src").mkdir(parents=True)
    linked_entry = source_root / "src" / "external.py"
    linked_entry.write_text("not a real link in this portable regression fixture", encoding="utf-8")
    original_is_symlink = Path.is_symlink
    monkeypatch.setattr(
        Path,
        "is_symlink",
        lambda path: True if path == linked_entry else original_is_symlink(path),
    )

    with pytest.raises(ValueError, match="symbolic links"):
        files_for_archive(source_root)


def test_source_archive_manifest_includes_archive_path_guard():
    assert "scripts/archive_paths.py" in REQUIRED_ARCHIVE_PATHS
    assert "scripts/run_tests_sharded.py" in REQUIRED_ARCHIVE_PATHS
    assert "scripts/latest_verified_launcher.py" in REQUIRED_ARCHIVE_PATHS
    assert "tests/test_latest_verified_launcher.py" in REQUIRED_ARCHIVE_PATHS


@pytest.mark.parametrize(
    "fixture_template",
    [
        'idn = "FLUKE,8845A,SERIAL,2.0"\n',
        'device = {"serial_number": "SN-SERIAL"}\n',
        'profiles = {"SERIAL": {"outguard_sw": "2.0"}}\n',
    ],
)
def test_source_archive_file_collection_rejects_numeric_hardware_serial_fixtures(
    tmp_path, monkeypatch, fixture_template
):
    source_root = tmp_path / "source"
    tests_dir = source_root / "tests"
    tests_dir.mkdir(parents=True)
    serial_digits = "123" + "4567"
    fixture_text = fixture_template.replace("SERIAL", serial_digits)
    (tests_dir / "test_identity.py").write_text(fixture_text, encoding="utf-8")
    monkeypatch.setattr(package_source, "INCLUDED_ROOT_FILES", ())
    monkeypatch.setattr(package_source, "INCLUDED_TREES", ("tests",))

    with pytest.raises(ValueError, match="synthetic instrument serial"):
        files_for_archive(source_root)


def test_windows_archive_file_collection_rejects_symbolic_links(tmp_path, monkeypatch):
    app_dist = tmp_path / "app.dist"
    app_dist.mkdir()
    linked_entry = app_dist / "external.dll"
    linked_entry.write_bytes(b"not a real link in this portable regression fixture")
    original_is_symlink = Path.is_symlink
    monkeypatch.setattr(
        Path,
        "is_symlink",
        lambda path: True if path == linked_entry else original_is_symlink(path),
    )

    with pytest.raises(ValueError, match="symbolic links"):
        package_windows_release._release_files(app_dist)


def test_windows_portable_archive_uses_portable_quick_start_as_root_readme(tmp_path, monkeypatch):
    root = tmp_path / "checkout"
    docs = root / "docs"
    docs.mkdir(parents=True)
    (root / "README.md").write_text("源码 ZIP 安装说明：运行 Python 启动器。", encoding="utf-8")
    portable_readme = docs / "portable-windows-quick-start.md"
    portable_text = "便携版：解压后双击 Fluke8845A.exe 启动。"
    portable_readme.write_text(portable_text, encoding="utf-8")
    app_dist = tmp_path / "app.dist"
    app_dist.mkdir()
    monkeypatch.setattr(package_windows_release, "ROOT", root)

    readme_entries = [
        (source, member)
        for source, member in package_windows_release._release_files(app_dist)
        if member == "README.md"
    ]

    assert len(readme_entries) == 1
    assert readme_entries[0][0].read_text(encoding="utf-8") == portable_text


def test_windows_release_docs_reject_directory_junction_escape(tmp_path, monkeypatch):
    root = tmp_path / "checkout"
    docs_source = root / "docs"
    junction = docs_source / "external-docs"
    junction.mkdir(parents=True)
    (junction / "private.txt").write_text("private", encoding="utf-8")
    external = tmp_path / "outside"
    external.mkdir()
    destination = tmp_path / "app.dist" / "docs"
    monkeypatch.setattr(package_windows_release, "ROOT", root)
    original_resolve = Path.resolve

    def resolve_with_junction(path, *args, **kwargs):
        if path == junction:
            return external
        return original_resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", resolve_with_junction)

    with pytest.raises(ValueError, match="outside its archive root"):
        package_windows_release._copy_release_docs(docs_source, destination)
    assert not destination.exists()


def test_windows_release_docs_reject_stale_destination_files(tmp_path, monkeypatch):
    root = tmp_path / "checkout"
    docs_source = root / "docs"
    docs_source.mkdir(parents=True)
    (docs_source / "current.md").write_text("current", encoding="utf-8")
    app_dist = tmp_path / "app.dist"
    destination = app_dist / "docs"
    (destination / "old").mkdir(parents=True)
    stale = destination / "old" / "stale.md"
    stale.write_text("stale content", encoding="utf-8")
    monkeypatch.setattr(package_windows_release, "ROOT", root)

    with pytest.raises(ValueError, match="stale files"):
        package_windows_release._copy_release_docs(docs_source, destination)
    assert stale.read_text(encoding="utf-8") == "stale content"


def test_windows_release_docs_preserve_currently_manifested_runtime_licenses(tmp_path, monkeypatch):
    root = tmp_path / "checkout"
    docs_source = root / "docs"
    docs_source.mkdir(parents=True)
    (docs_source / "guide.md").write_text("current guide", encoding="utf-8")
    app_dist = tmp_path / "app.dist"
    docs_target = app_dist / "docs"
    licenses_root = docs_target / "licenses"
    runtime_license = licenses_root / "third-party" / "cpython" / "LICENSE.txt"
    runtime_license.parent.mkdir(parents=True)
    license_bytes = b"current runtime license"
    runtime_license.write_bytes(license_bytes)
    manifest = {
        "format_version": 1,
        "license_files": [{
            "component": "CPython",
            "path": "third-party/cpython/LICENSE.txt",
            "bytes": len(license_bytes),
            "sha256": hashlib.sha256(license_bytes).hexdigest().upper(),
        }],
        "native_binaries": [{
            "path": "PySide6/QtCore.pyd",
            "family": "Qt/PySide/Shiboken",
            "bytes": 8,
            "sha256": hashlib.sha256(b"Qt binary").hexdigest().upper(),
        }],
    }
    (licenses_root / "windows-runtime-license-manifest.json").write_text(
        json.dumps(manifest), encoding="utf-8"
    )
    qt_inventory = licenses_root / "windows-qt-runtime-inventory.csv"
    qt_inventory.write_text(
        "path,family,bytes,sha256\n"
        f"PySide6/QtCore.pyd,Qt/PySide/Shiboken,8,{hashlib.sha256(b'Qt binary').hexdigest().upper()}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(package_windows_release, "ROOT", root)

    package_windows_release._copy_release_docs(docs_source, docs_target)

    assert (docs_target / "guide.md").read_text(encoding="utf-8") == "current guide"
    assert runtime_license.read_bytes() == license_bytes
    assert qt_inventory.is_file()


def test_windows_release_docs_reject_modified_manifested_runtime_license(tmp_path, monkeypatch):
    root = tmp_path / "checkout"
    docs_source = root / "docs"
    docs_source.mkdir(parents=True)
    app_dist = tmp_path / "app.dist"
    docs_target = app_dist / "docs"
    licenses_root = docs_target / "licenses"
    runtime_license = licenses_root / "third-party" / "cpython" / "LICENSE.txt"
    runtime_license.parent.mkdir(parents=True)
    runtime_license.write_bytes(b"tampered runtime license")
    manifest = {
        "format_version": 1,
        "license_files": [{
            "component": "CPython",
            "path": "third-party/cpython/LICENSE.txt",
            "bytes": 100,
            "sha256": "0" * 64,
        }],
    }
    (licenses_root / "windows-runtime-license-manifest.json").write_text(
        json.dumps(manifest), encoding="utf-8"
    )
    monkeypatch.setattr(package_windows_release, "ROOT", root)

    with pytest.raises(ValueError, match="runtime license manifest"):
        package_windows_release._copy_release_docs(docs_source, docs_target)


def test_windows_release_docs_copy_validates_and_copies_current_source_files(tmp_path, monkeypatch):
    root = tmp_path / "checkout"
    docs_source = root / "docs"
    nested_source = docs_source / "guide" / "usage.md"
    nested_source.parent.mkdir(parents=True)
    nested_source.write_text("current docs", encoding="utf-8")
    app_dist = tmp_path / "app.dist"
    app_dist.mkdir()
    destination = app_dist / "docs"
    monkeypatch.setattr(package_windows_release, "ROOT", root)

    package_windows_release._copy_release_docs(docs_source, destination)

    assert (destination / "guide" / "usage.md").read_text(encoding="utf-8") == "current docs"


def test_stable_release_identity_requires_matching_version_tag():
    assert release_identity("1.0.1", "tag", "v1.0.1") == {
        "tag": "v1.0.1",
        "title": "Fluke 8845A Lab v1.0.1",
        "version": "1.0.1",
        "prerelease": False,
    }
    with pytest.raises(ValueError, match="does not match"):
        release_identity("1.0.1", "tag", "v1.0.2")


def test_main_release_identity_rejects_manual_dispatch_from_other_branches():
    with pytest.raises(ValueError, match="only be published from the main branch"):
        release_identity("1.0.1", "branch", "feature/new-ui", "af1db2104318abcd")


def test_release_validator_requires_matching_published_release_and_exact_assets():
    release = {
        "tagName": "main-v1.0.1-build-af1db2104318",
        "isDraft": False,
        "isPrerelease": True,
        "assets": [
            {"name": "Fluke8845A_Source_Project.zip", "size": 1024},
            {"name": "SHA256SUMS.txt", "size": 130},
        ],
    }
    validate_github_release(
        release,
        expected_tag="main-v1.0.1-build-af1db2104318",
        expected_prerelease=True,
        expected_assets={"Fluke8845A_Source_Project.zip": 1024, "SHA256SUMS.txt": 130},
    )


@pytest.mark.parametrize(
    "mutation",
    ["wrong_tag", "draft", "wrong_channel", "missing_asset", "wrong_size", "extra_asset"],
)
def test_release_validator_rejects_partial_or_mismatched_release(mutation):
    release = {
        "tagName": "main-v1.0.1-build-af1db2104318",
        "isDraft": False,
        "isPrerelease": True,
        "assets": [
            {"name": "Fluke8845A_Source_Project.zip", "size": 1024},
            {"name": "SHA256SUMS.txt", "size": 130},
        ],
    }
    expected_tag = "main-v1.0.1-build-af1db2104318"
    expected_prerelease = True
    expected_assets = {"Fluke8845A_Source_Project.zip": 1024, "SHA256SUMS.txt": 130}
    if mutation == "wrong_tag":
        release["tagName"] = "main-v1.0.1-build-000000000000"
    elif mutation == "draft":
        release["isDraft"] = True
    elif mutation == "wrong_channel":
        release["isPrerelease"] = False
    elif mutation == "missing_asset":
        release["assets"].pop()
    elif mutation == "wrong_size":
        release["assets"][0]["size"] += 1
    elif mutation == "extra_asset":
        release["assets"].append({"name": "unrelated.zip", "size": 1})

    with pytest.raises(ValueError):
        validate_github_release(
            release,
            expected_tag=expected_tag,
            expected_prerelease=expected_prerelease,
            expected_assets=expected_assets,
        )


def test_oci_manifest_validator_matches_both_release_files_by_digest():
    manifest = {
        "schemaVersion": 2,
        "artifactType": "application/vnd.fluke8845a.source-bundle.v1",
        "layers": [
            {"mediaType": "application/zip", "digest": "sha256:abc123", "size": 1024},
            {"mediaType": "text/plain", "digest": "sha256:def456", "size": 130},
        ],
    }
    validate_oci_manifest(
        manifest,
        expected_layers={
            "application/zip": "sha256:abc123",
            "text/plain": "sha256:def456",
        },
    )


def test_oci_manifest_validator_accepts_oras_formatted_content_envelope():
    validate_oci_manifest(
        {
            "reference": "ghcr.io/chickensooup/fluke8845a-lab-source@sha256:manifest",
            "content": {
                "schemaVersion": 2,
                "artifactType": "application/vnd.fluke8845a.source-bundle.v1",
                "layers": [
                    {"mediaType": "application/zip", "digest": "sha256:abc123"},
                    {"mediaType": "text/plain", "digest": "sha256:def456"},
                ],
            },
        },
        expected_layers={"application/zip": "sha256:abc123", "text/plain": "sha256:def456"},
    )


@pytest.mark.parametrize(
    "mutation", ["missing_layer", "wrong_digest", "duplicate_type", "extra_layer", "wrong_artifact"]
)
def test_oci_manifest_validator_rejects_incomplete_or_different_package(mutation):
    manifest = {
        "schemaVersion": 2,
        "artifactType": "application/vnd.fluke8845a.source-bundle.v1",
        "layers": [
            {"mediaType": "application/zip", "digest": "sha256:abc123", "size": 1024},
            {"mediaType": "text/plain", "digest": "sha256:def456", "size": 130},
        ],
    }
    expected = {"application/zip": "sha256:abc123", "text/plain": "sha256:def456"}
    if mutation == "missing_layer":
        manifest["layers"].pop()
    elif mutation == "wrong_digest":
        manifest["layers"][0]["digest"] = "sha256:stale"
    elif mutation == "duplicate_type":
        manifest["layers"][1]["mediaType"] = "application/zip"
    elif mutation == "extra_layer":
        manifest["layers"].append({"mediaType": "application/json", "digest": "sha256:other"})
    elif mutation == "wrong_artifact":
        manifest["artifactType"] = "application/vnd.other"

    with pytest.raises(ValueError):
        validate_oci_manifest(manifest, expected_layers=expected)


def test_release_publication_cli_checks_fetched_oci_digests(tmp_path):
    archive = tmp_path / "Fluke8845A_Source_Project.zip"
    checksums = tmp_path / "SHA256SUMS.txt"
    archive.write_bytes(b"source archive")
    checksums.write_text("source hash\n", encoding="utf-8")
    manifest = {
        "reference": "ghcr.io/chickensooup/fluke8845a-lab-source@sha256:manifest",
        "content": {
            "schemaVersion": 2,
            "artifactType": "application/vnd.fluke8845a.source-bundle.v1",
            "layers": [
                {"mediaType": "application/zip", "digest": f"sha256:{hashlib.sha256(archive.read_bytes()).hexdigest()}"},
                {"mediaType": "text/plain", "digest": f"sha256:{hashlib.sha256(checksums.read_bytes()).hexdigest()}"},
            ],
        },
    }
    manifest_path = tmp_path / "oci.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    assert release_publication_main([
        "package", "--json", str(manifest_path), "--archive", str(archive), "--checksums", str(checksums),
    ]) == 0


def test_release_publication_cli_checks_release_tag_status_and_asset_sizes(tmp_path):
    archive = tmp_path / "Fluke8845A_Source_Project.zip"
    checksums = tmp_path / "SHA256SUMS.txt"
    archive.write_bytes(b"source archive")
    checksums.write_text("source hash\n", encoding="utf-8")
    release = {
        "tagName": "main-v1.0.1-build-af1db2104318",
        "isDraft": False,
        "isPrerelease": True,
        "assets": [
            {"name": archive.name, "size": archive.stat().st_size},
            {"name": checksums.name, "size": checksums.stat().st_size},
        ],
    }
    release_path = tmp_path / "release.json"
    release_path.write_text(json.dumps(release), encoding="utf-8")

    assert release_publication_main([
        "release", "--json", str(release_path), "--tag", release["tagName"], "--prerelease", "true",
        "--archive", str(archive), "--checksums", str(checksums),
    ]) == 0


def test_source_and_portable_manifests_require_qt_license_text_and_notice():
    source_expected = {
        "docs/qt-licensing-and-third-party-notices.md",
        "docs/licenses/LGPL-3.0.txt",
        "docs/licenses/windows-portable-review-2026-10-15.md",
        "docs/licenses/windows-qt-runtime-inventory-2026-10-15.csv",
        "docs/licenses/windows-source-offer.md",
    }
    binary_expected = {
        "docs/licenses/windows-runtime-license-manifest.json",
        "docs/licenses/third-party/cpython/LICENSE.txt",
        "docs/licenses/third-party/numpy/LICENSE.txt",
        "docs/licenses/third-party/pyserial/LICENSE.txt",
        "docs/licenses/third-party/openssl/Apache-2.0.txt",
        "docs/licenses/third-party/nuitka-runtime/LICENSE-RUNTIME.txt",
    }
    assert source_expected <= set(REQUIRED_ARCHIVE_PATHS)
    assert source_expected | binary_expected <= REQUIRED_ENTRIES


def test_third_party_notice_distinguishes_source_zip_from_frozen_runtime_bundle():
    notice = (Path(__file__).resolve().parents[1] / "THIRD-PARTY-NOTICES.md").read_text(encoding="utf-8")
    assert "源码 ZIP 不包含第三方运行时二进制文件" in notice
    assert "Windows 便携包会捆绑 CPython" in notice
    assert "第三方运行时二进制文件" in notice
    assert "does not bundle third-party runtime binaries" in notice
    assert "included in the Windows portable package, which bundles third-party runtime binaries" in notice


def test_source_archive_includes_runtime_license_collector_and_sources():
    assert "scripts/collect_windows_runtime_licenses.py" in REQUIRED_ARCHIVE_PATHS
    assert {
        "docs/licenses/third-party/README.md",
        "docs/licenses/third-party/Apache-2.0.txt",
        "docs/licenses/third-party/pyserial-3.5-LICENSE.txt",
    } <= set(REQUIRED_ARCHIVE_PATHS)


def test_source_and_portable_manifests_include_the_localhost_interface():
    assert {
        "Fluke8845A_Web.py",
        "run_web.bat",
        "src/fluke8845a/webapp.py",
        "src/fluke8845a/web/index.html",
    } <= set(REQUIRED_ARCHIVE_PATHS)
    assert {
        "Fluke8845A-Web.dist/Fluke8845A-Web.exe",
        "Fluke8845A-Web.dist/fluke8845a/web/index.html",
        "Fluke8845A-Web.dist/fluke8845a/web/app.css",
        "Fluke8845A-Web.dist/fluke8845a/web/app.js",
        "Fluke8845A-Web.dist/fluke8845a/web/history_worker.js",
    } <= REQUIRED_ENTRIES


def test_portable_package_requires_a_gui_readable_build_identity():
    assert "BUILD-IDENTITY.json" in REQUIRED_ENTRIES


def test_portable_gui_identity_must_match_the_archive_provenance():
    provenance = {
        "candidate_build_name": "Fluke8845A_test",
        "source_tree_sha256": "A" * 64,
        "executables": {"desktop": {"sha256": "B" * 64}},
    }
    validate_runtime_build_identity(provenance, json.loads(json.dumps(provenance)))

    stale_identity = json.loads(json.dumps(provenance))
    stale_identity["source_tree_sha256"] = "C" * 64
    with pytest.raises(ValueError, match="does not match package provenance"):
        validate_runtime_build_identity(stale_identity, provenance)


def test_portable_web_smoke_compares_all_static_resources_and_requires_no_store(monkeypatch):
    web_root = Path(__file__).resolve().parents[1] / "src" / "fluke8845a" / "web"
    expected = {
        "/": (web_root / "index.html").read_bytes(),
        "/app.css": (web_root / "app.css").read_bytes(),
        "/app.js": (web_root / "app.js").read_bytes(),
        "/history_worker.js": (web_root / "history_worker.js").read_bytes(),
    }
    requested = []

    class Response:
        headers = {"Cache-Control": "no-store"}

        def __init__(self, body):
            self.body = body

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return self.body

    def fake_urlopen(url, timeout):
        assert timeout == 1.0
        route = url.removeprefix("http://127.0.0.1:8765")
        requested.append(route)
        return Response(expected[route])

    monkeypatch.setattr(package_windows_release, "urlopen", fake_urlopen)

    package_windows_release.validate_local_web_resources("http://127.0.0.1:8765", web_root)

    assert requested == ["/", "/app.css", "/app.js", "/history_worker.js"]


def test_portable_web_smoke_rejects_missing_no_store_header(monkeypatch):
    web_root = Path(__file__).resolve().parents[1] / "src" / "fluke8845a" / "web"

    class Response:
        headers = {}

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return (web_root / "index.html").read_bytes()

    monkeypatch.setattr(package_windows_release, "urlopen", lambda *_args, **_kwargs: Response())

    with pytest.raises(ValueError, match="Cache-Control: no-store"):
        package_windows_release.validate_local_web_resources("http://127.0.0.1:8765", web_root)


def test_portable_web_smoke_rejects_stale_history_worker_response(monkeypatch):
    web_root = Path(__file__).resolve().parents[1] / "src" / "fluke8845a" / "web"

    class Response:
        headers = {"Cache-Control": "no-store"}

        def __init__(self, body):
            self.body = body

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return self.body

    def fake_urlopen(url, **_kwargs):
        route = url.removeprefix("http://127.0.0.1:8765")
        asset = {
            "/": "index.html",
            "/app.css": "app.css",
            "/app.js": "app.js",
            "/history_worker.js": "history_worker.js",
        }[route]
        body = (web_root / asset).read_bytes()
        if route == "/history_worker.js":
            body += b"\n// stale packaged response\n"
        return Response(body)

    monkeypatch.setattr(package_windows_release, "urlopen", fake_urlopen)

    with pytest.raises(ValueError, match="complete package resource"):
        package_windows_release.validate_local_web_resources("http://127.0.0.1:8765", web_root)


def _validate_packaged_web_resources(html, javascript):
    validator = getattr(package_windows_release, "validate_packaged_web_resources", None)
    assert callable(validator), "packaged web smoke must validate HTML and JavaScript together"
    validator(html, javascript)


def _current_packaged_web_sources():
    web_root = Path(__file__).resolve().parents[1] / "src" / "fluke8845a" / "web"
    return (
        (web_root / "index.html").read_text(encoding="utf-8"),
        (web_root / "app.js").read_text(encoding="utf-8"),
    )


def test_packaged_web_resource_validator_accepts_current_html_and_sampling_interval_code():
    html, javascript = _current_packaged_web_sources()
    _validate_packaged_web_resources(html, javascript)


def test_packaged_web_resource_validator_rejects_stale_sampling_frequency_html():
    html, javascript = _current_packaged_web_sources()
    stale_html = html.replace("实测采样间隔", "采样频率")
    with pytest.raises(ValueError, match="采样间隔"):
        _validate_packaged_web_resources(stale_html, javascript)


@pytest.mark.parametrize(
    "javascript",
    [
        'el("actualRate").textContent = numberText(state.actual_hz);',
        lambda current: current.replace(
            "samplingIntervalText(state.actual_hz)",
            "samplingIntervalText(state.target_interval_s)",
        ),
    ],
    ids=["old-rate-display", "mismatched-target-interval"],
)
def test_packaged_web_resource_validator_rejects_stale_or_mismatched_javascript(javascript):
    html, current_javascript = _current_packaged_web_sources()
    if callable(javascript):
        javascript = javascript(current_javascript)
    with pytest.raises(ValueError, match="采样间隔"):
        _validate_packaged_web_resources(html, javascript)


@pytest.mark.parametrize(
    "archive_prefix",
    ["src/fluke8845a/web", "Fluke8845A-Web.dist/fluke8845a/web"],
    ids=["source-zip", "windows-zip"],
)
def test_release_zip_web_resources_match_current_source_byte_for_byte(tmp_path, archive_prefix):
    web_root = Path(__file__).resolve().parents[1] / "src" / "fluke8845a" / "web"
    archive_path = tmp_path / "release.zip"
    with ZipFile(archive_path, "w") as archive:
        for asset in ("index.html", "app.js", "app.css", "history_worker.js"):
            archive.write(web_root / asset, f"{archive_prefix}/{asset}")

    with ZipFile(archive_path) as archive:
        validate_web_archive_resources(archive, archive_prefix, web_root)


@pytest.mark.parametrize(
    "archive_prefix",
    ["src/fluke8845a/web", "Fluke8845A-Web.dist/fluke8845a/web"],
    ids=["source-zip", "windows-zip"],
)
def test_release_zip_rejects_stale_sampling_copy_even_when_source_is_current(tmp_path, archive_prefix):
    web_root = Path(__file__).resolve().parents[1] / "src" / "fluke8845a" / "web"
    archive_path = tmp_path / "stale-release.zip"
    with ZipFile(archive_path, "w") as archive:
        for asset in ("index.html", "app.js", "app.css", "history_worker.js"):
            content = (web_root / asset).read_bytes()
            if asset == "index.html":
                content = content.replace("实测采样间隔".encode(), "实测采样频率".encode())
            archive.writestr(f"{archive_prefix}/{asset}", content)

    with ZipFile(archive_path) as archive:
        with pytest.raises(ValueError, match="当前源码"):
            validate_web_archive_resources(archive, archive_prefix, web_root)


@pytest.mark.parametrize(
    "archive_prefix",
    ["src/fluke8845a/web", "Fluke8845A-Web.dist/fluke8845a/web"],
    ids=["source-zip", "windows-zip"],
)
def test_release_zip_rejects_stale_history_worker_copy(tmp_path, archive_prefix):
    web_root = Path(__file__).resolve().parents[1] / "src" / "fluke8845a" / "web"
    archive_path = tmp_path / "stale-worker-release.zip"
    with ZipFile(archive_path, "w") as archive:
        for asset in ("index.html", "app.js", "app.css", "history_worker.js"):
            content = (web_root / asset).read_bytes()
            if asset == "history_worker.js":
                content += b"\n// stale package copy\n"
            archive.writestr(f"{archive_prefix}/{asset}", content)

    with ZipFile(archive_path) as archive:
        with pytest.raises(ValueError, match="当前源码"):
            validate_web_archive_resources(archive, archive_prefix, web_root)


def test_source_archive_includes_first_run_windows_launchers():
    assert {
        "run_windows.bat",
        "run_web.bat",
        "scripts/launch_windows.py",
        "scripts/generate_demo.py",
        "docs/release-notes-v1.1.0.md",
        "docs/release-notes-v1.0.7.md",
        "docs/release-notes-v1.0.6.md",
        "docs/release-notes-v1.0.5.md",
        "docs/release-notes-v1.0.4.md",
        "docs/release-notes-v1.0.3.md",
        "docs/release-notes-v1.0.2.md",
        "docs/release-notes-v1.0.1.md",
    } <= set(REQUIRED_ARCHIVE_PATHS)


def test_source_archive_includes_release_notes_for_the_project_version():
    import tomllib

    from fluke8845a import __version__

    root = Path(__file__).resolve().parents[1]
    project_version = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]

    assert __version__ == project_version
    assert f"docs/release-notes-v{project_version}.md" in REQUIRED_ARCHIVE_PATHS
    assert (root / f"docs/release-notes-v{project_version}.md").is_file()


def test_source_archive_manifest_requires_the_history_import_module():
    assert "src/fluke8845a/history_import.py" in REQUIRED_ARCHIVE_PATHS


def test_source_archive_includes_automated_release_workflow_and_guide():
    assert ".github/workflows/publish-release.yml" in REQUIRED_ARCHIVE_PATHS
    assert "scripts/release_metadata.py" in REQUIRED_ARCHIVE_PATHS
    assert "scripts/release_publication.py" in REQUIRED_ARCHIVE_PATHS
    assert "docs/releasing.md" in REQUIRED_ARCHIVE_PATHS


def test_release_workflow_stages_and_verifies_both_publications():
    workflow = (Path(__file__).resolve().parents[1] / ".github/workflows/publish-release.yml").read_text(encoding="utf-8")
    oras_arguments = workflow.split("$orasArguments = @(\n", 1)[1].split("\n          )", 1)[0]
    assert "release-notes-v$($metadata.version).md" in workflow
    assert "          $releaseNotes\n\n          ---\n\n          Automated source-only package" in workflow
    assert workflow.index("GitHub draft Release creation failed before GHCR publish") < workflow.index('"push",')
    assert '"push",' in oras_arguments
    assert '"--disable-path-validation"' in oras_arguments
    assert workflow.index("release_publication.py package") < workflow.index("--draft=false")
    assert workflow.index("--draft=false") < workflow.index("release_publication.py release")
    assert 'throw "GitHub Release lookup failed' in workflow
    assert 'throw "GitHub Release asset upload failed' in workflow


def test_release_workflow_allows_software_tests_to_run_under_job_timeout():
    workflow = (Path(__file__).resolve().parents[1] / ".github/workflows/publish-release.yml").read_text(encoding="utf-8")
    marker = "- name: Run Web frontend JavaScript tests\n"
    assert marker in workflow
    web_step = workflow.split(marker, 1)[1].split("      - name:", 1)[0]
    assert "python scripts/run_web_tests.py" in web_step
    assert "--test-timeout=" not in web_step
    assert "timeout-minutes:" not in web_step

    for marker in ("- name: Run Python 3.11 tests\n", "- name: Run tests\n"):
        step = workflow.split(marker, 1)[1].split("      - name:", 1)[0]
        assert "run_tests_sharded.py" in step
        assert "--timeout-seconds" not in step
        assert "\n        timeout-minutes:" not in step

    assert "timeout-minutes: 10" in workflow
    smoke_marker = "- name: Smoke test first launch from extracted source ZIP\n"
    smoke_step = workflow.split(smoke_marker, 1)[1].split("      - name:", 1)[0]
    assert "\n        timeout-minutes:" not in smoke_step


def test_release_workflow_installs_python_dependencies_before_web_frontend_tests():
    workflow = (Path(__file__).resolve().parents[1] / ".github/workflows/publish-release.yml").read_text(encoding="utf-8")
    install_marker = "- name: Install locked test environment"
    web_marker = "- name: Run Web frontend JavaScript tests\n"
    assert workflow.index(install_marker) < workflow.index(web_marker)
    web_step = workflow.split(web_marker, 1)[1].split("      - name:", 1)[0]
    assert "PYTHON: python" in web_step


def test_release_workflow_uses_node24_compatible_setup_node_action():
    workflow = (Path(__file__).resolve().parents[1] / ".github/workflows/publish-release.yml").read_text(encoding="utf-8")
    marker = "- name: Set up Node.js for Web frontend tests\n"
    setup_step = workflow.split(marker, 1)[1].split("      - name:", 1)[0]
    assert "uses: actions/setup-node@949feb2413d6458794dcd2491c4babbbce0c15c1 # v7.1.0" in setup_step
    assert 'node-version: "22"' in setup_step


def test_source_archive_includes_python311_dependency_lock():
    assert "requirements-lock-python311.txt" in REQUIRED_ARCHIVE_PATHS


def valid_report(executable_sha256="ABCD"):
    modes = ("DCV", "ACV", "DCI", "ACI", "RES2", "RES4")
    return {
        "probe_type": "mock_gui_six_mode_e2e",
        "status": "ok",
        "simulation": True,
        "executable_sha256": executable_sha256,
        "mode_count": len(modes),
        "wheel_zoom_validation": {
            "mode": "DCV",
            "simulation": True,
            "wheel_event_count": 1400,
            "wheel_events_accepted": 1400,
            "initial_span_si": 1.0,
            "final_span_si": 5e-324,
            "precision_limit_reached": True,
            "follow_disabled": True,
            "plot_values_preserved": True,
            "raw_readings_preserved": True,
        },
        "modes": [
            {
                "mode": mode,
                "simulation": True,
                "sample_count": 8,
                "valid_sample_count": 8,
                "files_verified": ["measurements.csv", "config.json", "events.csv", "run.log"],
            }
            for mode in modes
        ],
    }


def test_release_report_validator_accepts_current_complete_mock_report():
    validate_mock_e2e_report(valid_report(), "abcd")


def test_extracted_mock_report_confirms_the_gui_title_matches_packaged_source_identity():
    report = valid_report()
    report["software_version"] = "1.0.7"
    report["window_title"] = "Fluke 8845A 科研测量与长期记录 · v1.0.7 · 构建 3CCF61D7"

    validate_mock_e2e_report(
        report,
        "ABCD",
        expected_source_tree_sha256="3CCF61D761B49A127E1A5EB69415ED780138ECB42CD07E8BEE5A855332C4BC05",
    )

    report["window_title"] = "Fluke 8845A 科研测量与长期记录 · v1.0.7 · 构建 标识缺失"
    with pytest.raises(ValueError, match="GUI title does not identify the packaged source build"):
        validate_mock_e2e_report(
            report,
            "ABCD",
            expected_source_tree_sha256="3CCF61D761B49A127E1A5EB69415ED780138ECB42CD07E8BEE5A855332C4BC05",
        )


def test_archived_mock_report_must_match_provenance_build_identity():
    report = valid_report()
    report["software_version"] = "1.0.7"
    report["window_title"] = "Fluke 8845A 科研测量与长期记录 · v1.0.7 · 构建 标识缺失"
    provenance = {
        "source_tree_sha256": "3CCF61D761B49A127E1A5EB69415ED780138ECB42CD07E8BEE5A855332C4BC05",
    }

    with pytest.raises(ValueError, match="GUI title does not identify the packaged source build"):
        validate_packaged_mock_e2e_report(report, "ABCD", provenance)

    report["window_title"] = "Fluke 8845A 科研测量与长期记录 · v1.0.7 · 构建 3CCF61D7"
    validate_packaged_mock_e2e_report(report, "ABCD", provenance)


def test_release_report_validator_accepts_deepest_representable_zoom_boundary():
    report = valid_report()
    report["wheel_zoom_validation"].update(initial_span_si=1.0, final_span_si=1e-323)

    validate_mock_e2e_report(report, "ABCD")


@pytest.mark.parametrize(
    "mutation",
    [
        "hash", "simulation", "missing_mode", "bad_samples", "missing_file",
        "missing_wheel_zoom", "bad_wheel_zoom", "wide_final_span", "unaccepted_wheel_events",
        "plot_changed", "raw_changed", "insufficient_zoom_depth", "missing_precision_limit",
    ],
)
def test_release_report_validator_rejects_incomplete_or_stale_report(mutation):
    report = valid_report()
    if mutation == "hash":
        report["executable_sha256"] = "STALE"
    elif mutation == "simulation":
        report["modes"][0]["simulation"] = False
    elif mutation == "missing_mode":
        report["modes"].pop()
    elif mutation == "bad_samples":
        report["modes"][0]["valid_sample_count"] = 7
    elif mutation == "missing_file":
        report["modes"][0]["files_verified"].remove("run.log")
    elif mutation == "missing_wheel_zoom":
        report.pop("wheel_zoom_validation")
    elif mutation == "bad_wheel_zoom":
        report["wheel_zoom_validation"]["final_span_si"] = 1e-3
    elif mutation == "wide_final_span":
        report["wheel_zoom_validation"].update(initial_span_si=1e4, final_span_si=1e-3)
    elif mutation == "unaccepted_wheel_events":
        report["wheel_zoom_validation"]["wheel_events_accepted"] = 15
    elif mutation == "insufficient_zoom_depth":
        report["wheel_zoom_validation"].update(
            wheel_event_count=16, wheel_events_accepted=16, final_span_si=1e-7,
        )
    elif mutation == "missing_precision_limit":
        report["wheel_zoom_validation"]["precision_limit_reached"] = False
    elif mutation == "plot_changed":
        report["wheel_zoom_validation"]["plot_values_preserved"] = False
    elif mutation == "raw_changed":
        report["wheel_zoom_validation"]["raw_readings_preserved"] = False

    with pytest.raises(ValueError):
        validate_mock_e2e_report(report, "ABCD")


@pytest.mark.parametrize("component", ["qpdf.dll", "qt6pdf.dll", "QtPdf.pyd"])
def test_portable_release_rejects_unused_qt_pdf_components(component):
    with pytest.raises(ValueError, match="unused Qt PDF components"):
        validate_portable_runtime_components({"Fluke8845A-Web.dist/PySide6/qt-plugins/imageformats/" + component})


def test_portable_release_accepts_runtime_without_qt_pdf_components():
    validate_portable_runtime_components({"Fluke8845A.exe", "PySide6/qt-plugins/imageformats/qjpeg.dll"})


def test_prune_unused_qt_pdf_components_removes_only_forbidden_runtime_files(tmp_path):
    app_dist = tmp_path / "Fluke8845A.dist"
    qpdf_plugin = app_dist / "PySide6" / "qt-plugins" / "imageformats" / "qpdf.dll"
    qt_pdf_library = app_dist / "qt6pdf.dll"
    qt_pdf_binding = app_dist / "PySide6" / "QtPdf.pyd"
    needed_image_plugin = app_dist / "PySide6" / "qt-plugins" / "imageformats" / "qjpeg.dll"
    for path in (qpdf_plugin, qt_pdf_library, qt_pdf_binding, needed_image_plugin):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"generated test component")

    prune = getattr(package_windows_release, "prune_unused_qt_pdf_components", None)
    assert callable(prune), "the Windows build needs a tested Qt PDF cleanup step"

    removed = prune(app_dist)

    assert set(removed) == {
        "PySide6/QtPdf.pyd",
        "PySide6/qt-plugins/imageformats/qpdf.dll",
        "qt6pdf.dll",
    }
    assert not qpdf_plugin.exists()
    assert not qt_pdf_library.exists()
    assert not qt_pdf_binding.exists()
    assert needed_image_plugin.read_bytes() == b"generated test component"


def test_qt_pdf_prune_cli_refuses_paths_outside_dist(tmp_path, monkeypatch):
    release_dist = tmp_path / "dist"
    release_dist.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    qt_pdf_library = outside / "qt6pdf.dll"
    qt_pdf_library.write_bytes(b"leave files outside dist untouched")
    monkeypatch.setattr(package_windows_release, "DIST", release_dist)

    result = package_windows_release.main([
        "--app-dist", str(outside),
        "--prune-unused-qt-pdf",
    ])

    assert result == 2
    assert qt_pdf_library.read_bytes() == b"leave files outside dist untouched"


def test_runtime_license_manifest_verifies_every_copied_file_hash():
    contents = {
        "third-party/cpython/LICENSE.txt": b"PSF license",
        "third-party/pyside6_essentials/LicenseRef-Qt-Commercial.txt": b"Qt commercial ref",
        "third-party/shiboken6/LicenseRef-Qt-Commercial.txt": b"Shiboken commercial ref",
        "third-party/qt-for-python/LGPL-3.0.txt": b"LGPLv3",
    }
    qt_open_item = "Qt/PySide module and third-party notices require final exact-bundle review."
    manifest = {
        "format_version": 1,
        "python_runtime_version": "3.12.10",
        "components": [
            {
                "name": name,
                "declared_license": "LGPL-3.0-only OR GPL-2.0-only OR GPL-3.0-only" if name in {"PySide6_Essentials", "shiboken6"} else "",
                "planned_license_path": "LGPL-3.0-only; review remains open" if name in {"PySide6_Essentials", "shiboken6"} else "",
                "license_files": ["third-party/cpython/LICENSE.txt"] if name == "CPython" else (
                    [f"third-party/{name.casefold()}/LicenseRef-Qt-Commercial.txt", "third-party/qt-for-python/LGPL-3.0.txt"]
                    if name in {"PySide6_Essentials", "shiboken6"} else []
                ),
            }
            for name in ("CPython", "numpy", "pyqtgraph", "PySide6_Essentials", "shiboken6", "pyserial", "OpenSSL", "Nuitka runtime library")
        ],
        "license_files": [{
            "component": "CPython",
            "path": "third-party/cpython/LICENSE.txt",
            "bytes": len(b"PSF license"),
            "sha256": hashlib.sha256(b"PSF license").hexdigest().upper(),
        }] + [
            {
                "component": "Qt test fixture",
                "path": path,
                "bytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest().upper(),
            }
            for path, data in contents.items() if path != "third-party/cpython/LICENSE.txt"
        ],
        "known_open_items": [qt_open_item],
    }
    validate_runtime_license_manifest(manifest, contents)

    missing_qt_license = json.loads(json.dumps(manifest))
    pyside_component = next(item for item in missing_qt_license["components"] if item["name"] == "PySide6_Essentials")
    pyside_component["license_files"] = []
    with pytest.raises(ValueError, match="(?i)missing required license files for PySide6_Essentials"):
        validate_runtime_license_manifest(missing_qt_license, contents)

    missing_qt_review = json.loads(json.dumps(manifest))
    missing_qt_review["known_open_items"] = ["Other review remains open"]
    with pytest.raises(ValueError, match="Qt/PySide review item"):
        validate_runtime_license_manifest(missing_qt_review, contents)

    missing_qt_binding = dict(manifest)
    missing_qt_binding["components"] = [
        component for component in manifest["components"] if component["name"] != "PySide6_Essentials"
    ]
    with pytest.raises(ValueError, match="PySide6_Essentials"):
        validate_runtime_license_manifest(missing_qt_binding, contents)

    manifest["license_files"][0]["sha256"] = "0" * 64
    with pytest.raises(ValueError, match="SHA-256"):
        validate_runtime_license_manifest(manifest, contents)


def test_runtime_license_manifest_binds_exact_native_binary_inventory():
    contents = {
        "third-party/cpython/LICENSE.txt": b"PSF license",
        "third-party/pyside6_essentials/LicenseRef-Qt-Commercial.txt": b"Qt commercial ref",
        "third-party/shiboken6/LicenseRef-Qt-Commercial.txt": b"Shiboken commercial ref",
        "third-party/qt-for-python/LGPL-3.0.txt": b"LGPLv3",
    }
    qt_license_records = [
        {
            "component": "Qt test fixture",
            "path": path,
            "bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest().upper(),
        }
        for path, data in contents.items() if path != "third-party/cpython/LICENSE.txt"
    ]
    native_contents = {
        "Fluke8845A.exe": b"desktop exe",
        "PySide6/QtCore.pyd": b"Qt binding",
    }
    manifest = {
        "format_version": 1,
        "python_runtime_version": "3.12.10",
        "components": [
            {
                "name": name,
                "declared_license": "LGPL-3.0-only OR GPL-2.0-only OR GPL-3.0-only" if name in {"PySide6_Essentials", "shiboken6"} else "",
                "planned_license_path": "LGPL-3.0-only; review remains open" if name in {"PySide6_Essentials", "shiboken6"} else "",
                "license_files": ["third-party/cpython/LICENSE.txt"] if name == "CPython" else (
                    [f"third-party/{name.casefold()}/LicenseRef-Qt-Commercial.txt", "third-party/qt-for-python/LGPL-3.0.txt"]
                    if name in {"PySide6_Essentials", "shiboken6"} else []
                ),
            }
            for name in ("CPython", "numpy", "pyqtgraph", "PySide6_Essentials", "shiboken6", "pyserial", "OpenSSL", "Nuitka runtime library")
        ],
        "license_files": [{
            "component": "CPython",
            "path": "third-party/cpython/LICENSE.txt",
            "bytes": len(b"PSF license"),
            "sha256": hashlib.sha256(b"PSF license").hexdigest().upper(),
        }] + qt_license_records,
        "native_binaries": [
            {
                "path": path,
                "family": "review required",
                "bytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest().upper(),
            }
            for path, data in native_contents.items()
        ],
        "review_status": "partial; license review open",
        "known_open_items": ["Qt/PySide module and third-party notices require final exact-bundle review."],
    }

    validate_runtime_license_manifest(manifest, contents, native_contents)

    for unsafe_path in ("C:/x.dll", "./x.dll"):
        invalid_path_manifest = dict(manifest)
        invalid_path_manifest["native_binaries"] = [dict(record) for record in manifest["native_binaries"]]
        invalid_path_manifest["native_binaries"][0]["path"] = unsafe_path
        with pytest.raises(ValueError, match="unsafe native binary path"):
            validate_runtime_license_manifest(invalid_path_manifest, contents, native_contents)

    unclassified_manifest = dict(manifest)
    unclassified_manifest["native_binaries"] = [dict(record) for record in manifest["native_binaries"]]
    unclassified_manifest["native_binaries"][0]["family"] = "unclassified; review required"
    with pytest.raises(ValueError, match="family is unclassified"):
        validate_runtime_license_manifest(unclassified_manifest, contents, native_contents)

    manifest["native_binaries"][0]["sha256"] = "0" * 64
    with pytest.raises(ValueError, match="native binary SHA-256"):
        validate_runtime_license_manifest(manifest, contents, native_contents)

    manifest["native_binaries"][0]["sha256"] = hashlib.sha256(native_contents[manifest["native_binaries"][0]["path"]]).hexdigest().upper()
    manifest["native_binaries"] = manifest["native_binaries"][:1]
    with pytest.raises(ValueError, match="does not match the package"):
        validate_runtime_license_manifest(manifest, contents, native_contents)


def test_qt_runtime_csv_inventory_must_match_manifest_records():
    validate_inventory = getattr(package_windows_release, "validate_qt_runtime_inventory", None)
    assert callable(validate_inventory), "portable verifier must validate the generated Qt runtime CSV"

    qt_digest = hashlib.sha256(b"Qt binding").hexdigest().upper()
    manifest = {
        "native_binaries": [
            {"path": "Fluke8845A.exe", "family": "Python/Nuitka", "bytes": 9, "sha256": "A" * 64},
            {"path": "PySide6/QtCore.pyd", "family": "Qt/PySide/Shiboken", "bytes": 10, "sha256": qt_digest},
            {"path": "shiboken6/Shiboken.pyd", "family": "Qt/PySide/Shiboken", "bytes": 13, "sha256": "B" * 64},
        ]
    }
    valid_csv = (
        "path,family,bytes,sha256\n"
        f"PySide6/QtCore.pyd,Qt/PySide/Shiboken,10,{qt_digest}\n"
        f"shiboken6/Shiboken.pyd,Qt/PySide/Shiboken,13,{'B' * 64}\n"
    ).encode("utf-8")
    validate_inventory(manifest, valid_csv)

    with pytest.raises(ValueError, match="Qt runtime CSV inventory does not match"):
        validate_inventory(manifest, valid_csv.replace(b"QtCore.pyd", b"QtGui.pyd"))

    with pytest.raises(ValueError, match="Qt runtime CSV inventory does not match"):
        validate_inventory(manifest, valid_csv.replace(b"Qt/PySide/Shiboken,10", b"Qt/PySide/Shiboken,11"))

    differently_cased_duplicate = dict(manifest["native_binaries"][1])
    differently_cased_duplicate["path"] = differently_cased_duplicate["path"].lower()
    duplicate_manifest = {
        "native_binaries": [manifest["native_binaries"][1], differently_cased_duplicate]
    }
    duplicate_csv = (
        "path,family,bytes,sha256\n"
        f"PySide6/QtCore.pyd,Qt/PySide/Shiboken,10,{qt_digest}\n"
    ).encode("utf-8")
    with pytest.raises(ValueError, match="repeats a native binary path"):
        validate_inventory(duplicate_manifest, duplicate_csv)

    alias_manifest = {
        "native_binaries": [
            manifest["native_binaries"][1],
            {**manifest["native_binaries"][1], "path": "PySide6/./QtCore.pyd"},
        ]
    }
    alias_csv = (
        "path,family,bytes,sha256\n"
        f"PySide6/QtCore.pyd,Qt/PySide/Shiboken,10,{qt_digest}\n"
        f"PySide6/./QtCore.pyd,Qt/PySide/Shiboken,10,{qt_digest}\n"
    ).encode("utf-8")
    with pytest.raises(ValueError, match="non-canonical native binary path"):
        validate_inventory(alias_manifest, alias_csv)
