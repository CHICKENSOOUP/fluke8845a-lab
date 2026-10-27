"""Create and verify source and executable provenance for a Windows build."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import platform
import re
import struct
import subprocess
import sys
import tomllib
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Iterable

if __package__:
    from .release_metadata import SEMVER
else:  # Support direct execution as ``python scripts/build_provenance.py``.
    from release_metadata import SEMVER


BUILD_PROVENANCE_NAME = "BUILD-PROVENANCE.json"
SOURCE_ARCHIVE_SUFFIX = "_Source_Project.zip"
WINDOWS_ARCHIVE_SUFFIX = "_Windows_x64.zip"
DESKTOP_ARCHIVE_PATH = "Fluke8845A.exe"
WEB_ARCHIVE_PATH = "Fluke8845A-Web.dist/Fluke8845A-Web.exe"
SOURCE_TREE_DIGEST_ALGORITHM = "sha256-v1: sorted POSIX path NUL size NUL file-sha256 LF records; BUILD-PROVENANCE.json excluded"
_BUILD_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")
_SHA256_PATTERN = re.compile(r"^[A-Fa-f0-9]{64}$")
_GIT_COMMIT_PATTERN = re.compile(r"^[A-Fa-f0-9]{40}(?:[A-Fa-f0-9]{24})?$")


def validate_candidate_build_name(value: str) -> str:
    if not isinstance(value, str) or not _BUILD_NAME_PATTERN.fullmatch(value) or ".." in value:
        raise ValueError("candidate build name must be a safe Windows filename stem")
    return value


def candidate_name_from_archive(archive_path: Path | str, suffix: str) -> str:
    name = Path(archive_path).name
    if not name.endswith(suffix):
        raise ValueError(f"archive name must end with {suffix}")
    return validate_candidate_build_name(name[:-len(suffix)])


def _normalized_member_name(name: str) -> str:
    value = str(name).replace("\\", "/")
    relative = PurePosixPath(value)
    if (
        not value
        or value.startswith("/")
        or ":" in value
        or any(part in {"", ".", ".."} for part in value.split("/"))
        or relative.is_absolute()
    ):
        raise ValueError(f"unsafe source path in provenance digest: {name}")
    if value == BUILD_PROVENANCE_NAME:
        raise ValueError("BUILD-PROVENANCE.json cannot be included in its own source digest")
    return relative.as_posix()


def source_tree_digest_from_members(members: Iterable[tuple[str, bytes]]) -> str:
    """Hash a stable sequence of POSIX path, size, and per-file SHA-256 records."""
    normalized: dict[str, bytes] = {}
    for raw_name, data in members:
        name = _normalized_member_name(raw_name)
        if name in normalized:
            raise ValueError(f"duplicate source path in provenance digest: {name}")
        if not isinstance(data, bytes):
            raise TypeError(f"source member content must be bytes: {name}")
        normalized[name] = data

    digest = hashlib.sha256()
    for name in sorted(normalized, key=lambda item: (item.casefold(), item)):
        data = normalized[name]
        record = (
            name.encode("utf-8") + b"\0"
            + str(len(data)).encode("ascii") + b"\0"
            + hashlib.sha256(data).hexdigest().encode("ascii") + b"\n"
        )
        digest.update(record)
    return digest.hexdigest().upper()


def source_tree_digest(root: Path, relative_paths: Iterable[str | Path] | None = None) -> str:
    """Hash the exact allowlisted source package file set, not the whole checkout."""
    root = Path(root).resolve()
    if relative_paths is None:
        root_string = str(root)
        if root_string not in sys.path:
            sys.path.insert(0, root_string)
        from scripts.package_source import files_for_archive

        files = files_for_archive(root)
        members = ((path.relative_to(root).as_posix(), path.read_bytes()) for path in files)
    else:
        def read_member(value: str | Path) -> tuple[str, bytes]:
            path = Path(value)
            if path.is_absolute():
                try:
                    name = path.resolve().relative_to(root).as_posix()
                except ValueError as exc:
                    raise ValueError(f"source path is outside project root: {path}") from exc
                source = path
            else:
                name = path.as_posix()
                source = root.joinpath(*PurePosixPath(name).parts)
            return name, source.read_bytes()

        members = (read_member(value) for value in relative_paths)
    return source_tree_digest_from_members(members)


def capture_git_state(root: Path) -> dict[str, str | bool]:
    root = Path(root).resolve()
    try:
        head_commit = subprocess.check_output(
            ["git", "rev-parse", "--verify", "HEAD"], cwd=root, text=True, stderr=subprocess.PIPE
        ).strip()
        status = subprocess.check_output(
            ["git", "status", "--porcelain=v1", "--untracked-files=all"],
            cwd=root,
            text=True,
            stderr=subprocess.PIPE,
        )
        tracked = set(
            subprocess.check_output(
                ["git", "ls-files", "-z"], cwd=root, stderr=subprocess.PIPE
            ).decode("utf-8", errors="surrogateescape").split("\0")
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ValueError(f"unable to capture Git state for build provenance: {exc}") from exc
    if not _GIT_COMMIT_PATTERN.fullmatch(head_commit):
        raise ValueError("Git HEAD is not a full 40- or 64-character commit id")
    root_string = str(root)
    if root_string not in sys.path:
        sys.path.insert(0, root_string)
    from scripts.package_source import files_for_archive

    packaged_untracked = any(
        path.relative_to(root).as_posix() not in tracked
        for path in files_for_archive(root)
    )
    return {
        "head_commit": head_commit.lower(),
        "worktree_dirty": bool(status.strip()) or packaged_untracked,
    }


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def _command_output(command: list[str]) -> str | None:
    try:
        result = subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    output = getattr(result, "stdout", None)
    return output.strip() if isinstance(output, str) and output.strip() else None


def _reported_value(output: str | None, label: str) -> str | None:
    if output is None:
        return None
    match = re.search(
        rf"^{re.escape(label)}:\s*(.+?)\s*$", output, flags=re.MULTILINE | re.IGNORECASE
    )
    return match.group(1) if match else None


def capture_toolchain() -> dict[str, str | int | None]:
    """Record the active Python and available Nuitka, Node, and C compiler versions."""
    nuitka_output = _command_output([sys.executable, "-m", "nuitka", "--version"])
    nuitka_version = _reported_value(nuitka_output, "Nuitka-Version")
    if nuitka_version is None:
        try:
            nuitka_version = importlib.metadata.version("Nuitka")
        except importlib.metadata.PackageNotFoundError:
            pass

    pointer_bits = struct.calcsize("P") * 8
    return {
        "python_version": platform.python_version(),
        "python_implementation": platform.python_implementation(),
        "python_architecture": platform.machine() or f"{pointer_bits}-bit",
        "python_bits": pointer_bits,
        "nuitka_version": nuitka_version,
        "node_version": _command_output(["node", "--version"]),
        "compiler_version": _reported_value(nuitka_output, "Version C compiler"),
    }


def dependency_lock_hashes(root: Path) -> dict[str, str]:
    """Return SHA-256 hashes for the project's version-specific pip lock files."""
    root = Path(root).resolve()
    lock_files = sorted(
        (path for path in root.glob("requirements-lock*.txt") if path.is_file()),
        key=lambda path: path.name.casefold(),
    )
    return {path.name: _file_sha256(path) for path in lock_files}


def source_project_metadata(root: Path) -> tuple[str, str]:
    """Read the authoritative project name and version from the source tree."""
    path = Path(root) / "pyproject.toml"
    try:
        document = tomllib.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ValueError(f"unable to read project metadata from pyproject.toml: {exc}") from exc
    project = document.get("project")
    name = project.get("name") if isinstance(project, dict) else None
    version = project.get("version") if isinstance(project, dict) else None
    if not isinstance(name, str) or not name.strip():
        raise ValueError("pyproject.toml must define a project name")
    if not isinstance(version, str) or not SEMVER.fullmatch(version):
        raise ValueError("pyproject.toml must define a valid static project version")
    return name.strip(), version


def installed_distribution_inventory() -> list[dict[str, str]]:
    """Return the exact Python distributions visible to the build interpreter."""
    distributions: dict[str, dict[str, str]] = {}
    for distribution in importlib.metadata.distributions():
        name = distribution.metadata.get("Name")
        version = distribution.version
        if not name or not version:
            continue
        key = re.sub(r"[-_.]+", "-", name).casefold()
        entry = {"name": name, "version": version}
        previous = distributions.get(key)
        if previous is not None and previous["version"] != version:
            raise ValueError(f"multiple installed versions found for {name}")
        distributions[key] = entry
    return [distributions[key] for key in sorted(distributions)]


def make_build_provenance(
    *,
    candidate_build_name: str,
    built_at_utc: str,
    source_tree_sha256: str,
    git_state: dict[str, str | bool],
    desktop_exe: Path,
    web_exe: Path | None,
    desktop_only: bool = False,
    toolchain: dict[str, str | int | None] | None = None,
    dependency_lock_sha256: dict[str, str] | None = None,
    installed_distributions: list[dict[str, str]] | None = None,
    project_version: str | None = None,
) -> dict:
    candidate_build_name = validate_candidate_build_name(candidate_build_name)
    head_commit = str(git_state.get("head_commit", ""))
    dirty = git_state.get("worktree_dirty")
    if not _GIT_COMMIT_PATTERN.fullmatch(head_commit):
        raise ValueError("build provenance requires a full Git HEAD commit id")
    if not isinstance(dirty, bool):
        raise ValueError("build provenance requires a Boolean Git dirty state")
    if not _SHA256_PATTERN.fullmatch(source_tree_sha256):
        raise ValueError("source tree SHA-256 must contain exactly 64 hexadecimal characters")
    if desktop_only and web_exe is not None:
        raise ValueError("desktop-only provenance must not include a web executable")
    if not desktop_only and web_exe is None:
        raise ValueError("combined provenance requires a web executable")
    document = {
        "format_version": 1,
        "candidate_build_name": candidate_build_name,
        "built_at_utc": built_at_utc,
        "head_commit": head_commit.lower(),
        "worktree_dirty": dirty,
        "build_source_kind": "working_tree_snapshot" if dirty else "commit_tree",
        "source_tree_sha256": source_tree_sha256.upper(),
        "source_tree_digest_algorithm": SOURCE_TREE_DIGEST_ALGORITHM,
        "executables": {
            "desktop": {
                "archive_path": DESKTOP_ARCHIVE_PATH,
                "sha256": _file_sha256(Path(desktop_exe)),
            },
        },
    }
    if desktop_only:
        document["artifact_scope"] = "desktop_only"
    else:
        document["executables"]["web"] = {
            "archive_path": WEB_ARCHIVE_PATH,
            "sha256": _file_sha256(Path(web_exe)),
        }
    if toolchain is not None:
        document["toolchain"] = toolchain
    if dependency_lock_sha256 is not None:
        document["dependency_lock_sha256"] = dependency_lock_sha256
    if installed_distributions is not None:
        document["installed_distributions"] = installed_distributions
    if project_version is not None:
        document["project_version"] = project_version
    return document


def create_build_provenance(
    *,
    root: Path,
    candidate_build_name: str,
    expected_source_tree_sha256: str,
    desktop_exe: Path,
    web_exe: Path | None,
    desktop_only: bool = False,
) -> dict:
    actual_digest = source_tree_digest(root)
    if actual_digest.upper() != expected_source_tree_sha256.upper():
        raise ValueError(
            "source tree changed between build start and provenance capture; refusing to package a mixed-source candidate"
        )
    project_name, project_version = source_project_metadata(root)
    git_state = capture_git_state(root)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    return make_build_provenance(
        candidate_build_name=candidate_build_name,
        built_at_utc=now,
        source_tree_sha256=actual_digest,
        git_state=git_state,
        desktop_exe=desktop_exe,
        web_exe=web_exe,
        desktop_only=desktop_only,
        toolchain=capture_toolchain(),
        dependency_lock_sha256=dependency_lock_hashes(root),
        installed_distributions=[
            entry
            for entry in installed_distribution_inventory()
            if re.sub(r"[-_.]+", "-", entry["name"]).casefold()
            != re.sub(r"[-_.]+", "-", project_name).casefold()
        ],
        project_version=project_version,
    )


def create_source_provenance(*, root: Path, candidate_build_name: str) -> dict:
    """Create provenance for a source-only archive without inventing EXE hashes."""
    root = Path(root).resolve()
    _, project_version = source_project_metadata(root)
    git_state = capture_git_state(root)
    return {
        "format_version": 1,
        "artifact_scope": "source_only",
        "candidate_build_name": validate_candidate_build_name(candidate_build_name),
        "built_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "head_commit": git_state["head_commit"],
        "worktree_dirty": git_state["worktree_dirty"],
        "build_source_kind": "working_tree_snapshot" if git_state["worktree_dirty"] else "commit_tree",
        "source_tree_sha256": source_tree_digest(root),
        "source_tree_digest_algorithm": SOURCE_TREE_DIGEST_ALGORITHM,
        "project_version": project_version,
        "toolchain": capture_toolchain(),
        "dependency_lock_sha256": dependency_lock_hashes(root),
    }


def validate_provenance(
    document: dict,
    *,
    expected_candidate_build_name: str,
    desktop_exe: bytes | None = None,
    web_exe: bytes | None = None,
) -> None:
    if not isinstance(document, dict) or document.get("format_version") != 1:
        raise ValueError("unsupported or malformed build provenance document")
    expected = validate_candidate_build_name(expected_candidate_build_name)
    if document.get("candidate_build_name") != expected:
        raise ValueError("provenance candidate build name does not match the archive name")
    head = document.get("head_commit")
    if not isinstance(head, str) or not _GIT_COMMIT_PATTERN.fullmatch(head):
        raise ValueError("provenance does not contain a full Git HEAD commit id")
    dirty = document.get("worktree_dirty")
    if not isinstance(dirty, bool):
        raise ValueError("provenance worktree_dirty must be Boolean")
    expected_kind = "working_tree_snapshot" if dirty else "commit_tree"
    if document.get("build_source_kind") != expected_kind:
        raise ValueError(f"provenance source kind must be {expected_kind}")
    if not isinstance(document.get("source_tree_sha256"), str) or not _SHA256_PATTERN.fullmatch(document["source_tree_sha256"]):
        raise ValueError("provenance source tree SHA-256 is malformed")
    if document.get("source_tree_digest_algorithm") != SOURCE_TREE_DIGEST_ALGORITHM:
        raise ValueError("provenance source tree digest algorithm is missing or unsupported")
    if "project_version" in document and (
        not isinstance(document["project_version"], str)
        or not SEMVER.fullmatch(document["project_version"])
    ):
        raise ValueError("provenance project version is malformed")
    timestamp = document.get("built_at_utc")
    try:
        parsed_timestamp = datetime.fromisoformat(str(timestamp).replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("provenance build timestamp is not valid ISO 8601") from exc
    if parsed_timestamp.tzinfo is None or parsed_timestamp.utcoffset() is None:
        raise ValueError("provenance build timestamp must include a UTC offset")
    if parsed_timestamp.utcoffset().total_seconds() != 0:
        raise ValueError("provenance build timestamp must be UTC")

    if "toolchain" in document:
        toolchain = document["toolchain"]
        toolchain_fields = {
            "python_version",
            "python_implementation",
            "python_architecture",
            "python_bits",
            "nuitka_version",
            "node_version",
            "compiler_version",
        }
        required_text_fields = (
            "python_version",
            "python_implementation",
            "python_architecture",
        )
        if not isinstance(toolchain, dict) or not toolchain_fields.issubset(toolchain):
            raise ValueError("provenance toolchain metadata is malformed")
        if any(not isinstance(toolchain[key], str) or not toolchain[key].strip() for key in required_text_fields):
            raise ValueError("provenance toolchain metadata is malformed")
        python_bits = toolchain.get("python_bits")
        if isinstance(python_bits, bool) or not isinstance(python_bits, int) or python_bits <= 0:
            raise ValueError("provenance toolchain metadata has invalid Python architecture bits")
        for key in ("nuitka_version", "node_version", "compiler_version"):
            value = toolchain.get(key)
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise ValueError(f"provenance toolchain metadata has invalid {key}")

    if "dependency_lock_sha256" in document:
        lock_hashes = document["dependency_lock_sha256"]
        if not isinstance(lock_hashes, dict):
            raise ValueError("provenance dependency lock SHA-256 metadata is malformed")
        for name, value in lock_hashes.items():
            try:
                safe_name = isinstance(name, str) and _normalized_member_name(name) == name
            except ValueError:
                safe_name = False
            if not safe_name or not _SHA256_PATTERN.fullmatch(value if isinstance(value, str) else ""):
                raise ValueError("provenance dependency lock SHA-256 metadata is malformed")

    if "installed_distributions" in document:
        inventory = document["installed_distributions"]
        if not isinstance(inventory, list):
            raise ValueError("provenance installed package inventory is malformed")
        seen_names: set[str] = set()
        for entry in inventory:
            if not isinstance(entry, dict):
                raise ValueError("provenance installed package inventory is malformed")
            name = entry.get("name")
            version = entry.get("version")
            if (
                not isinstance(name, str)
                or not name.strip()
                or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", name)
                or not isinstance(version, str)
                or not version.strip()
            ):
                raise ValueError("provenance installed package inventory is malformed")
            normalized_name = re.sub(r"[-_.]+", "-", name).casefold()
            if normalized_name in seen_names:
                raise ValueError("provenance installed package inventory contains duplicate names")
            seen_names.add(normalized_name)

    if document.get("artifact_scope") == "source_only":
        if "executables" in document:
            raise ValueError("source-only provenance must not claim executable hashes")
        return

    executables = document.get("executables")
    if not isinstance(executables, dict):
        raise ValueError("provenance executable hashes are missing")
    desktop_only = document.get("artifact_scope") == "desktop_only"
    if desktop_only and (set(executables) != {"desktop"} or web_exe is not None):
        raise ValueError("desktop-only provenance must not include a web executable")
    expected_executables = [("desktop", DESKTOP_ARCHIVE_PATH, desktop_exe, "desktop")]
    if not desktop_only:
        expected_executables.append(("web", WEB_ARCHIVE_PATH, web_exe, "web"))
    for key, archive_path, content, label in expected_executables:
        entry = executables.get(key)
        if not isinstance(entry, dict) or entry.get("archive_path") != archive_path:
            raise ValueError(f"provenance {label} executable archive path is invalid")
        expected_hash = entry.get("sha256")
        if not isinstance(expected_hash, str) or not _SHA256_PATTERN.fullmatch(expected_hash):
            raise ValueError(f"provenance {label} executable SHA-256 is malformed")
        if content is not None and hashlib.sha256(content).hexdigest().upper() != expected_hash.upper():
            raise ValueError(f"provenance {label} executable SHA-256 does not match the archive")


def validate_source_archive_provenance(
    document: dict,
    source_members: dict[str, bytes],
    *,
    expected_candidate_build_name: str,
) -> None:
    validate_provenance(document, expected_candidate_build_name=expected_candidate_build_name)
    actual_digest = source_tree_digest_from_members(source_members.items())
    if actual_digest != document["source_tree_sha256"].upper():
        raise ValueError("source tree SHA-256 does not match source archive contents")
    for name, expected_hash in document.get("dependency_lock_sha256", {}).items():
        content = source_members.get(name)
        if content is None or hashlib.sha256(content).hexdigest().upper() != expected_hash.upper():
            raise ValueError(f"provenance dependency lock SHA-256 does not match {name}")


def validate_windows_archive_provenance(
    document: dict,
    archive_members: dict[str, bytes],
    *,
    expected_candidate_build_name: str,
) -> None:
    if document.get("artifact_scope") == "source_only":
        raise ValueError("source-only provenance cannot validate a Windows executable archive")
    required_paths = {DESKTOP_ARCHIVE_PATH}
    if document.get("artifact_scope") != "desktop_only":
        required_paths.add(WEB_ARCHIVE_PATH)
    if not required_paths.issubset(archive_members):
        raise ValueError("Windows executable archive is missing a declared executable")
    if document.get("artifact_scope") == "desktop_only" and WEB_ARCHIVE_PATH in archive_members:
        raise ValueError("desktop-only archive must not include a web executable")
    validate_provenance(
        document,
        expected_candidate_build_name=expected_candidate_build_name,
        desktop_exe=archive_members.get(DESKTOP_ARCHIVE_PATH),
        web_exe=archive_members.get(WEB_ARCHIVE_PATH),
    )


def _source_digest_cli(root: Path, output: Path) -> None:
    digest = source_tree_digest(root)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(digest + "\n", encoding="ascii")


def _create_cli(args: argparse.Namespace) -> None:
    expected_digest = args.expected_source_digest.read_text(encoding="ascii").strip()
    document = create_build_provenance(
        root=args.root,
        candidate_build_name=args.candidate_build_name,
        expected_source_tree_sha256=expected_digest,
        desktop_exe=args.desktop_exe,
        web_exe=args.web_exe,
        desktop_only=args.desktop_only,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _source_cli(args: argparse.Namespace) -> None:
    document = create_source_provenance(root=args.root, candidate_build_name=args.candidate_build_name)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    digest_parser = subparsers.add_parser("digest", help="capture the current packaged source-tree digest")
    digest_parser.add_argument("--root", type=Path, required=True)
    digest_parser.add_argument("--output", type=Path, required=True)
    create_parser = subparsers.add_parser("create", help="create final provenance after both EXEs are built")
    create_parser.add_argument("--root", type=Path, required=True)
    create_parser.add_argument("--candidate-build-name", required=True)
    create_parser.add_argument("--expected-source-digest", type=Path, required=True)
    create_parser.add_argument("--desktop-exe", type=Path, required=True)
    create_parser.add_argument("--web-exe", type=Path)
    create_parser.add_argument("--desktop-only", action="store_true")
    create_parser.add_argument("--output", type=Path, required=True)
    source_parser = subparsers.add_parser("source", help="create provenance for a source-only archive")
    source_parser.add_argument("--root", type=Path, required=True)
    source_parser.add_argument("--candidate-build-name", required=True)
    source_parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == "digest":
            _source_digest_cli(args.root, args.output)
        elif args.command == "source":
            _source_cli(args)
        else:
            _create_cli(args)
    except (OSError, ValueError) as exc:
        parser.exit(2, f"Could not create build provenance: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
