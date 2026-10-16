"""Copy exact third-party license materials into a frozen Windows build."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import ssl
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _normalized_distribution_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).casefold()


def _distribution(site_packages: Path, expected_name: str) -> tuple[Path, dict[str, str]]:
    expected = _normalized_distribution_name(expected_name)
    matches: list[Path] = []
    for candidate in site_packages.glob("*.dist-info"):
        metadata = candidate / "METADATA"
        if not metadata.is_file():
            continue
        fields: dict[str, str] = {}
        for line in metadata.read_text(encoding="utf-8", errors="replace").splitlines():
            if ":" in line:
                key, value = line.split(":", 1)
                fields.setdefault(key.strip().casefold(), value.strip())
        if _normalized_distribution_name(fields.get("name", "")) == expected:
            matches.append(candidate)
    if len(matches) != 1:
        raise ValueError(f"Expected one {expected_name} distribution in {site_packages}, found {len(matches)}")
    fields = {}
    for line in (matches[0] / "METADATA").read_text(encoding="utf-8", errors="replace").splitlines():
        if ":" in line:
            key, value = line.split(":", 1)
            fields.setdefault(key.strip().casefold(), value.strip())
    if not fields.get("version"):
        raise ValueError(f"{expected_name} METADATA does not declare a version")
    return matches[0], fields


def _copy_file(source: Path, target: Path, component: str, source_label: str, license_records: list[dict]) -> None:
    if not source.is_file():
        raise ValueError(f"Required license material is missing: {source_label}")
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)
    data = target.read_bytes()
    license_records.append({
        "component": component,
        "source": source_label,
        "path": target.as_posix(),
        "bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest().upper(),
    })


def _copy_license_tree(
    distribution: Path,
    target_root: Path,
    component: str,
    license_records: list[dict],
) -> list[str]:
    source_root = distribution / "licenses"
    files = sorted(path for path in source_root.rglob("*") if path.is_file()) if source_root.is_dir() else []
    if not files:
        raise ValueError(f"Distribution has no bundled license files: {distribution.name}")
    copied: list[str] = []
    for source in files:
        relative = source.relative_to(source_root)
        target = target_root / relative
        _copy_file(source, target, component, f"{distribution.name}/licenses/{relative.as_posix()}", license_records)
        copied.append(target.as_posix())
    return copied


def _native_binary_family(path: Path) -> str:
    relative = path.as_posix().casefold()
    basename = path.name.casefold()
    if basename.startswith(("msvcp", "vcruntime", "concrt", "ucrtbase", "api-ms-win-crt")):
        return "Microsoft Visual C++ runtime"
    if "pyside6" in relative or "shiboken6" in relative or basename.startswith(("qt6", "pyside6", "shiboken6")):
        return "Qt/PySide/Shiboken"
    if "numpy" in relative or basename.startswith(("libopenblas", "openblas")):
        return "NumPy/OpenBLAS"
    if "openssl" in relative or basename.startswith(("libcrypto", "libssl")):
        return "OpenSSL"
    if "nuitka" in relative:
        return "Nuitka"
    if basename in {"fluke8845a.exe", "fluke8845a-web.exe"}:
        return "Fluke8845A application executable"
    if basename.startswith("libffi-"):
        return "CPython libffi dependency"
    if path.suffix.casefold() == ".pyd":
        return "CPython extension module"
    if "python" in relative or basename.startswith("python"):
        return "CPython"
    return "unclassified; review required"


def collect_runtime_license_materials(
    *,
    python_root: Path,
    site_packages: Path,
    project_root: Path,
    output_root: Path,
    binary_root: Path,
    python_version: str,
) -> Path:
    """Copy bundled-runtime notices and write a content-addressed manifest."""
    python_root = python_root.resolve()
    site_packages = site_packages.resolve()
    project_root = project_root.resolve()
    output_root = output_root.resolve()
    binary_root = binary_root.resolve()
    for label, path in (("Python runtime", python_root), ("site-packages", site_packages), ("project", project_root), ("frozen binary root", binary_root)):
        if not path.is_dir():
            raise ValueError(f"{label} directory does not exist: {path}")

    licenses_root = output_root / "third-party"
    records: list[dict] = []
    components: list[dict] = []

    cpython_license = licenses_root / "cpython" / "LICENSE.txt"
    _copy_file(python_root / "LICENSE.txt", cpython_license, "CPython", "python-root/LICENSE.txt", records)
    components.append({"name": "CPython", "version": python_version, "license_files": [cpython_license.as_posix()]})

    for name, directory_name in (("numpy", "numpy"), ("pyqtgraph", "pyqtgraph")):
        distribution, metadata = _distribution(site_packages, name)
        copied = _copy_license_tree(distribution, licenses_root / directory_name, name, records)
        if name == "pyqtgraph":
            cet_license = site_packages / "pyqtgraph" / "colors" / "maps" / "CC-BY license - applies to CET color map data.txt"
            target = licenses_root / "pyqtgraph" / "CET-color-map-CC-BY.txt"
            _copy_file(cet_license, target, "pyqtgraph CET color maps", "pyqtgraph/colors/maps/CC-BY license - applies to CET color map data.txt", records)
            copied.append(target.as_posix())
        components.append({"name": metadata["name"], "version": metadata["version"], "license_files": copied})

    qt_for_python_license = licenses_root / "qt-for-python" / "LGPL-3.0.txt"
    _copy_file(
        project_root / "docs" / "licenses" / "LGPL-3.0.txt",
        qt_for_python_license,
        "Qt for Python planned LGPLv3 path",
        "docs/licenses/LGPL-3.0.txt (planned path; not a legal clearance)",
        records,
    )
    for name in ("PySide6_Essentials", "shiboken6"):
        distribution, metadata = _distribution(site_packages, name)
        copied = _copy_license_tree(distribution, licenses_root / name.casefold(), name, records)
        copied.append(qt_for_python_license.as_posix())
        components.append({
            "name": metadata["name"],
            "version": metadata["version"],
            "declared_license": metadata.get("license", ""),
            "declared_license_file": metadata.get("license-file", ""),
            "planned_license_path": "LGPL-3.0-only; review remains open",
            "license_files": copied,
        })

    pyserial_distribution, pyserial_metadata = _distribution(site_packages, "pyserial")
    if pyserial_metadata["version"] != "3.5":
        raise ValueError(f"The vendored pySerial 3.5 license cannot cover pyserial {pyserial_metadata['version']}")
    pyserial_license = licenses_root / "pyserial" / "LICENSE.txt"
    _copy_file(
        project_root / "docs" / "licenses" / "third-party" / "pyserial-3.5-LICENSE.txt",
        pyserial_license,
        "pySerial",
        "docs/licenses/third-party/pyserial-3.5-LICENSE.txt (upstream v3.5)",
        records,
    )
    components.append({"name": pyserial_metadata["name"], "version": pyserial_metadata["version"], "license_files": [pyserial_license.as_posix()]})

    openssl_license = licenses_root / "openssl" / "Apache-2.0.txt"
    _copy_file(
        project_root / "docs" / "licenses" / "third-party" / "Apache-2.0.txt",
        openssl_license,
        "OpenSSL 3.x",
        "docs/licenses/third-party/Apache-2.0.txt (Apache License 2.0)",
        records,
    )
    openssl_parts = ssl.OPENSSL_VERSION.split()
    if len(openssl_parts) < 2 or openssl_parts[0] != "OpenSSL" or not openssl_parts[1].startswith("3."):
        raise ValueError(f"Expected an OpenSSL 3.x runtime, got {ssl.OPENSSL_VERSION}")
    components.append({"name": "OpenSSL", "version": openssl_parts[1], "license_files": [openssl_license.as_posix()]})

    nuitka_distribution, nuitka_metadata = _distribution(site_packages, "Nuitka")
    nuitka_target = licenses_root / "nuitka-runtime"
    nuitka_copied = []
    for filename in ("LICENSE.txt", "LICENSE-RUNTIME.txt", "NOTICE.txt"):
        target = nuitka_target / filename
        _copy_file(
            nuitka_distribution / "licenses" / filename,
            target,
            "Nuitka compiled runtime",
            f"{nuitka_distribution.name}/licenses/{filename}",
            records,
        )
        nuitka_copied.append(target.as_posix())
    components.append({"name": "Nuitka runtime library", "version": nuitka_metadata["version"], "license_files": nuitka_copied})

    native_binaries = []
    for binary in sorted(path for path in binary_root.rglob("*") if path.is_file() and path.suffix.casefold() in {".dll", ".pyd", ".exe"}):
        data = binary.read_bytes()
        native_binaries.append({
            "path": binary.relative_to(binary_root).as_posix(),
            "family": _native_binary_family(binary.relative_to(binary_root)),
            "bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest().upper(),
        })
    if not native_binaries:
        raise ValueError(f"No Windows native binaries found under {binary_root}")

    for record in records:
        record["path"] = Path(record["path"]).relative_to(output_root).as_posix()
    for component in components:
        component["license_files"] = [Path(path).relative_to(output_root).as_posix() for path in component["license_files"]]

    manifest_path = output_root / "windows-runtime-license-manifest.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest = {
        "format_version": 1,
        "python_runtime_version": python_version,
        "review_status": "partial; see docs/licenses/windows-portable-review-2026-10-15.md",
        "components": components,
        "license_files": sorted(records, key=lambda row: row["path"]),
        "native_binary_scope": "All .dll, .pyd, and .exe files present in the frozen application directory at collection time; family labels are inventory hints, not license conclusions.",
        "native_binaries": native_binaries,
        "known_open_items": [
            "Qt/PySide module and third-party notices require final exact-bundle review.",
            "Microsoft Visual C++ redistribution source and applicable license terms require confirmation.",
        ],
    }
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return manifest_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--python-root", type=Path, required=True)
    parser.add_argument("--site-packages", type=Path, required=True)
    parser.add_argument("--project-root", type=Path, default=ROOT)
    parser.add_argument("--output-root", type=Path, required=True, help="destination docs/licenses directory inside the build")
    parser.add_argument("--binary-root", type=Path, required=True, help="complete frozen application .dist directory")
    parser.add_argument("--python-version", required=True)
    args = parser.parse_args(argv)
    try:
        manifest = collect_runtime_license_materials(
            python_root=args.python_root,
            site_packages=args.site_packages,
            project_root=args.project_root,
            output_root=args.output_root,
            binary_root=args.binary_root,
            python_version=args.python_version,
        )
    except (OSError, ValueError) as exc:
        parser.exit(2, f"Could not collect exact runtime license materials: {exc}\n")
    print(f"Created runtime license manifest {manifest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
