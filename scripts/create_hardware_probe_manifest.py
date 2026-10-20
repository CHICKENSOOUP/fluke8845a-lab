"""Create a privacy-conscious SHA-256 manifest for a successful real GUI probe."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SOURCE_FILES = (
    "src/fluke8845a/gui.py",
    "src/fluke8845a/app.py",
    "src/fluke8845a/worker.py",
    "src/fluke8845a/storage.py",
    "src/fluke8845a/scpi.py",
    "src/fluke8845a/modes.py",
    "src/fluke8845a/analysis.py",
)
PROBE_PATTERN = re.compile(r"real_gui_(?:visible_)?(dcv|dci|aci|res2)_speed_and_interval_change\Z")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest().upper()


def project_file(path: Path, root: Path) -> Path:
    resolved = path.expanduser().resolve()
    try:
        resolved.relative_to(root.resolve())
    except ValueError as exc:
        raise ValueError(f"Evidence must be inside the project directory: {path}") from exc
    if not resolved.exists():
        raise FileNotFoundError(resolved)
    return resolved


def create_manifest(report_path: Path, manifest_path: Path | None = None, exe_path: Path | None = None) -> Path:
    root = ROOT.resolve()
    report_file = project_file(report_path, root)
    if not report_file.is_file():
        raise ValueError("Report path must be a file")
    report = json.loads(report_file.read_text(encoding="utf-8-sig"))
    if report.get("status") != "ok" or report.get("simulation") is not False:
        raise ValueError("Refusing to manifest an unsuccessful or simulated probe")
    match = PROBE_PATTERN.fullmatch(str(report.get("probe_type", "")))
    if not match:
        raise ValueError("Expected a successful real GUI DCV, DCI, ACI or RES2 probe")
    mode = match.group(1).upper()
    if report.get("mode", mode) != mode:
        raise ValueError("Probe report mode does not match its probe type")
    device = report.get("device") or {}
    if str(device.get("model", "")).strip().upper() != "8845A":
        raise ValueError("Report does not identify a Fluke 8845A")
    experiment = project_file(Path(report["experiment_directory"]), root)
    if not experiment.is_dir():
        raise ValueError("Experiment path must be a directory")
    config_path = project_file(experiment / "config.json", root)
    config = json.loads(config_path.read_text(encoding="utf-8-sig"))
    if config.get("mode") != mode or config.get("simulation") is not False:
        raise ValueError(f"Experiment config is not real {mode} data")

    measurements = project_file(experiment / "measurements.csv", root)
    with measurements.open("r", encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.DictReader(stream))
    if len(rows) != report.get("sample_count") or not rows:
        raise ValueError("CSV row count does not match the probe report")
    if any(row.get("status") != "ok" or not row.get("raw_reading") for row in rows):
        raise ValueError("CSV contains missing or invalid measurements")
    elapsed = [float(row["elapsed_s"]) for row in rows]
    if any(right <= left for left, right in zip(elapsed, elapsed[1:])):
        raise ValueError("CSV elapsed timestamps are not strictly increasing")
    if not any(line.strip().upper() == "> SYST:LOC" for line in report.get("communication_log", [])):
        raise ValueError("Probe report does not record SYST:LOC on exit")

    evidence = {
        "report": report_file,
        "measurements_csv": measurements,
        "config_json": config_path,
        "events_csv": project_file(experiment / "events.csv", root),
        "run_log": project_file(experiment / "run.log", root),
    }
    if exe_path is not None:
        evidence["executed_exe"] = project_file(exe_path, root)
    visible_gui = report.get("visible_gui")
    if visible_gui:
        evidence["visible_gui_screenshot"] = project_file(Path(visible_gui["screenshot_path"]), root)

    source_hashes = {name: sha256(project_file(root / name, root)) for name in SOURCE_FILES}
    manifest = {
        "manifest_version": 1,
        "probe_type": report["probe_type"],
        "simulation": False,
        "run_status": "ok",
        "manufacturer": str(device.get("manufacturer", "")),
        "model": "8845A",
        "mode": mode,
        "baudrate": report.get("baudrate"),
        "collection_duration_s": report.get("collection_duration_s"),
        "sample_count": len(rows),
        "invalid_count": report.get("invalid_count", 0),
        "displayed_actual_interval": report.get("displayed_actual_interval"),
        "csv_reconstructed_actual_rate_hz": report.get("csv_reconstructed_actual_rate_hz"),
        "evidence_files": {
            label: {"size_bytes": path.stat().st_size, "sha256": sha256(path)}
            for label, path in evidence.items()
        },
        "source_reference_sha256": source_hashes,
        "scope_note": (
            f"Real 8845A {mode} GUI communication, acquisition, plotting, CSV persistence, "
            "speed/interval changes and configuration restore were checked. This is not a traceable accuracy test."
        ),
    }
    target = project_file(manifest_path, root) if manifest_path and manifest_path.exists() else (
        manifest_path.expanduser().resolve() if manifest_path else report_file.with_name(report_file.stem + "_manifest.json")
    )
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise ValueError("Manifest output must be inside the project directory") from exc
    if target.exists():
        raise FileExistsError(f"Refusing to overwrite manifest: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return target


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--exe", type=Path, help="optional frozen EXE used for the GUI probe")
    parser.add_argument("--manifest", type=Path, help="output path; defaults beside the report")
    args = parser.parse_args(argv)
    try:
        print(create_manifest(args.report, args.manifest, args.exe))
    except (OSError, KeyError, TypeError, ValueError) as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
