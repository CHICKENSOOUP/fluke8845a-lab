import pytest

from scripts.package_source import REQUIRED_ARCHIVE_PATHS
from scripts.package_windows_release import (
    REQUIRED_ENTRIES,
    validate_mock_e2e_report,
    validate_portable_runtime_components,
)


def test_source_and_portable_manifests_require_qt_license_text_and_notice():
    expected = {
        "docs/qt-licensing-and-third-party-notices.md",
        "docs/licenses/LGPL-3.0.txt",
        "docs/licenses/windows-portable-review-2026-10-15.md",
        "docs/licenses/windows-qt-runtime-inventory-2026-10-15.csv",
        "docs/licenses/windows-source-offer.md",
    }
    assert expected <= set(REQUIRED_ARCHIVE_PATHS)
    assert expected <= REQUIRED_ENTRIES


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
    } <= REQUIRED_ENTRIES


def test_source_archive_includes_first_run_windows_launchers():
    assert {
        "run_windows.bat",
        "run_web.bat",
        "scripts/launch_windows.py",
        "docs/release-notes-v1.0.1.md",
    } <= set(REQUIRED_ARCHIVE_PATHS)


def test_source_archive_includes_automated_release_workflow_and_guide():
    assert ".github/workflows/publish-release.yml" in REQUIRED_ARCHIVE_PATHS
    assert "docs/releasing.md" in REQUIRED_ARCHIVE_PATHS


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
