"""Create a source archive from an explicit public-release allowlist."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

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
    "docs/licenses/LGPL-3.0.txt",
    "docs/licenses/windows-portable-review-2026-10-15.md",
    "docs/licenses/windows-portable-review-2026-10-20.md",
    "docs/licenses/windows-qt-runtime-inventory-2026-10-15.csv",
    "docs/licenses/windows-source-offer.md",
    "docs/licenses/third-party/README.md",
    "docs/licenses/third-party/Apache-2.0.txt",
    "docs/licenses/third-party/pyserial-3.5-LICENSE.txt",
    "docs/hardware-validation.md",
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
            if any(part in EXCLUDED_PATH_PARTS for part in relative_parts):
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
                if any(part in EXCLUDED_PATH_PARTS for part in Path(name).parts)
            ]
            if unsafe:
                raise RuntimeError(f"Source archive contains excluded paths: {', '.join(sorted(unsafe))}")
    return len(files) + 1


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "dist" / "Fluke8845A_Source_Project.zip",
        help="output archive path (defaults to dist/Fluke8845A_Source_Project.zip)",
    )
    parser.add_argument("--provenance", type=Path, required=True, help="the shared BUILD-PROVENANCE.json for this build")
    args = parser.parse_args()
    destination = args.output.resolve()
    try:
        destination.relative_to(ROOT.resolve())
    except ValueError as exc:
        raise SystemExit("Source archive output must stay inside the project directory") from exc

    count = create_archive(destination, provenance_path=args.provenance)
    print(f"Created and verified {destination} ({count} files)")


if __name__ == "__main__":
    main()
