"""Assemble a portable Windows package from an explicit public-release allowlist."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from urllib.error import URLError
from urllib.request import Request, urlopen
from pathlib import Path, PurePosixPath
from zipfile import BadZipFile, ZIP_DEFLATED, ZipFile

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.archive_paths import ensure_path_within, ensure_regular_file_within
from scripts.build_provenance import (
    BUILD_PROVENANCE_NAME,
    WINDOWS_ARCHIVE_SUFFIX,
    candidate_name_from_archive,
    validate_provenance,
    validate_windows_archive_provenance,
)
from scripts.package_source import validate_packaged_web_resources, validate_web_archive_resources


DIST = ROOT / "dist"
APP_DIST = DIST / "Fluke8845A.dist"
ARCHIVE = DIST / "Fluke8845A_Windows_x64.zip"
MOCK_E2E_REPORT = "output/mock_validation/latest_frozen_exe_six_mode_e2e.json"
MOCK_E2E_MODES = {"DCV", "ACV", "DCI", "ACI", "RES2", "RES4"}
UNSUPPORTED_QT_PDF_BASENAMES = {"qpdf.dll", "qt6pdf.dll", "qtpdf.pyd"}
REQUIRED_ENTRIES = {
    "Fluke8845A.exe",
    "Fluke8845A-Web.dist/Fluke8845A-Web.exe",
    "Fluke8845A-Web.dist/fluke8845a/web/index.html",
    "Fluke8845A-Web.dist/fluke8845a/web/app.css",
    "Fluke8845A-Web.dist/fluke8845a/web/app.js",
    "Fluke8845A-Web.dist/fluke8845a/web/history_worker.js",
    "README.md",
    "LICENSE",
    "THIRD-PARTY-NOTICES.md",
    BUILD_PROVENANCE_NAME,
    "docs/user-guide.md",
    "docs/hardware-validation.md",
    "docs/qt-licensing-and-third-party-notices.md",
    "docs/licenses/LGPL-3.0.txt",
    "docs/licenses/windows-portable-review-2026-10-15.md",
    "docs/licenses/windows-qt-runtime-inventory-2026-10-15.csv",
    "docs/licenses/windows-source-offer.md",
    "docs/licenses/windows-runtime-license-manifest.json",
    "docs/licenses/third-party/cpython/LICENSE.txt",
    "docs/licenses/third-party/numpy/LICENSE.txt",
    "docs/licenses/third-party/pyqtgraph/LICENSE.txt",
    "docs/licenses/third-party/pyqtgraph/CET-color-map-CC-BY.txt",
    "docs/licenses/third-party/pyserial/LICENSE.txt",
    "docs/licenses/third-party/openssl/Apache-2.0.txt",
    "docs/licenses/third-party/nuitka-runtime/LICENSE.txt",
    "docs/licenses/third-party/nuitka-runtime/LICENSE-RUNTIME.txt",
    "docs/licenses/third-party/nuitka-runtime/NOTICE.txt",
    "assets/fluke8845a_app_icon_v3.png",
    MOCK_E2E_REPORT,
}
EXCLUDED_PATH_PARTS = {
    ".git",
    ".pytest_cache",
    "__pycache__",
    "hardware_validation",
    "Fluke8845A_Agent_Reference_Pack",
    # Nuitka names its intermediate web output after the underscore entry-point.
    "Fluke8845A_Web.dist",
}


def validate_mock_e2e_report(report: dict, executable_sha256: str) -> None:
    """Reject stale, partial, or real/mocked-identity-mixed smoke reports."""
    if report.get("probe_type") != "mock_gui_six_mode_e2e" or report.get("status") != "ok":
        raise ValueError("Frozen Mock E2E report does not describe a successful GUI run")
    if report.get("simulation") is not True:
        raise ValueError("Frozen Mock E2E report must identify simulated data")
    if str(report.get("executable_sha256", "")).upper() != executable_sha256.upper():
        raise ValueError("Frozen Mock E2E report executable SHA256 does not match the packaged EXE")

    modes = report.get("modes")
    if report.get("mode_count") != len(MOCK_E2E_MODES) or not isinstance(modes, list):
        raise ValueError("Frozen Mock E2E report does not contain all six measurement modes")
    found_modes: set[str] = set()
    required_files = {"measurements.csv", "config.json", "events.csv", "run.log"}
    for result in modes:
        if not isinstance(result, dict):
            raise ValueError("Frozen Mock E2E mode entry is malformed")
        mode = result.get("mode")
        if mode not in MOCK_E2E_MODES or mode in found_modes:
            raise ValueError(f"Frozen Mock E2E report has an invalid or duplicate mode: {mode}")
        if result.get("simulation") is not True:
            raise ValueError(f"Frozen Mock E2E mode {mode} is not marked as simulated")
        sample_count = result.get("sample_count")
        valid_count = result.get("valid_sample_count")
        if not isinstance(sample_count, int) or not isinstance(valid_count, int):
            raise ValueError(f"Frozen Mock E2E mode {mode} has invalid sample counts")
        if sample_count < 3 or valid_count != sample_count:
            raise ValueError(f"Frozen Mock E2E mode {mode} has failed or missing samples")
        if not required_files.issubset(set(result.get("files_verified", []))):
            raise ValueError(f"Frozen Mock E2E mode {mode} is missing an experiment file")
        found_modes.add(mode)
    if found_modes != MOCK_E2E_MODES:
        raise ValueError("Frozen Mock E2E report omits a measurement mode")


def validate_portable_runtime_components(entries: set[str]) -> None:
    """Reject Qt PDF binaries; the app does not use PDF rendering or image loading."""
    unexpected = sorted(
        entry for entry in entries
        if Path(entry).name.casefold() in UNSUPPORTED_QT_PDF_BASENAMES
    )
    if unexpected:
        raise ValueError("Portable package contains unused Qt PDF components: " + ", ".join(unexpected))


def validate_runtime_license_manifest(
    manifest: dict,
    license_contents: dict[str, bytes],
    native_binary_contents: dict[str, bytes] | None = None,
) -> None:
    """Require copied license texts to match the collector's content-addressed manifest."""
    if not isinstance(manifest, dict):
        raise ValueError("Runtime license manifest is not a JSON object")
    if manifest.get("format_version") != 1 or not manifest.get("python_runtime_version"):
        raise ValueError("Runtime license manifest has an unsupported format or no Python runtime version")
    required_components = {
        "CPython", "numpy", "pyqtgraph", "PySide6_Essentials", "shiboken6",
        "pyserial", "OpenSSL", "Nuitka runtime library",
    }
    components = manifest.get("components")
    if not isinstance(components, list):
        raise ValueError("Runtime license manifest does not list components")
    component_names = {entry.get("name") for entry in components if isinstance(entry, dict)}
    if not required_components <= component_names:
        raise ValueError("Runtime license manifest omits components: " + ", ".join(sorted(required_components - component_names)))
    qt_components = {
        entry.get("name"): entry
        for entry in components
        if isinstance(entry, dict) and entry.get("name") in {"PySide6_Essentials", "shiboken6"}
    }
    for name, component in qt_components.items():
        declared_license = str(component.get("declared_license", ""))
        planned_license = str(component.get("planned_license_path", ""))
        expected_files = {
            f"third-party/{name.casefold()}/LicenseRef-Qt-Commercial.txt",
            "third-party/qt-for-python/LGPL-3.0.txt",
        }
        component_files = component.get("license_files")
        if not declared_license or "LGPL-3.0-only" not in declared_license:
            raise ValueError(f"Runtime license manifest omits the declared license expression for {name}")
        if "LGPL-3.0-only" not in planned_license or "review remains open" not in planned_license.casefold():
            raise ValueError(f"Runtime license manifest must keep the planned license path open for review: {name}")
        if not isinstance(component_files, list) or not expected_files <= set(component_files):
            raise ValueError(f"Runtime license manifest is missing required license files for {name}")

    records = manifest.get("license_files")
    if not isinstance(records, list) or not records:
        raise ValueError("Runtime license manifest contains no file records")
    record_paths: set[str] = set()
    for record in records:
        if not isinstance(record, dict):
            raise ValueError("Runtime license manifest contains an invalid file record")
        relative = PurePosixPath(str(record.get("path", "")))
        if "\\" in str(record.get("path", "")) or relative.is_absolute() or ".." in relative.parts or not relative.parts or relative.parts[0] != "third-party":
            raise ValueError(f"Runtime license manifest contains an unsafe path: {relative}")
        relative_name = relative.as_posix()
        if relative_name in record_paths:
            raise ValueError(f"Runtime license manifest repeats a path: {relative_name}")
        record_paths.add(relative_name)
        data = license_contents.get(relative_name)
        if data is None:
            raise ValueError(f"Runtime license file is missing from the portable package: {relative_name}")
        if len(data) != record.get("bytes"):
            raise ValueError(f"Runtime license file size does not match manifest: {relative_name}")
        digest = hashlib.sha256(data).hexdigest().upper()
        if digest != str(record.get("sha256", "")).upper():
            raise ValueError(f"Runtime license file SHA-256 does not match manifest: {relative_name}")
    for component in components:
        if not isinstance(component, dict) or not isinstance(component.get("license_files"), list):
            raise ValueError("Runtime license manifest contains an invalid component entry")
        if not set(component["license_files"]) <= record_paths:
            raise ValueError(f"Runtime license files for {component.get('name')} are not covered by the file manifest")
    if native_binary_contents is not None:
        native_records = manifest.get("native_binaries")
        if not isinstance(native_records, list) or not native_records:
            raise ValueError("Runtime manifest does not list native binaries from this build")
        native_paths: set[str] = set()
        for record in native_records:
            if not isinstance(record, dict):
                raise ValueError("Runtime manifest contains an invalid native binary record")
            value = str(record.get("path", ""))
            relative = PurePosixPath(value)
            raw_parts = value.split("/")
            if (
                "\\" in value or ":" in value or relative.is_absolute()
                or any(part in {"", ".", ".."} for part in raw_parts)
                or not relative.parts
            ):
                raise ValueError(f"Runtime manifest contains an unsafe native binary path: {value}")
            if relative.suffix.casefold() not in {".dll", ".pyd", ".exe"}:
                raise ValueError(f"Runtime manifest contains an unsupported native binary type: {value}")
            if not isinstance(record.get("family"), str) or not record["family"].strip():
                raise ValueError(f"Runtime manifest does not classify native binary for review: {value}")
            if "unclassified" in record["family"].casefold():
                raise ValueError(f"Native binary family is unclassified and requires review: {value}")
            path = relative.as_posix()
            if path in native_paths:
                raise ValueError(f"Runtime manifest repeats a native binary path: {path}")
            native_paths.add(path)
            data = native_binary_contents.get(path)
            if data is None or len(data) != record.get("bytes"):
                raise ValueError(f"Native binary inventory does not match the package: {path}")
            digest = hashlib.sha256(data).hexdigest().upper()
            if digest != str(record.get("sha256", "")).upper():
                raise ValueError(f"Runtime native binary SHA-256 does not match the package: {path}")
        if native_paths != set(native_binary_contents):
            raise ValueError("Native binary inventory does not match the package file set")
        review_status = str(manifest.get("review_status", "")).casefold()
        if not review_status.startswith("partial") or not manifest.get("known_open_items"):
            raise ValueError("Binary package manifest must preserve the unapproved review status and open items")
    if not manifest.get("known_open_items"):
        raise ValueError("Runtime license manifest must preserve known open review items")
    if not any(
        isinstance(item, str) and "qt/pyside" in item.casefold() and "exact-bundle review" in item.casefold()
        for item in manifest["known_open_items"]
    ):
        raise ValueError("Runtime license manifest must preserve the Qt/PySide review item")


def verify_extracted_portable_release(archive_path: Path, executable_sha256: str) -> None:
    """Run serial enumeration and six-mode Mock acquisition from an extracted ZIP."""
    with tempfile.TemporaryDirectory(prefix="fluke8845a-portable-smoke-") as temporary:
        temporary_root = Path(temporary)
        release_root = temporary_root / "release"
        release_root.mkdir()
        with ZipFile(archive_path, "r") as archive:
            archive.extractall(release_root)

        executable = release_root / "Fluke8845A.exe"
        if not executable.is_file():
            raise RuntimeError("Extracted portable ZIP has no Fluke8845A.exe")
        serial_result = subprocess.run(
            [str(executable), "--serial-smoke"], cwd=release_root,
            capture_output=True, text=True, timeout=30, check=False,
        )
        if serial_result.returncode != 0:
            raise RuntimeError(f"Extracted portable EXE serial smoke failed: {serial_result.returncode}")

        report_path = temporary_root / "portable_mock_e2e.json"
        mock_result = subprocess.run(
            [str(executable), "--mock-e2e", "--report", str(report_path)], cwd=release_root,
            capture_output=True, text=True, timeout=55, check=False,
        )
        if mock_result.returncode != 0 or not report_path.is_file():
            raise RuntimeError(f"Extracted portable EXE six-mode Mock E2E failed: {mock_result.returncode}")
        report = json.loads(report_path.read_text(encoding="utf-8-sig"))
        validate_mock_e2e_report(report, executable_sha256)
        verify_local_web_release(release_root)


def verify_local_web_release(release_root: Path) -> None:
    """Smoke the packaged loopback UI and its exit control with a hard time bound."""
    executable = release_root / "Fluke8845A-Web.dist" / "Fluke8845A-Web.exe"
    if not executable.is_file():
        raise RuntimeError("Portable package has no Fluke8845A-Web.exe")
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    base_url = f"http://127.0.0.1:{port}"
    process = subprocess.Popen(
        [str(executable), "--port", str(port), "--no-browser"],
        cwd=release_root, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    deadline = time.monotonic() + 12.0
    try:
        health = None
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError(f"Packaged web UI exited before startup with {process.returncode}")
            try:
                with urlopen(base_url + "/api/health", timeout=1.0) as response:
                    health = json.loads(response.read().decode("utf-8"))
                break
            except (URLError, TimeoutError, OSError):
                time.sleep(0.15)
        if not health or health.get("status") != "ok" or health.get("bind") != "127.0.0.1":
            raise RuntimeError("Packaged web UI did not start on IPv4 loopback within 12 seconds")
        with urlopen(base_url + "/", timeout=1.0) as response:
            html = response.read().decode("utf-8")
        with urlopen(base_url + "/app.js", timeout=1.0) as response:
            javascript = response.read().decode("utf-8")
        try:
            validate_packaged_web_resources(html, javascript)
        except ValueError as exc:
            raise RuntimeError(f"Packaged localhost UI resources are stale or incomplete: {exc}") from exc
        for asset in ("/app.css",):
            with urlopen(base_url + asset, timeout=1.0) as response:
                if not response.read(256):
                    raise RuntimeError(f"Packaged localhost asset is empty: {asset}")
        body = json.dumps({"action": "shutdown", "payload": {}}, ensure_ascii=False).encode("utf-8")
        request = Request(
            base_url + "/api/command", data=body, method="POST",
            headers={"Content-Type": "application/json", "Origin": base_url},
        )
        with urlopen(request, timeout=2.0) as response:
            result = json.loads(response.read().decode("utf-8"))
        if result.get("success") is not True:
            raise RuntimeError("Packaged localhost exit command was rejected")
        process.wait(timeout=5.0)
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2.0)


def _release_files(app_dist: Path) -> list[tuple[Path, str]]:
    app_dist = Path(app_dist).resolve(strict=True)
    files: dict[str, Path] = {}
    for path in sorted(app_dist.rglob("*")):
        relative = path.relative_to(app_dist).as_posix()
        if any(part in EXCLUDED_PATH_PARTS for part in Path(relative).parts):
            continue
        ensure_path_within(path, app_dist)
        if not path.is_file():
            continue
        files[relative] = ensure_regular_file_within(path, app_dist)

    for relative in ("README.md", "LICENSE", "THIRD-PARTY-NOTICES.md"):
        source = ROOT / relative
        if source.is_file():
            files[relative] = ensure_regular_file_within(source, ROOT)
    return [(path, name) for name, path in sorted(files.items())]


def _copy_release_docs(docs_source: Path, docs_target: Path) -> None:
    """Copy docs only after validating every file and directory against ROOT."""
    ensure_path_within(docs_source, ROOT)
    source_files: dict[str, Path] = {}
    for source in sorted(docs_source.rglob("*")):
        relative_path = source.relative_to(docs_source)
        validated = ensure_path_within(source, ROOT)
        if source.is_dir():
            continue
        if not source.is_file():
            continue
        source_files[relative_path.as_posix()] = ensure_regular_file_within(validated, ROOT)

    app_dist = docs_target.parent.resolve(strict=True)
    if docs_target.exists():
        ensure_path_within(docs_target, app_dist)
        generated_runtime_docs = _validated_runtime_license_doc_paths(docs_target, app_dist)
        existing_files: set[str] = set()
        for existing in sorted(docs_target.rglob("*")):
            ensure_path_within(existing, app_dist)
            if existing.is_file():
                existing_files.add(existing.relative_to(docs_target).as_posix())
        stale_files = sorted(existing_files - set(source_files) - generated_runtime_docs)
        if stale_files:
            raise ValueError("release docs destination contains stale files: " + ", ".join(stale_files))

    for relative_path, source in source_files.items():
        destination = docs_target / Path(relative_path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)


def _validated_runtime_license_doc_paths(docs_target: Path, app_dist: Path) -> set[str]:
    """Preserve only runtime license files verified by their generated manifest."""
    manifest_relative = "licenses/windows-runtime-license-manifest.json"
    manifest_path = docs_target / Path(*PurePosixPath(manifest_relative).parts)
    if not manifest_path.exists():
        return set()

    ensure_regular_file_within(manifest_path, app_dist)
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"runtime license manifest is unreadable: {exc}") from exc
    records = manifest.get("license_files") if isinstance(manifest, dict) else None
    if (
        not isinstance(manifest, dict)
        or manifest.get("format_version") != 1
        or not isinstance(records, list)
    ):
        raise ValueError("runtime license manifest is malformed")

    generated = {manifest_relative}
    for record in records:
        if not isinstance(record, dict):
            raise ValueError("runtime license manifest contains an invalid license record")
        value = record.get("path")
        if not isinstance(value, str):
            raise ValueError("runtime license manifest contains an invalid license path")
        relative = PurePosixPath(value)
        if (
            not value
            or "\\" in value
            or ":" in value
            or relative.is_absolute()
            or any(part in {"", ".", ".."} for part in value.split("/"))
            or not relative.parts
            or relative.parts[0] != "third-party"
        ):
            raise ValueError(f"runtime license manifest contains an unsafe license path: {value}")
        relative_docs_path = "licenses/" + relative.as_posix()
        if relative_docs_path in generated:
            raise ValueError(f"runtime license manifest repeats a license path: {value}")
        license_path = docs_target / Path(*PurePosixPath(relative_docs_path).parts)
        data = ensure_regular_file_within(license_path, app_dist).read_bytes()
        expected_bytes = record.get("bytes")
        expected_sha256 = record.get("sha256")
        if (
            isinstance(expected_bytes, bool)
            or not isinstance(expected_bytes, int)
            or expected_bytes != len(data)
            or not isinstance(expected_sha256, str)
            or hashlib.sha256(data).hexdigest().upper() != expected_sha256.upper()
        ):
            raise ValueError(f"runtime license manifest does not match {value}")
        generated.add(relative_docs_path)
    return generated


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app-dist", type=Path, default=APP_DIST, help="Nuitka .dist directory")
    parser.add_argument("--archive", type=Path, default=ARCHIVE, help="portable ZIP destination under dist/")
    parser.add_argument("--mock-report", type=Path, default=ROOT / MOCK_E2E_REPORT)
    parser.add_argument("--provenance", type=Path, required=True, help="shared BUILD-PROVENANCE.json for this build")
    args = parser.parse_args(argv)
    app_dist = args.app_dist.resolve()
    archive_path = args.archive.resolve()
    mock_report_path = args.mock_report.resolve()
    provenance_path = args.provenance.resolve()
    try:
        app_dist.relative_to(DIST.resolve())
        archive_path.relative_to(DIST.resolve())
        mock_report_path.relative_to((ROOT / "output" / "mock_validation").resolve())
    except ValueError:
        print("Application directory, archive, and Mock report must stay inside their release folders", file=sys.stderr)
        return 2

    if not app_dist.is_dir() or not (app_dist / "Fluke8845A.exe").is_file():
        print(f"Built Nuitka application directory not found: {app_dist}", file=sys.stderr)
        return 2
    if not mock_report_path.is_file():
        print(f"Frozen Mock E2E report not found: {mock_report_path}", file=sys.stderr)
        return 2
    if not provenance_path.is_file():
        print(f"Build provenance file not found: {provenance_path}", file=sys.stderr)
        return 2
    try:
        candidate_name = candidate_name_from_archive(archive_path, WINDOWS_ARCHIVE_SUFFIX)
        provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
        validate_provenance(provenance, expected_candidate_build_name=candidate_name)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        print(f"Invalid Windows build provenance: {exc}", file=sys.stderr)
        return 2

    docs_source = ROOT / "docs"
    docs_target = app_dist / "docs"
    if not docs_source.is_dir():
        print(f"Documentation directory not found: {docs_source}", file=sys.stderr)
        return 2
    try:
        _copy_release_docs(docs_source, docs_target)
    except (OSError, ValueError) as exc:
        print(f"Documentation contains an unsafe release path: {exc}", file=sys.stderr)
        return 2

    DIST.mkdir(parents=True, exist_ok=True)
    try:
        archive_path.relative_to(DIST.resolve())
        with ZipFile(archive_path, "w", compression=ZIP_DEFLATED, compresslevel=6) as archive:
            for source, member in _release_files(app_dist):
                archive.write(source, member)
            archive.write(mock_report_path, MOCK_E2E_REPORT)
            archive.writestr(BUILD_PROVENANCE_NAME, provenance_path.read_bytes())

        with ZipFile(archive_path, "r") as archive:
            names = set(archive.namelist())
            missing = sorted(REQUIRED_ENTRIES - names)
            unsafe = sorted(
                name for name in names
                if any(part in EXCLUDED_PATH_PARTS for part in Path(name).parts)
                or "1234567" in name
            )
            damaged = archive.testzip()
            if missing:
                raise RuntimeError("Portable package is missing required files: " + ", ".join(missing))
            validate_web_archive_resources(
                archive,
                "Fluke8845A-Web.dist/fluke8845a/web",
                ROOT / "src" / "fluke8845a" / "web",
            )
            validate_portable_runtime_components(names)
            license_manifest = json.loads(archive.read("docs/licenses/windows-runtime-license-manifest.json").decode("utf-8"))
            if not isinstance(license_manifest, dict):
                raise RuntimeError("Portable package runtime license manifest is not a JSON object")
            license_records = license_manifest.get("license_files", [])
            if not isinstance(license_records, list):
                license_records = []
            license_contents = {
                path: archive.read("docs/licenses/" + path)
                for path in (str(record.get("path", "")) for record in license_records if isinstance(record, dict))
                if "docs/licenses/" + path in names and "\\" not in path
            }
            native_binary_contents = {
                name: archive.read(name)
                for name in names
                if PurePosixPath(name).suffix.casefold() in {".dll", ".pyd", ".exe"}
            }
            validate_runtime_license_manifest(license_manifest, license_contents, native_binary_contents)
            if unsafe:
                raise RuntimeError("Portable package contains excluded paths or identifiers: " + ", ".join(unsafe))
            if damaged:
                raise RuntimeError(f"Portable package failed CRC verification at {damaged}")
            exe_hash = hashlib.sha256(archive.read("Fluke8845A.exe")).hexdigest().upper()
            archived_provenance = json.loads(archive.read(BUILD_PROVENANCE_NAME).decode("utf-8"))
            validate_windows_archive_provenance(
                archived_provenance,
                {
                    "Fluke8845A.exe": archive.read("Fluke8845A.exe"),
                    "Fluke8845A-Web.dist/Fluke8845A-Web.exe": archive.read(
                        "Fluke8845A-Web.dist/Fluke8845A-Web.exe"
                    ),
                },
                expected_candidate_build_name=candidate_name,
            )
            report = json.loads(archive.read(MOCK_E2E_REPORT).decode("utf-8-sig"))
            validate_mock_e2e_report(report, exe_hash)
    except (OSError, RuntimeError, BadZipFile, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        print(f"Unable to create or validate portable release: {exc}", file=sys.stderr)
        return 2

    verify_extracted_portable_release(archive_path, exe_hash)
    print(f"Created and verified {archive_path} ({len(names)} files; EXE SHA256 {exe_hash})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
