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
from pathlib import Path
from zipfile import BadZipFile, ZIP_DEFLATED, ZipFile


ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "dist"
APP_DIST = DIST / "Fluke8845A.dist"
ARCHIVE = DIST / "Fluke8845A_Windows_x64.zip"
MOCK_E2E_REPORT = "output/mock_validation/latest_frozen_exe_six_mode_e2e.json"
MOCK_E2E_MODES = {"DCV", "ACV", "DCI", "ACI", "RES2", "RES4"}
REQUIRED_ENTRIES = {
    "Fluke8845A.exe",
    "Fluke8845A-Web.dist/Fluke8845A-Web.exe",
    "Fluke8845A-Web.dist/fluke8845a/web/index.html",
    "Fluke8845A-Web.dist/fluke8845a/web/app.css",
    "Fluke8845A-Web.dist/fluke8845a/web/app.js",
    "README.md",
    "LICENSE",
    "THIRD-PARTY-NOTICES.md",
    "docs/user-guide.md",
    "docs/hardware-validation.md",
    "docs/qt-licensing-and-third-party-notices.md",
    "docs/licenses/LGPL-3.0.txt",
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
        if "实时趋势" not in html:
            raise RuntimeError("Packaged localhost page is missing its measurement dashboard")
        for asset in ("/app.css", "/app.js"):
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
    files: dict[str, Path] = {}
    for path in sorted(app_dist.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(app_dist).as_posix()
        if any(part in EXCLUDED_PATH_PARTS for part in Path(relative).parts):
            continue
        files[relative] = path

    for relative in ("README.md", "LICENSE", "THIRD-PARTY-NOTICES.md"):
        source = ROOT / relative
        if source.is_file():
            files[relative] = source
    return [(path, name) for name, path in sorted(files.items())]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app-dist", type=Path, default=APP_DIST, help="Nuitka .dist directory")
    parser.add_argument("--archive", type=Path, default=ARCHIVE, help="portable ZIP destination under dist/")
    parser.add_argument("--mock-report", type=Path, default=ROOT / MOCK_E2E_REPORT)
    args = parser.parse_args(argv)
    app_dist = args.app_dist.resolve()
    archive_path = args.archive.resolve()
    mock_report_path = args.mock_report.resolve()
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

    docs_source = ROOT / "docs"
    docs_target = app_dist / "docs"
    if not docs_source.is_dir():
        print(f"Documentation directory not found: {docs_source}", file=sys.stderr)
        return 2
    shutil.copytree(docs_source, docs_target, dirs_exist_ok=True)

    DIST.mkdir(parents=True, exist_ok=True)
    try:
        archive_path.relative_to(DIST.resolve())
        with ZipFile(archive_path, "w", compression=ZIP_DEFLATED, compresslevel=6) as archive:
            for source, member in _release_files(app_dist):
                archive.write(source, member)
            archive.write(mock_report_path, MOCK_E2E_REPORT)

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
            if unsafe:
                raise RuntimeError("Portable package contains excluded paths or identifiers: " + ", ".join(unsafe))
            if damaged:
                raise RuntimeError(f"Portable package failed CRC verification at {damaged}")
            exe_hash = hashlib.sha256(archive.read("Fluke8845A.exe")).hexdigest().upper()
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
