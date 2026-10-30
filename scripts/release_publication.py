"""Validate that a GitHub Release and GHCR artifact contain the same build."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any


def validate_github_release(
    release: Any,
    *,
    expected_tag: str,
    expected_prerelease: bool,
    expected_assets: dict[str, int],
    expected_digests: dict[str, str] | None = None,
) -> None:
    if not isinstance(release, dict):
        raise ValueError("GitHub Release response is not a JSON object")
    if release.get("tagName") != expected_tag:
        raise ValueError(f"GitHub Release tag mismatch: expected {expected_tag!r}")
    if release.get("isDraft") is not False:
        raise ValueError("GitHub Release is still a draft")
    if release.get("isPrerelease") is not expected_prerelease:
        raise ValueError("GitHub Release stable/prerelease status does not match its ref")

    assets = release.get("assets")
    if not isinstance(assets, list):
        raise ValueError("GitHub Release response has no asset list")
    actual: dict[str, int] = {}
    for asset in assets:
        if not isinstance(asset, dict) or not isinstance(asset.get("name"), str):
            raise ValueError("GitHub Release contains an invalid asset record")
        name = asset["name"]
        if name in actual:
            raise ValueError(f"GitHub Release has duplicate asset {name!r}")
        size = asset.get("size")
        if isinstance(size, bool) or not isinstance(size, int):
            raise ValueError(f"GitHub Release asset {name!r} has no valid size")
        actual[name] = size
    if actual != expected_assets:
        raise ValueError(
            f"GitHub Release assets differ: expected {expected_assets!r}, got {actual!r}"
        )
    if expected_digests is not None:
        if set(expected_digests) != set(expected_assets):
            raise ValueError("Expected release asset digests do not match the asset names")
        for asset in assets:
            name = asset["name"]
            digest = asset.get("digest")
            if not isinstance(digest, str) or digest.lower() != expected_digests[name].lower():
                raise ValueError(f"GitHub Release asset {name!r} has a missing or mismatched digest")


def validate_oci_manifest(manifest: Any, *, expected_layers: dict[str, str]) -> None:
    # ORAS 1.2 `manifest fetch --format json` returns metadata around the
    # fetched registry manifest; the raw OCI document is in `content`.
    if isinstance(manifest, dict) and isinstance(manifest.get("content"), dict):
        manifest = manifest["content"]
    if not isinstance(manifest, dict) or manifest.get("schemaVersion") != 2:
        raise ValueError("GHCR response is not an OCI image manifest")
    if manifest.get("artifactType") != "application/vnd.fluke8845a.source-bundle.v1":
        raise ValueError("GHCR OCI artifact type is not the Fluke 8845A source bundle")
    layers = manifest.get("layers")
    if not isinstance(layers, list):
        raise ValueError("GHCR OCI manifest has no artifact layer list")
    actual: dict[str, str] = {}
    for layer in layers:
        if not isinstance(layer, dict):
            raise ValueError("GHCR OCI manifest contains an invalid layer descriptor")
        media_type = layer.get("mediaType")
        digest = layer.get("digest")
        if not isinstance(media_type, str) or not isinstance(digest, str):
            raise ValueError("GHCR OCI layer is missing its media type or digest")
        if media_type in actual:
            raise ValueError(f"GHCR OCI manifest has duplicate layer type {media_type!r}")
        actual[media_type] = digest.lower()
    expected = {media_type: digest.lower() for media_type, digest in expected_layers.items()}
    if actual != expected:
        raise ValueError(f"GHCR OCI layers differ: expected {expected!r}, got {actual!r}")


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _asset_sizes(*paths: Path) -> dict[str, int]:
    return {path.name: path.stat().st_size for path in paths}


def _sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return "sha256:" + hashlib.file_digest(stream, "sha256").hexdigest()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    release_parser = commands.add_parser("release")
    release_parser.add_argument("--json", type=Path, required=True)
    release_parser.add_argument("--tag", required=True)
    release_parser.add_argument("--prerelease", choices=("true", "false"), required=True)
    release_parser.add_argument("--archive", type=Path, required=True)
    release_parser.add_argument("--checksums", type=Path, required=True)
    package_parser = commands.add_parser("package")
    package_parser.add_argument("--json", type=Path, required=True)
    package_parser.add_argument("--archive", type=Path, required=True)
    package_parser.add_argument("--checksums", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        payload = _read_json(args.json)
        if args.command == "release":
            validate_github_release(
                payload,
                expected_tag=args.tag,
                expected_prerelease=args.prerelease == "true",
                expected_assets=_asset_sizes(args.archive, args.checksums),
                expected_digests={path.name: _sha256(path) for path in (args.archive, args.checksums)},
            )
        else:
            validate_oci_manifest(
                payload,
                expected_layers={
                    "application/zip": _sha256(args.archive),
                    "text/plain": _sha256(args.checksums),
                },
            )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        parser.exit(1, f"Publication verification failed: {exc}\n")
    print(f"Verified {args.command} publication")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
