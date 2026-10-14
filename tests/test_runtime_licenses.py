import json
from pathlib import Path

import pytest

from scripts.collect_windows_runtime_licenses import collect_runtime_license_materials


def write_distribution(site_packages: Path, name: str, version: str, licenses: dict[str, str]) -> None:
    dist_info = site_packages / f"{name}-{version}.dist-info"
    dist_info.mkdir(parents=True)
    (dist_info / "METADATA").write_text(f"Metadata-Version: 2.4\nName: {name}\nVersion: {version}\n", encoding="utf-8")
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
    return python_root, site_packages, project_root


def test_collect_runtime_licenses_copies_exact_materials_and_hash_manifest(tmp_path):
    python_root, site_packages, project_root = make_license_sources(tmp_path)
    output_root = tmp_path / "build" / "docs" / "licenses"

    manifest_path = collect_runtime_license_materials(
        python_root=python_root,
        site_packages=site_packages,
        project_root=project_root,
        output_root=output_root,
        python_version="3.12.10",
    )

    assert (output_root / "third-party" / "cpython" / "LICENSE.txt").read_text(encoding="utf-8") == "CPython license text"
    assert (output_root / "third-party" / "numpy" / "numpy" / "vendor" / "LICENSE.txt").is_file()
    assert (output_root / "third-party" / "nuitka-runtime" / "LICENSE-RUNTIME.txt").is_file()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["python_runtime_version"] == "3.12.10"
    assert {component["name"] for component in manifest["components"]} == {
        "CPython", "numpy", "pyqtgraph", "pyserial", "OpenSSL", "Nuitka runtime library",
    }
    assert all(not Path(record["path"]).is_absolute() for record in manifest["license_files"])


def test_collector_rejects_wrong_pyserial_license_version(tmp_path):
    python_root, site_packages, project_root = make_license_sources(tmp_path)
    metadata = next(site_packages.glob("pyserial-*.dist-info")) / "METADATA"
    metadata.write_text("Metadata-Version: 2.4\nName: pyserial\nVersion: 3.6\n", encoding="utf-8")

    with pytest.raises(ValueError, match="cannot cover pyserial 3.6"):
        collect_runtime_license_materials(
            python_root=python_root,
            site_packages=site_packages,
            project_root=project_root,
            output_root=tmp_path / "build" / "docs" / "licenses",
            python_version="3.12.10",
        )
