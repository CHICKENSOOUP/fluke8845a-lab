import hashlib
import json
from pathlib import Path
from zipfile import ZipFile

import pytest

from scripts import package_windows_release
from scripts.archive_paths import ensure_regular_file_within
from scripts.package_source import REQUIRED_ARCHIVE_PATHS, files_for_archive, validate_web_archive_resources
from scripts.package_windows_release import (
    REQUIRED_ENTRIES,
    validate_mock_e2e_report,
    validate_portable_runtime_components,
    validate_runtime_license_manifest,
)
from scripts.release_metadata import release_identity
from scripts.release_publication import (
    main as release_publication_main,
    validate_github_release,
    validate_oci_manifest,
)


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
    }
    (licenses_root / "windows-runtime-license-manifest.json").write_text(
        json.dumps(manifest), encoding="utf-8"
    )
    monkeypatch.setattr(package_windows_release, "ROOT", root)

    package_windows_release._copy_release_docs(docs_source, docs_target)

    assert (docs_target / "guide.md").read_text(encoding="utf-8") == "current guide"
    assert runtime_license.read_bytes() == license_bytes


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
        for asset in ("index.html", "app.js", "app.css"):
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
        for asset in ("index.html", "app.js", "app.css"):
            content = (web_root / asset).read_bytes()
            if asset == "index.html":
                content = content.replace("实测采样间隔".encode(), "实测采样频率".encode())
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
        "docs/release-notes-v1.0.1.md",
    } <= set(REQUIRED_ARCHIVE_PATHS)


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
    assert workflow.index("GitHub draft Release creation failed before GHCR publish") < workflow.index('"push",')
    assert '"push",' in oras_arguments
    assert '"--disable-path-validation"' in oras_arguments
    assert workflow.index("release_publication.py package") < workflow.index("--draft=false")
    assert workflow.index("--draft=false") < workflow.index("release_publication.py release")
    assert 'throw "GitHub Release lookup failed' in workflow
    assert 'throw "GitHub Release asset upload failed' in workflow


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


@pytest.mark.parametrize("mutation", ["hash", "simulation", "missing_mode", "bad_samples", "missing_file"])
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

    with pytest.raises(ValueError):
        validate_mock_e2e_report(report, "ABCD")


@pytest.mark.parametrize("component", ["qpdf.dll", "qt6pdf.dll", "QtPdf.pyd"])
def test_portable_release_rejects_unused_qt_pdf_components(component):
    with pytest.raises(ValueError, match="unused Qt PDF components"):
        validate_portable_runtime_components({"Fluke8845A-Web.dist/PySide6/qt-plugins/imageformats/" + component})


def test_portable_release_accepts_runtime_without_qt_pdf_components():
    validate_portable_runtime_components({"Fluke8845A.exe", "PySide6/qt-plugins/imageformats/qjpeg.dll"})


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
