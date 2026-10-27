"""Create a privacy-conscious SHA-256 manifest for a successful real GUI probe."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
_SRC_ROOT = str(ROOT / "src")
if _SRC_ROOT not in sys.path:
    sys.path.insert(0, _SRC_ROOT)
from fluke8845a.probe_provenance import PROBE_SOURCE_FILES

SOURCE_FILES = PROBE_SOURCE_FILES
PROBE_PATTERN = re.compile(r"real_gui_(?:visible_)?(dcv|acv|dci|aci|res2|res4)_speed_and_interval_change\Z")
SHA256_PATTERN = re.compile(r"[0-9a-fA-F]{64}\Z")
GIT_COMMIT_PATTERN = re.compile(r"[0-9a-fA-F]{40}(?:[0-9a-fA-F]{24})?\Z")
MODE_FUNCTIONS = {
    "DCV": {"VOLT", "VOLT:DC", "VOLTAGE:DC"},
    "ACV": {"VOLT:AC", "VOLTAGE:AC"},
    "DCI": {"CURR", "CURR:DC", "CURRENT", "CURRENT:DC"},
    "ACI": {"CURR:AC", "CURRENT:AC"},
    "RES2": {"RES", "RESISTANCE"},
    "RES4": {"FRES", "FRESISTANCE"},
}
MODE_UNITS = {"DCV": "V", "ACV": "V", "DCI": "A", "ACI": "A", "RES2": "Ohm", "RES4": "Ohm"}
MODE_CONFIRMATIONS = {
    "DCV": {"front_panel_voltage_input_adjustable_source"},
    "ACV": {"front_panel_acv_voltage_source"},
    "DCI": {"front_panel_dci_5ma_series"},
    "ACI": {"front_panel_aci_current_series"},
    "RES2": {"front_panel_2w_short", "front_panel_2w_30mohm"},
    "RES4": {"front_panel_res4_four_wire_unpowered_dut"},
}
SUPPORTED_RANGES = {
    "ACV": (0.1, 1.0, 10.0, 100.0, 750.0),
    "RES4": (100.0, 1e3, 10e3, 100e3, 1e6, 10e6, 100e6),
}
AC_BANDWIDTHS_HZ = (3, 20, 200)


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


def _finite_number(value: object, label: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be a finite number") from exc
    if not math.isfinite(number):
        raise ValueError(f"{label} must be a finite number")
    return number


def _normalize_function(value: object) -> str:
    return str(value or "").strip().strip('"\'').upper()


def _validate_runtime_source_identity(report: dict) -> dict:
    if report.get("runtime_source_identity_verified_at_finish") is not True:
        raise ValueError("Probe did not verify that its runtime source identity remained unchanged")
    identity = report.get("runtime_source_identity")
    if not isinstance(identity, dict):
        raise ValueError("Report is missing runtime source identity")

    if identity.get("kind") == "source_tree_files":
        hashes = identity.get("source_file_sha256")
        if not isinstance(hashes, dict) or set(hashes) != set(SOURCE_FILES):
            raise ValueError("Runtime source identity file set is missing or inconsistent")
        normalized_hashes = {}
        for name in SOURCE_FILES:
            value = hashes.get(name)
            if not isinstance(value, str) or not SHA256_PATTERN.fullmatch(value):
                raise ValueError(f"Runtime source identity has an invalid SHA-256 for {name}")
            normalized_hashes[name] = value.upper()
        return {"kind": "source_tree_files", "source_file_sha256": normalized_hashes}

    if identity.get("kind") == "frozen_executable":
        head_commit = identity.get("head_commit")
        source_digest = identity.get("source_tree_sha256")
        executable_digest = identity.get("executable_sha256")
        if not isinstance(head_commit, str) or not GIT_COMMIT_PATTERN.fullmatch(head_commit):
            raise ValueError("Frozen runtime source identity has an invalid Git commit")
        if not isinstance(identity.get("worktree_dirty"), bool):
            raise ValueError("Frozen runtime source identity has no Boolean worktree state")
        if not isinstance(source_digest, str) or not SHA256_PATTERN.fullmatch(source_digest):
            raise ValueError("Frozen runtime source identity has an invalid source tree digest")
        if not isinstance(executable_digest, str) or not SHA256_PATTERN.fullmatch(executable_digest):
            raise ValueError("Frozen runtime source identity has an invalid EXE digest")
        return {
            "kind": "frozen_executable",
            "head_commit": head_commit.lower(),
            "worktree_dirty": identity["worktree_dirty"],
            "source_tree_sha256": source_digest.upper(),
            "executable_sha256": executable_digest.upper(),
        }

    raise ValueError("Runtime source identity kind is unsupported")


def _validate_mode_evidence(mode: str, report: dict) -> dict:
    if report.get("operator_wiring_confirmation") not in MODE_CONFIRMATIONS[mode]:
        raise ValueError(f"Report is missing the required {mode} wiring confirmation")

    for label, state in (
        ("pre-configuration", report.get("pre_configuration")),
        ("initial configuration readback", report.get("initial_configuration_readback")),
    ):
        if not isinstance(state, dict):
            raise ValueError(f"Report is missing {label} for {mode}")
        if _normalize_function(state.get("function")) not in MODE_FUNCTIONS[mode]:
            raise ValueError(f"{label} function does not match {mode}")
        if not _normalize_function(state.get("input_terminals")).startswith("FRON"):
            raise ValueError(f"{label} does not confirm front-panel terminals")

    if mode == "ACV":
        source = report.get("acv_source")
        if not isinstance(source, dict):
            raise ValueError("ACV report is missing declared source conditions")
        rms_v = _finite_number(source.get("nominal_voltage_rms_v"), "ACV RMS source voltage")
        peak_v = _finite_number(source.get("declared_voltage_peak_v"), "ACV source peak voltage")
        frequency_hz = _finite_number(source.get("frequency_hz"), "ACV source frequency")
        range_v = _finite_number(source.get("range_v"), "ACV fixed range")
        bandwidth_value = _finite_number(source.get("ac_bandwidth_hz"), "ACV bandwidth")
        if bandwidth_value not in AC_BANDWIDTHS_HZ:
            raise ValueError("ACV bandwidth must exactly match a documented 3/20/200 Hz setting")
        bandwidth_hz = int(bandwidth_value)
        if (
            rms_v <= 0 or rms_v > 750 or rms_v > range_v
            or peak_v < rms_v or peak_v > 1000
            or range_v not in SUPPORTED_RANGES["ACV"]
            or bandwidth_hz not in AC_BANDWIDTHS_HZ
            or frequency_hz < max(20, bandwidth_hz) or frequency_hz > 300_000
            or rms_v * frequency_hz > 8e7
        ):
            raise ValueError("ACV source conditions exceed the documented 8845A input limits")
        readback = report["initial_configuration_readback"]
        if not math.isclose(_finite_number(readback.get("range_response"), "ACV range readback"), range_v):
            raise ValueError("ACV source range does not match the initial instrument readback")
        readback_bandwidth = _finite_number(
            readback.get("ac_bandwidth_response"), "ACV bandwidth readback",
        )
        if readback_bandwidth not in AC_BANDWIDTHS_HZ:
            raise ValueError("ACV bandwidth readback is not an exact documented 3/20/200 Hz setting")
        if readback_bandwidth != bandwidth_hz:
            raise ValueError("ACV bandwidth does not match the initial instrument readback")
        return {
            "declared_source_rms_v": rms_v,
            "declared_source_peak_v": peak_v,
            "declared_source_frequency_hz": frequency_hz,
            "fixed_range_v": range_v,
            "ac_bandwidth_hz": bandwidth_hz,
            "source_conditions_independently_verified": False,
        }

    if mode == "RES4":
        dut = report.get("res4_dut")
        if (
            not isinstance(dut, dict)
            or dut.get("operator_confirmed_unpowered_isolated_discharged") is not True
            or dut.get("four_wire_topology_independently_verified") is not False
        ):
            raise ValueError("RES4 report is missing explicit DUT safety and four-wire confirmation")
        range_ohm = _finite_number(dut.get("range_ohm"), "RES4 fixed range")
        if range_ohm not in SUPPORTED_RANGES["RES4"]:
            raise ValueError("RES4 range is outside the documented 8845A range list")
        readback = _finite_number(
            report["initial_configuration_readback"].get("range_response"), "RES4 range readback",
        )
        if not math.isclose(readback, range_ohm):
            raise ValueError("RES4 range does not match the initial instrument readback")
        return {
            "fixed_range_ohm": range_ohm,
            "operator_confirmed_unpowered_isolated_discharged": True,
            "four_wire_topology_independently_verified": False,
        }
    return {}


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
        raise ValueError("Expected a successful real GUI probe for one of the six 8845A modes")
    mode = match.group(1).upper()
    if report.get("mode", mode) != mode:
        raise ValueError("Probe report mode does not match its probe type")
    device = report.get("device") or {}
    if str(device.get("manufacturer", "")).strip().upper() != "FLUKE":
        raise ValueError("Report does not identify FLUKE as the manufacturer")
    if str(device.get("model", "")).strip().upper() != "8845A":
        raise ValueError("Report does not identify a Fluke 8845A")
    experiment = project_file(Path(report["experiment_directory"]), root)
    if not experiment.is_dir():
        raise ValueError("Experiment path must be a directory")
    config_path = project_file(experiment / "config.json", root)
    config = json.loads(config_path.read_text(encoding="utf-8-sig"))
    if (
        config.get("mode") != mode or config.get("simulation") is not False
        or config.get("unit_si") != MODE_UNITS[mode]
    ):
        raise ValueError(f"Experiment config is not real {mode} data")
    runtime_source_identity = _validate_runtime_source_identity(report)
    mode_conditions = _validate_mode_evidence(mode, report)

    measurements = project_file(experiment / "measurements.csv", root)
    with measurements.open("r", encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.DictReader(stream))
    if not rows or len(rows) != report.get("sample_count"):
        raise ValueError("CSV row count does not match the probe report")
    invalid_count = sum(row.get("status") != "ok" for row in rows)
    if report.get("invalid_count") != invalid_count:
        raise ValueError("CSV invalid count does not match the probe report")
    if invalid_count or any(not row.get("raw_reading") for row in rows):
        raise ValueError("CSV contains missing or invalid measurements")
    for row in rows:
        if row.get("mode") != mode or row.get("unit_si") != MODE_UNITS[mode]:
            raise ValueError("CSV mode or unit does not match the experiment configuration")
        raw_value = _finite_number(row.get("raw_reading"), "CSV raw reading")
        value_si = _finite_number(row.get("value_si"), "CSV measurement value")
        if abs(raw_value) >= 9e37:
            raise ValueError("CSV raw reading contains an instrument overflow value")
        if not math.isclose(raw_value, value_si, rel_tol=1e-12, abs_tol=0.0):
            raise ValueError("CSV raw reading does not match the stored SI value")
        elapsed_value = _finite_number(row.get("elapsed_s"), "CSV elapsed timestamp")
        duration_value = _finite_number(row.get("read_duration_s"), "CSV read duration")
        if elapsed_value < 0 or duration_value < 0:
            raise ValueError("CSV elapsed timestamps and read durations must be nonnegative")
    elapsed = [float(row["elapsed_s"]) for row in rows]
    if any(right <= left for left, right in zip(elapsed, elapsed[1:])):
        raise ValueError("CSV elapsed timestamps are not strictly increasing")
    receipt_times = [
        float(row["elapsed_s"]) + float(row["read_duration_s"]) for row in rows
    ][-21:]
    if any(right <= left for left, right in zip(receipt_times, receipt_times[1:])):
        raise ValueError("CSV response receipt timestamps are not strictly increasing")
    receipt_span = receipt_times[-1] - receipt_times[0]
    if len(receipt_times) < 2 or receipt_span <= 0:
        raise ValueError("CSV has insufficient strictly increasing response timestamps")
    reconstructed_rate = (len(receipt_times) - 1) / receipt_span
    reported_rate = _finite_number(
        report.get("csv_reconstructed_actual_rate_hz"), "Reported reconstructed sample rate",
    )
    if not math.isclose(reconstructed_rate, reported_rate, rel_tol=1e-6, abs_tol=1e-9):
        raise ValueError("CSV reconstructed sample rate does not match the probe report")
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
        if runtime_source_identity["kind"] != "frozen_executable":
            raise ValueError("--exe can only be attached to a probe report from a frozen executable")
        executable = project_file(exe_path, root)
        if sha256(executable).upper() != runtime_source_identity["executable_sha256"]:
            raise ValueError("--exe SHA-256 does not match the executable recorded at probe time")
        evidence["executed_exe"] = executable
    visible_gui = report.get("visible_gui")
    if visible_gui:
        evidence["visible_gui_screenshot"] = project_file(Path(visible_gui["screenshot_path"]), root)

    manifest = {
        "manifest_version": 3,
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
        "mode_conditions": mode_conditions,
        "runtime_source_identity": runtime_source_identity,
        "evidence_files": {
            label: {"size_bytes": path.stat().st_size, "sha256": sha256(path)}
            for label, path in evidence.items()
        },
        "scope_note": (
            f"The hashed probe report records the application checks for real 8845A {mode} GUI communication, "
            "acquisition, plotting, CSV persistence, speed/interval changes and configuration restore. "
            "This manifest does not independently verify those actions or certify traceable measurement accuracy."
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
