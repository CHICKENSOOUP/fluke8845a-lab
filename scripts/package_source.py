"""Create a source archive from an explicit public-release allowlist."""

from __future__ import annotations

import argparse
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile


ROOT = Path(__file__).resolve().parents[1]
INCLUDED_ROOT_FILES = (
    ".gitignore",
    "README.md",
    "LICENSE",
    "THIRD-PARTY-NOTICES.md",
    "Fluke8845A.py",
    "run_app.py",
    "run_windows.bat",
    "build_windows.ps1",
    "pyproject.toml",
    "requirements.txt",
    "requirements-lock.txt",
)
INCLUDED_TREES = (
    "src",
    "tests",
    "docs",
    "scripts",
    "examples/mock_experiments",
    "assets",
)
REQUIRED_ARCHIVE_PATHS = (
    "README.md",
    "LICENSE",
    "THIRD-PARTY-NOTICES.md",
    "src/fluke8845a/gui.py",
    "docs/Qt_许可与第三方声明.md",
    "docs/licenses/LGPL-3.0.txt",
    "docs/硬件验证清单.md",
    "tests/test_worker.py",
    "assets/fluke8845a_app_icon_v3.png",
)
EXCLUDED_PATH_PARTS = {
    ".git",
    ".pytest_cache",
    "__pycache__",
    "output",
    "Fluke8845A_Agent_Reference_Pack",
}


def files_for_archive() -> list[Path]:
    files: set[Path] = set()
    for name in INCLUDED_ROOT_FILES:
        path = ROOT / name
        if path.is_file():
            files.add(path)

    for name in INCLUDED_TREES:
        tree = ROOT / name
        if not tree.is_dir():
            continue
        for path in tree.rglob("*"):
            if not path.is_file():
                continue
            relative_parts = path.relative_to(ROOT).parts
            if any(part in EXCLUDED_PATH_PARTS for part in relative_parts):
                continue
            if path.suffix in {".pyc", ".pyo", ".pdf", ".zip"}:
                continue
            files.add(path)
    return sorted(files, key=lambda path: path.relative_to(ROOT).as_posix().casefold())


def create_archive(destination: Path) -> int:
    destination.parent.mkdir(parents=True, exist_ok=True)
    files = files_for_archive()
    with ZipFile(destination, "w", compression=ZIP_DEFLATED, compresslevel=6) as archive:
        for path in files:
            archive.write(path, path.relative_to(ROOT).as_posix())

    with ZipFile(destination) as archive:
        damaged = archive.testzip()
        if damaged:
            raise RuntimeError(f"Source archive CRC validation failed at {damaged}")
        names = set(archive.namelist())
        missing = [name for name in REQUIRED_ARCHIVE_PATHS if name not in names]
        if missing:
            raise RuntimeError(f"Source archive is missing required files: {', '.join(missing)}")
        unsafe = [
            name for name in names
            if any(part in EXCLUDED_PATH_PARTS for part in Path(name).parts)
        ]
        if unsafe:
            raise RuntimeError(f"Source archive contains excluded paths: {', '.join(sorted(unsafe))}")
    return len(files)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "dist" / "Fluke8845A_Source_Project.zip",
        help="output archive path (defaults to dist/Fluke8845A_Source_Project.zip)",
    )
    args = parser.parse_args()
    destination = args.output.resolve()
    try:
        destination.relative_to(ROOT.resolve())
    except ValueError as exc:
        raise SystemExit("Source archive output must stay inside the project directory") from exc

    count = create_archive(destination)
    print(f"Created and verified {destination} ({count} files)")


if __name__ == "__main__":
    main()
