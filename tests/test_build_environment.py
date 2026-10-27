from __future__ import annotations

import importlib

import pytest


def _environment_module():
    try:
        return importlib.import_module("scripts.validate_build_environment")
    except ModuleNotFoundError:
        pytest.fail("build environment lock validation is not implemented")


def test_lock_validation_accepts_matching_pins_and_reports_extra_packages(tmp_path):
    module = _environment_module()
    lock = tmp_path / "requirements-lock.txt"
    lock.write_text("# verified snapshot\nPySide6_Essentials==6.12.0\npyserial==3.5\n", encoding="utf-8")

    report = module.validate_lock_contents(
        lock.read_text(encoding="utf-8"),
        installed={"pyside6-essentials": "6.12.0", "pyserial": "3.5", "pytest": "9.1.1"},
    )

    assert report == {
        "locked": {"pyserial": "3.5", "pyside6-essentials": "6.12.0"},
        "extra": {"pytest": "9.1.1"},
    }


@pytest.mark.parametrize(
    ("installed", "message"),
    [
        ({"numpy": "2.5.4"}, "missing locked dependency: pyserial"),
        ({"numpy": "2.5.4", "pyserial": "3.4"}, "version mismatch for pyserial: locked 3.5, installed 3.4"),
    ],
)
def test_lock_validation_rejects_missing_or_drifted_packages(installed, message):
    module = _environment_module()

    with pytest.raises(ValueError, match=message):
        module.validate_lock_contents("numpy==2.5.4\npyserial==3.5\n", installed=installed)


def test_build_runtime_validation_accepts_later_cpython_312_patch_on_windows_x64():
    module = _environment_module()

    module.validate_runtime(
        implementation="CPython",
        version=(3, 12, 14),
        system="Windows",
        machine="AMD64",
        bits=64,
    )


@pytest.mark.parametrize(
    ("runtime", "message"),
    [
        ({"implementation": "PyPy", "version": (3, 12, 10), "system": "Windows", "machine": "AMD64", "bits": 64}, "CPython 3.12"),
        ({"implementation": "CPython", "version": (3, 13, 0), "system": "Windows", "machine": "AMD64", "bits": 64}, "CPython 3.12"),
        ({"implementation": "CPython", "version": (3, 12, 10), "system": "Linux", "machine": "x86_64", "bits": 64}, "Windows x64"),
        ({"implementation": "CPython", "version": (3, 12, 10), "system": "Windows", "machine": "AMD64", "bits": 32}, "64-bit"),
    ],
)
def test_build_runtime_validation_rejects_unsupported_python_or_platform(runtime, message):
    module = _environment_module()

    with pytest.raises(ValueError, match=message):
        module.validate_runtime(**runtime)


def test_build_environment_cli_reports_a_missing_locked_package(
    tmp_path, monkeypatch, capsys,
):
    lock = tmp_path / "requirements-lock.txt"
    lock.write_text("package-that-is-not-installed-for-fluke-test==1.0\n", encoding="utf-8")
    module = _environment_module()
    # The CLI's lock-validation behavior is independent of its deliberately
    # narrower CPython 3.12 Windows-x64 build-runtime gate. Stub that gate so
    # this command-path test remains valid on every supported test interpreter.
    monkeypatch.setattr(module, "validate_current_runtime", lambda: None)
    # Isolate the absent sentinel package from any local editable installs.
    monkeypatch.setattr(module, "installed_distribution_versions", lambda: {})

    exit_code = module.main(["--lock", str(lock)])
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "missing locked dependency: package-that-is-not-installed-for-fluke-test" in captured.err


def test_build_provenance_validator_rejects_malformed_installed_package_inventory():
    provenance = importlib.import_module("scripts.build_provenance")
    document = {
        "format_version": 1,
        "candidate_build_name": "Fluke8845A_environment_test",
        "built_at_utc": "2026-10-21T00:00:00Z",
        "head_commit": "1" * 40,
        "worktree_dirty": False,
        "build_source_kind": "commit_tree",
        "source_tree_sha256": "A" * 64,
        "source_tree_digest_algorithm": provenance.SOURCE_TREE_DIGEST_ALGORITHM,
        "executables": {
            "desktop": {"archive_path": "Fluke8845A.exe", "sha256": "B" * 64},
            "web": {"archive_path": "Fluke8845A-Web.dist/Fluke8845A-Web.exe", "sha256": "C" * 64},
        },
        "installed_distributions": [{"name": "numpy", "version": "2.5.4"}, {"name": "pyserial"}],
    }

    with pytest.raises(ValueError, match="installed package inventory"):
        provenance.validate_provenance(document, expected_candidate_build_name="Fluke8845A_environment_test")
