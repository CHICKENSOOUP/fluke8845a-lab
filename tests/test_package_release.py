import pytest

from scripts.package_source import REQUIRED_ARCHIVE_PATHS
from scripts.package_windows_release import REQUIRED_ENTRIES, validate_mock_e2e_report


def test_source_and_portable_manifests_require_qt_license_text_and_notice():
    expected = {
        "docs/Qt_许可与第三方声明.md",
        "docs/licenses/LGPL-3.0.txt",
    }
    assert expected <= set(REQUIRED_ARCHIVE_PATHS)
    assert expected <= REQUIRED_ENTRIES


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
