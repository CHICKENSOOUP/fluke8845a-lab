import json
import csv
from pathlib import Path

import pytest

from scripts.collect_windows_runtime_licenses import collect_runtime_license_materials


def write_distribution(
    site_packages: Path,
    name: str,
    version: str,
    licenses: dict[str, str],
    metadata_headers: str = "",
) -> None:
    dist_info = site_packages / f"{name}-{version}.dist-info"
    dist_info.mkdir(parents=True)
    (dist_info / "METADATA").write_text(
        f"Metadata-Version: 2.4\nName: {name}\nVersion: {version}\n{metadata_headers}", encoding="utf-8"
    )
    for relative, content in licenses.items():
        target = dist_info / "licenses" / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")


def make_license_sources(tmp_path: Path) -> tuple[Path, Path, Path]:
    python_root = tmp_path / "python-root"
    python_root.mkdir()
    (python_root / "LICENSE.txt").write_text("CPython license text", encoding="utf-8")

    site_packages = tmp_path / "site-packages"
    site_packages.mkdir()
    write_distribution(site_packages, "numpy", "2.5.4", {
        "LICENSE.txt": "NumPy and OpenBLAS notices",
        "numpy/vendor/LICENSE.txt": "Nested vendored license",
    })
    write_distribution(site_packages, "pyqtgraph", "0.14.0", {"LICENSE.txt": "pyqtgraph MIT license"})
    write_distribution(site_packages, "PySide6_Essentials", "6.12.0", {
        "LicenseRef-Qt-Commercial.txt": "Qt commercial terms reference",
    }, metadata_headers="License: LGPL-3.0-only OR GPL-2.0-only OR GPL-3.0-only\nLicense-File: LicenseRef-Qt-Commercial.txt\n")
    write_distribution(site_packages, "shiboken6", "6.12.0", {
        "LicenseRef-Qt-Commercial.txt": "Qt commercial terms reference",
    }, metadata_headers="License: LGPL-3.0-only OR GPL-2.0-only OR GPL-3.0-only\nLicense-File: LicenseRef-Qt-Commercial.txt\n")
    write_distribution(site_packages, "pyserial", "3.5", {})
    write_distribution(site_packages, "Nuitka", "4.1.1", {
        "LICENSE.txt": "Nuitka AGPL license",
        "LICENSE-RUNTIME.txt": "Nuitka runtime exception",
        "NOTICE.txt": "Nuitka notice",
    })
    cet_license = site_packages / "pyqtgraph" / "colors" / "maps" / "CC-BY license - applies to CET color map data.txt"
    cet_license.parent.mkdir(parents=True)
    cet_license.write_text("CET attribution", encoding="utf-8")

    project_root = tmp_path / "project"
    vendor_sources = project_root / "docs" / "licenses" / "third-party"
    vendor_sources.mkdir(parents=True)
    (vendor_sources / "pyserial-3.5-LICENSE.txt").write_text("pySerial BSD license", encoding="utf-8")
    (vendor_sources / "Apache-2.0.txt").write_text("Apache license", encoding="utf-8")
    (project_root / "docs" / "licenses" / "LGPL-3.0.txt").write_text("LGPLv3 license", encoding="utf-8")
    return python_root, site_packages, project_root


def test_collect_runtime_licenses_copies_exact_materials_and_hash_manifest(tmp_path):
    python_root, site_packages, project_root = make_license_sources(tmp_path)
    output_root = tmp_path / "build" / "docs" / "licenses"
    binary_root = tmp_path / "build" / "Fluke8845A.dist"
    (binary_root / "PySide6").mkdir(parents=True)
    (binary_root / "Fluke8845A.exe").write_bytes(b"MZ desktop executable")
    (binary_root / "PySide6" / "QtCore.pyd").write_bytes(b"MZ Qt binding")
    (binary_root / "vcruntime140.dll").write_bytes(b"MZ Microsoft runtime")

    manifest_path = collect_runtime_license_materials(
        python_root=python_root,
        site_packages=site_packages,
        project_root=project_root,
        output_root=output_root,
        binary_root=binary_root,
        python_version="3.12.10",
    )

    assert (output_root / "third-party" / "cpython" / "LICENSE.txt").read_text(encoding="utf-8") == "CPython license text"
    assert (output_root / "third-party" / "numpy" / "numpy" / "vendor" / "LICENSE.txt").is_file()
    assert (output_root / "third-party" / "nuitka-runtime" / "LICENSE-RUNTIME.txt").is_file()
    assert (output_root / "third-party" / "pyside6_essentials" / "LicenseRef-Qt-Commercial.txt").read_text(encoding="utf-8") == "Qt commercial terms reference"
    assert (output_root / "third-party" / "qt-for-python" / "LGPL-3.0.txt").read_text(encoding="utf-8") == "LGPLv3 license"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["python_runtime_version"] == "3.12.10"
    assert manifest["review_status"] == (
        "partial; see docs/licenses/windows-portable-review-2026-10-20.md"
    )
    assert manifest["known_open_items"] == [
        "Qt/PySide module and third-party notices require final exact-bundle review.",
        "Qt/PySide corresponding source, including applicable wheel patches and provenance, requires confirmation.",
        "Qt/PySide library replacement and relaunch have not been verified against this exact package.",
        "Microsoft Visual C++ runtime source and applicable redistribution terms require confirmation.",
    ]
    assert {component["name"] for component in manifest["components"]} == {
        "CPython", "numpy", "pyqtgraph", "PySide6_Essentials", "shiboken6", "pyserial", "OpenSSL", "Nuitka runtime library",
    }
    pyside = next(component for component in manifest["components"] if component["name"] == "PySide6_Essentials")
    assert pyside["declared_license"] == "LGPL-3.0-only OR GPL-2.0-only OR GPL-3.0-only"
    assert pyside["declared_license_file"] == "LicenseRef-Qt-Commercial.txt"
    assert all(not Path(record["path"]).is_absolute() for record in manifest["license_files"])
    native = {record["path"]: record for record in manifest["native_binaries"]}
    assert set(native) == {"Fluke8845A.exe", "PySide6/QtCore.pyd", "vcruntime140.dll"}
    assert native["PySide6/QtCore.pyd"]["family"] == "Qt/PySide/Shiboken"
    assert native["vcruntime140.dll"]["family"] == "Microsoft Visual C++ runtime"
    assert native["Fluke8845A.exe"]["family"] == "Fluke8845A application executable"
    with (output_root / "windows-qt-runtime-inventory.csv").open(encoding="utf-8", newline="") as handle:
        inventory = list(csv.DictReader(handle))
    assert len(inventory) == 1
    assert inventory[0]["path"] == "PySide6/QtCore.pyd"
    assert inventory[0]["family"] == "Qt/PySide/Shiboken"
    assert inventory[0]["bytes"] == str(len(b"MZ Qt binding"))
    assert inventory[0]["sha256"] == native["PySide6/QtCore.pyd"]["sha256"]


def test_collector_rejects_wrong_pyserial_license_version(tmp_path):
    python_root, site_packages, project_root = make_license_sources(tmp_path)
    binary_root = tmp_path / "build" / "Fluke8845A.dist"
    binary_root.mkdir(parents=True)
    metadata = next(site_packages.glob("pyserial-*.dist-info")) / "METADATA"
    metadata.write_text("Metadata-Version: 2.4\nName: pyserial\nVersion: 3.6\n", encoding="utf-8")

    with pytest.raises(ValueError, match="cannot cover pyserial 3.6"):
        collect_runtime_license_materials(
            python_root=python_root,
            site_packages=site_packages,
            project_root=project_root,
            output_root=tmp_path / "build" / "docs" / "licenses",
            binary_root=binary_root,
            python_version="3.12.10",
        )
