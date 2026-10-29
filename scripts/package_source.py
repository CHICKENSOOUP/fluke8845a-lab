"""Create a source archive from an explicit public-release allowlist."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from zipfile import ZIP_DEFLATED, ZipFile
import tomllib

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.archive_paths import ensure_path_within, ensure_regular_file_within
from scripts.atomic_archive import atomic_archive_path
from scripts.build_provenance import (
    BUILD_PROVENANCE_NAME,
    SOURCE_ARCHIVE_SUFFIX,
    candidate_name_from_archive,
    validate_provenance,
    validate_source_archive_provenance,
)


INCLUDED_ROOT_FILES = (
    ".gitignore",
    "README.md",
    "LICENSE",
    "THIRD-PARTY-NOTICES.md",
    "Fluke8845A.py",
    "Fluke8845A_Web.py",
    "run_app.py",
    "run_windows.bat",
    "run_web.bat",
    "build_windows.ps1",
    "pyproject.toml",
    "requirements.txt",
    "requirements-lock.txt",
    "requirements-lock-python311.txt",
)
INCLUDED_TREES = (
    "src",
    "tests",
    "docs",
    "scripts",
    "examples/mock_experiments",
    "assets",
    ".github/workflows",
)
REQUIRED_ARCHIVE_PATHS = (
    "README.md",
    "LICENSE",
    "THIRD-PARTY-NOTICES.md",
    "Fluke8845A_Web.py",
    "run_windows.bat",
    "run_web.bat",
    "scripts/launch_windows.py",
    "scripts/atomic_archive.py",
    "scripts/run_web_tests.py",
    "src/fluke8845a/gui.py",
    "src/fluke8845a/history_import.py",
    "src/fluke8845a/webapp.py",
    "src/fluke8845a/webservice.py",
    "src/fluke8845a/web/index.html",
    "src/fluke8845a/web/history_worker.js",
    "docs/qt-licensing-and-third-party-notices.md",
    "docs/licenses/GPL-3.0.txt",
    "docs/licenses/LGPL-3.0.txt",
    "docs/licenses/windows-portable-review-2026-10-15.md",
    "docs/licenses/windows-portable-review-2026-10-20.md",
    "docs/licenses/windows-qt-runtime-inventory-2026-10-15.csv",
    "docs/licenses/windows-source-offer.md",
    "docs/licenses/third-party/README.md",
    "docs/licenses/third-party/Apache-2.0.txt",
    "docs/licenses/third-party/pyserial-3.5-LICENSE.txt",
    "docs/hardware-validation.md",
    "docs/release-notes-v2.0.0.md",
    "docs/release-notes-v2.1.0.md",
    "docs/release-notes-v2.1.1.md",
    "docs/release-notes-v2.1.2.md",
    "docs/release-notes-v1.1.0.md",
    "docs/release-notes-v1.0.7.md",
    "docs/release-notes-v1.0.6.md",
    "docs/release-notes-v1.0.5.md",
    "docs/release-notes-v1.0.4.md",
    "docs/release-notes-v1.0.2.md",
    "docs/release-notes-v1.0.3.md",
    "docs/release-notes-v1.0.1.md",
    "docs/releasing.md",
    "requirements-lock.txt",
    "requirements-lock-python311.txt",
    BUILD_PROVENANCE_NAME,
    "scripts/generate_demo.py",
    "tests/test_worker.py",
    "scripts/collect_windows_runtime_licenses.py",
    "scripts/release_metadata.py",
    "scripts/build_path_helpers.ps1",
    "scripts/release_publication.py",
    "scripts/archive_paths.py",
    "scripts/run_tests_sharded.py",
    "scripts/latest_verified_launcher.py",
    "tests/test_latest_verified_launcher.py",
    "assets/fluke8845a_app_icon_v3.png",
    ".github/workflows/publish-release.yml",
)
EXCLUDED_PATH_PARTS = {
    ".git",
    ".pytest_cache",
    "__pycache__",
    "output",
    "Fluke8845A_Agent_Reference_Pack",
}


def _contains_excluded_archive_part(parts) -> bool:
    """Recognize excluded directories, including generated setuptools metadata."""
    return any(
        part in EXCLUDED_PATH_PARTS or part.casefold().endswith(".egg-info")
        for part in parts
    )
NUMERIC_INSTRUMENT_SERIAL_PATTERNS = (
    re.compile(r"FLUKE\s*,\s*8845A\s*,\s*\d{6,}\s*,", re.IGNORECASE),
    re.compile(
        r"(?:[\"']?serial_number[\"']?\s*[:=]\s*[\"'](?:SN-)?|SN-)\d{6,}",
        re.IGNORECASE,
    ),
    re.compile(
        r"[\"']\d{6,}[\"']\s*:\s*\{\s*[\"'](?:outguard_sw|inguard_sw)[\"']\s*:",
        re.IGNORECASE,
    ),
)


def validate_packaged_web_resources(html: str, javascript: str) -> None:
    """Reject stale sampling-interval UI behavior in release web assets."""
    if "实测采样间隔" not in html or not re.search(r"\bid\s*=\s*['\"]actualRate['\"]", html):
        raise ValueError("页面必须显示‘实测采样间隔’并包含对应读数位置，请从当前 Web 源码重新构建")
    if not re.search(r"function\s+samplingIntervalText\s*\(\s*rateHz\s*\)", javascript):
        raise ValueError("页面脚本缺少采样间隔换算逻辑，请从当前 Web 源码重新构建")
    if not re.search(r"const\s+intervalS\s*=\s*1\s*/\s*rate\s*;", javascript):
        raise ValueError("页面脚本未将实测频率换算为采样间隔，请从当前 Web 源码重新构建")
    if not re.search(
        r"const\s+samplingInterval\s*=\s*samplingIntervalText\s*\(\s*state\.actual_hz\s*\)",
        javascript,
    ):
        raise ValueError("页面脚本未使用仪器实测频率计算采样间隔，请从当前 Web 源码重新构建")
    if not re.search(r"const\s+samplingUnit\s*=\s*samplingInterval\.unit", javascript):
        raise ValueError("页面脚本未提供采样间隔单位，请从当前 Web 源码重新构建")
    if not re.search(
        r"el\(\s*['\"]actualRate['\"]\s*\)\.(?:innerHTML|textContent)\s*=\s*[^;\n]*samplingInterval\.value[^;\n]*samplingUnit",
        javascript,
    ):
        raise ValueError("页面脚本未将采样间隔显示在对应位置，请从当前 Web 源码重新构建")


def validate_synthetic_test_serials(files: list[Path], root: Path) -> None:
    """Prevent numeric-looking hardware serials from shipping in test fixtures."""
    for path in files:
        relative = path.relative_to(root)
        if not relative.parts or relative.parts[0] != "tests":
            continue
        content = path.read_text(encoding="utf-8", errors="replace")
        if any(pattern.search(content) for pattern in NUMERIC_INSTRUMENT_SERIAL_PATTERNS):
            raise ValueError(
                "Source package test fixture contains a numeric instrument serial; "
                f"use a synthetic instrument serial marker: {relative.as_posix()}"
            )


def validate_web_archive_resources(archive: ZipFile, member_prefix: str, source_web_dir: Path) -> None:
    """Require release ZIP assets to match all current static Web sources."""
    source_web_dir = Path(source_web_dir)
    assets = ("index.html", "app.js", "app.css", "history_worker.js")
    for asset in assets:
        member = f"{member_prefix.rstrip('/')}/{asset}"
        try:
            packaged = archive.read(member)
        except KeyError as exc:
            raise ValueError(f"发布 ZIP 缺少 Web 资源 {member}，请从当前源码重新打包") from exc
        source_path = source_web_dir / asset
        if not source_path.is_file() or packaged != source_path.read_bytes():
            raise ValueError(f"发布 ZIP 中的 {member} 与当前源码不一致，请重新打包")
        if asset == "index.html":
            html = packaged.decode("utf-8")
        elif asset == "app.js":
            javascript = packaged.decode("utf-8")
    validate_packaged_web_resources(html, javascript)


SOURCE_WEB_SMOKE_RESOURCES = (
    ("/", "index.html"),
    ("/app.css", "app.css"),
    ("/app.js", "app.js"),
    ("/history_worker.js", "history_worker.js"),
)
SOURCE_WEB_SMOKE_SERVER_CODE = """\
import sys
source = sys.argv.pop(1)
sys.path.insert(0, source)
from fluke8845a.webapp import main
raise SystemExit(main(["--port", sys.argv[1], "--no-browser"]))
"""


def _source_web_smoke_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as reservation:
        reservation.bind(("127.0.0.1", 0))
        return int(reservation.getsockname()[1])


def _stop_source_web_smoke_process(process: subprocess.Popen[str], base_url: str) -> str:
    if process.poll() is None:
        body = b'{"action":"shutdown","payload":{}}'
        request = Request(
            f"{base_url}/api/command",
            data=body,
            headers={
                "Content-Type": "application/json",
                "Origin": base_url,
                "Sec-Fetch-Site": "same-origin",
            },
            method="POST",
        )
        try:
            with urlopen(request, timeout=2.0) as response:
                response.read()
        except (HTTPError, URLError, TimeoutError, OSError):
            # A failed check may leave the service unavailable; process cleanup below is authoritative.
            pass

    if process.poll() is None:
        try:
            process.wait(timeout=4.0)
        except subprocess.TimeoutExpired:
            process.terminate()
    try:
        output, _ = process.communicate(timeout=4.0)
    except subprocess.TimeoutExpired:
        process.kill()
        output, _ = process.communicate(timeout=4.0)
    return output.strip()


def smoke_extracted_source_web(
    extracted_root: Path,
    *,
    expected_web_root: Path | None = None,
    timeout_seconds: float = 20.0,
) -> dict:
    """Launch the extracted source tree's Web app and compare its live HTTP assets to this checkout."""
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise ValueError("Web smoke timeout must be a finite positive number of seconds")

    source_root = Path(extracted_root).resolve()
    source_package = source_root / "src" / "fluke8845a"
    if not (source_package / "webapp.py").is_file():
        raise ValueError(f"Extracted source tree has no Web service module: {source_package / 'webapp.py'}")

    expected_web_root = Path(expected_web_root or ROOT / "src" / "fluke8845a" / "web").resolve()
    expected_assets: dict[str, tuple[bytes, str]] = {}
    for route, filename in SOURCE_WEB_SMOKE_RESOURCES:
        source_path = expected_web_root / filename
        if not source_path.is_file():
            raise ValueError(f"Current source Web asset is missing: {source_path}")
        content = source_path.read_bytes()
        expected_assets[route] = (content, hashlib.sha256(content).hexdigest())

    validate_packaged_web_resources(
        expected_assets["/"][0].decode("utf-8"),
        expected_assets["/app.js"][0].decode("utf-8"),
    )
    expected_version = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]

    port = _source_web_smoke_port()
    base_url = f"http://127.0.0.1:{port}"
    child_env = os.environ.copy()
    extracted_python_path = str((source_root / "src").resolve())
    existing_python_path = child_env.get("PYTHONPATH")
    child_env["PYTHONPATH"] = os.pathsep.join(
        part for part in (extracted_python_path, existing_python_path) if part
    )
    child_env["PYTHONUNBUFFERED"] = "1"

    process: subprocess.Popen[str] | None = None
    report: dict | None = None
    failure: Exception | None = None
    output = ""
    deadline = time.monotonic() + timeout_seconds
    try:
        process = subprocess.Popen(
            [sys.executable, "-c", SOURCE_WEB_SMOKE_SERVER_CODE, extracted_python_path, str(port)],
            cwd=source_root,
            env=child_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
        )

        last_startup_error = "no HTTP response yet"
        health: dict | None = None
        while time.monotonic() < deadline:
            if process.poll() is not None:
                break
            remaining = deadline - time.monotonic()
            try:
                with urlopen(f"{base_url}/api/health", timeout=min(1.0, max(0.1, remaining))) as response:
                    if response.status != 200:
                        raise RuntimeError(f"/api/health returned HTTP {response.status}")
                    health = json.loads(response.read().decode("utf-8"))
                break
            except (HTTPError, URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
                last_startup_error = f"{type(exc).__name__}: {exc}"
                time.sleep(min(0.1, max(0.0, deadline - time.monotonic())))

        if process.poll() is not None:
            raise RuntimeError(
                f"extracted Web service exited before health check (exit {process.returncode})"
            )
        if health is None:
            raise RuntimeError(
                f"/api/health did not become ready within {timeout_seconds:g} seconds; "
                f"last startup error: {last_startup_error}"
            )
        if health.get("status") != "ok" or health.get("bind") != "127.0.0.1":
            raise RuntimeError(f"/api/health returned unexpected payload: {health!r}")
        if health.get("version") != expected_version:
            raise RuntimeError(
                f"/api/health version mismatch: expected {expected_version!r}, "
                f"got {health.get('version')!r}"
            )

        asset_results: dict[str, dict[str, int | str]] = {}
        for route, (_expected_content, expected_sha256) in expected_assets.items():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeError(
                    f"Web smoke exceeded its {timeout_seconds:g}-second overall verification timeout "
                    f"before checking {route}"
                )
            try:
                with urlopen(f"{base_url}{route}", timeout=min(3.0, remaining)) as response:
                    actual_content = response.read()
                    status = response.status
            except (HTTPError, URLError, TimeoutError, OSError) as exc:
                raise RuntimeError(f"Web smoke request {route} failed: {type(exc).__name__}: {exc}") from exc
            actual_sha256 = hashlib.sha256(actual_content).hexdigest()
            if status != 200:
                raise RuntimeError(f"Web smoke request {route} returned HTTP {status}")
            if actual_sha256 != expected_sha256:
                raise RuntimeError(
                    f"Web smoke resource {route} SHA-256 mismatch: "
                    f"expected {expected_sha256} ({len(_expected_content)} bytes), "
                    f"got {actual_sha256} ({len(actual_content)} bytes)"
                )
            asset_results[route] = {"sha256": actual_sha256, "bytes": len(actual_content)}

        report = {"health": health, "assets": asset_results, "base_url": base_url}
    except Exception as exc:
        failure = exc
    finally:
        if process is not None:
            output = _stop_source_web_smoke_process(process, base_url)

    if failure is not None:
        diagnostic = f"\nExtracted Web service output:\n{output}" if output else ""
        raise RuntimeError(f"Source Web smoke failed: {failure}{diagnostic}") from failure
    assert report is not None
    return report


def files_for_archive(root: Path = ROOT) -> list[Path]:
    root = Path(root).resolve()
    files: set[Path] = set()
    for name in INCLUDED_ROOT_FILES:
        path = root / name
        if path.is_file():
            files.add(ensure_regular_file_within(path, root))

    for name in INCLUDED_TREES:
        tree = root / name
        if tree.exists():
            ensure_path_within(tree, root)
        if not tree.is_dir():
            continue
        for path in tree.rglob("*"):
            relative_parts = path.relative_to(root).parts
            if _contains_excluded_archive_part(relative_parts):
                continue
            ensure_path_within(path, root)
            if not path.is_file():
                continue
            if path.suffix in {".pyc", ".pyo", ".pdf", ".zip"}:
                continue
            files.add(ensure_regular_file_within(path, root))
    ordered_files = sorted(files, key=lambda path: path.relative_to(root).as_posix().casefold())
    validate_synthetic_test_serials(ordered_files, root)
    return ordered_files


def create_archive(destination: Path, *, provenance_path: Path) -> int:
    destination.parent.mkdir(parents=True, exist_ok=True)
    provenance_bytes = Path(provenance_path).read_bytes()
    try:
        provenance = json.loads(provenance_bytes.decode("utf-8"))
        candidate_name = candidate_name_from_archive(destination, SOURCE_ARCHIVE_SUFFIX)
        validate_provenance(provenance, expected_candidate_build_name=candidate_name)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError(f"Source archive build provenance is invalid: {exc}") from exc
    files = files_for_archive()
    source_members = {path.relative_to(ROOT).as_posix(): path.read_bytes() for path in files}
    validate_source_archive_provenance(
        provenance,
        source_members,
        expected_candidate_build_name=candidate_name,
    )
    with atomic_archive_path(destination) as candidate_archive:
        with ZipFile(candidate_archive, "w", compression=ZIP_DEFLATED, compresslevel=6) as archive:
            for path in files:
                archive.write(path, path.relative_to(ROOT).as_posix())
            archive.writestr(BUILD_PROVENANCE_NAME, provenance_bytes)

        with ZipFile(candidate_archive) as archive:
            damaged = archive.testzip()
            if damaged:
                raise RuntimeError(f"Source archive CRC validation failed at {damaged}")
            names = set(archive.namelist())
            missing = [name for name in REQUIRED_ARCHIVE_PATHS if name not in names]
            if BUILD_PROVENANCE_NAME not in names:
                missing.append(BUILD_PROVENANCE_NAME)
            if missing:
                raise RuntimeError(f"Source archive is missing required files: {', '.join(missing)}")
            packaged_provenance = json.loads(archive.read(BUILD_PROVENANCE_NAME).decode("utf-8"))
            validate_web_archive_resources(archive, "src/fluke8845a/web", ROOT / "src" / "fluke8845a" / "web")
            packaged_sources = {
                name: archive.read(name)
                for name in names
                if name != BUILD_PROVENANCE_NAME
            }
            validate_source_archive_provenance(
                packaged_provenance,
                packaged_sources,
                expected_candidate_build_name=candidate_name,
            )
            unsafe = [
                name for name in names
                if _contains_excluded_archive_part(Path(name).parts)
            ]
            if unsafe:
                raise RuntimeError(f"Source archive contains excluded paths: {', '.join(sorted(unsafe))}")
    return len(files) + 1


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--smoke-extracted-source-web",
        type=Path,
        help="launch and verify the Web service from an extracted source ZIP directory",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=float,
        default=20.0,
        help="overall startup and HTTP verification timeout for extracted Web smoke (default: 20)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "dist" / "Fluke8845A_Source_Project.zip",
        help="output archive path (defaults to dist/Fluke8845A_Source_Project.zip)",
    )
    parser.add_argument("--provenance", type=Path, help="the shared BUILD-PROVENANCE.json for this build")
    args = parser.parse_args()

    if args.smoke_extracted_source_web is not None:
        try:
            report = smoke_extracted_source_web(
                args.smoke_extracted_source_web,
                expected_web_root=ROOT / "src" / "fluke8845a" / "web",
                timeout_seconds=args.timeout_seconds,
            )
        except (OSError, RuntimeError, ValueError, KeyError) as exc:
            parser.exit(1, f"Source ZIP Web smoke failed: {exc}\n")
        print("Source ZIP Web smoke passed: " + json.dumps(report, ensure_ascii=False, sort_keys=True))
        return

    if args.provenance is None:
        parser.error("--provenance is required when building a source archive")
    destination = args.output.resolve()
    try:
        destination.relative_to(ROOT.resolve())
    except ValueError as exc:
        raise SystemExit("Source archive output must stay inside the project directory") from exc

    count = create_archive(destination, provenance_path=args.provenance)
    print(f"Created and verified {destination} ({count} files)")


if __name__ == "__main__":
    main()
