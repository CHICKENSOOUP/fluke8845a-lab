"""Compute a validated, versioned identity for a source release build."""

from __future__ import annotations

import argparse
import json
import re
import tomllib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SEMVER = re.compile(r"\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?\Z")


def project_version(project_file: Path = ROOT / "pyproject.toml") -> str:
    with project_file.open("rb") as stream:
        value = tomllib.load(stream).get("project", {}).get("version")
    if not isinstance(value, str) or not SEMVER.fullmatch(value):
        raise ValueError("pyproject.toml must declare a semantic project.version")
    return value


def release_identity(version: str, ref_type: str, ref_name: str, sha: str = "") -> dict[str, object]:
    if not SEMVER.fullmatch(version):
        raise ValueError(f"Invalid project version: {version}")
    if ref_type == "tag":
        expected_tag = f"v{version}"
        if ref_name != expected_tag:
            raise ValueError(f"Release tag {ref_name!r} does not match project version {expected_tag!r}")
        return {
            "tag": expected_tag,
            "title": f"Fluke 8845A Lab {expected_tag}",
            "version": version,
            "prerelease": False,
        }
    if ref_type != "branch":
        raise ValueError(f"Unsupported release ref type: {ref_type}")
    if ref_name != "main":
        raise ValueError(f"Main prereleases can only be published from the main branch, not {ref_name!r}")
    if not re.fullmatch(r"[0-9a-fA-F]{12,40}", sha):
        raise ValueError("Main build requires a full Git commit SHA")
    short_sha = sha[:12].lower()
    return {
        "tag": f"main-v{version}-build-{short_sha}",
        "title": f"Fluke 8845A Lab v{version} (main build {short_sha})",
        "version": version,
        "prerelease": True,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ref-type", required=True)
    parser.add_argument("--ref-name", required=True)
    parser.add_argument("--sha", default="")
    args = parser.parse_args()
    print(json.dumps(release_identity(project_version(), args.ref_type, args.ref_name, args.sha)))


if __name__ == "__main__":
    main()
