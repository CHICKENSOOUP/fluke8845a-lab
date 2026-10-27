from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path

import pytest

from scripts import create_hardware_probe_manifest as manifest_module


def prepare_probe(root: Path, *, simulation: bool = False, mode: str = "DCI") -> Path:
    (root / "build").mkdir(parents=True, exist_ok=True)
    for source in manifest_module.SOURCE_FILES:
        source_path = root / source
        source_path.parent.mkdir(parents=True, exist_ok=True)
        source_path.write_text("test source reference\n", encoding="utf-8")
    experiment = root / "build" / "experiment"
    experiment.mkdir()
    mode_details = {
        "DCV": ("VOLT:DC", "V", 10.0, "front_panel_voltage_input_adjustable_source"),
        "ACV": ("VOLT:AC", "V", 750.0, "front_panel_acv_voltage_source"),
        "DCI": ("CURR:DC", "A", 10e-3, "front_panel_dci_5ma_series"),
        "ACI": ("CURR:AC", "A", 10e-3, "front_panel_aci_current_series"),
        "RES2": ("RES", "Ohm", 100e6, "front_panel_2w_30mohm"),
        "RES4": ("FRES", "Ohm", 100e6, "front_panel_res4_four_wire_unpowered_dut"),
    }
    function, unit, selected_range, confirmation = mode_details[mode]
    (experiment / "config.json").write_text(json.dumps({
        "simulation": simulation, "mode": mode, "unit_si": unit,
    }), encoding="utf-8")
    (experiment / "events.csv").write_text("timestamp,event\n", encoding="utf-8")
    (experiment / "run.log").write_text("probe finished\n", encoding="utf-8")
    with (experiment / "measurements.csv").open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=(
            "status", "raw_reading", "value_si", "unit_si", "mode", "elapsed_s", "read_duration_s",
        ))
        writer.writeheader()
        writer.writerow({"status": "ok", "raw_reading": "+5.000E-3", "value_si": "0.005", "unit_si": unit, "mode": mode, "elapsed_s": "0.0", "read_duration_s": "0.02"})
        writer.writerow({"status": "ok", "raw_reading": "+5.001E-3", "value_si": "0.005001", "unit_si": unit, "mode": mode, "elapsed_s": "0.2", "read_duration_s": "0.02"})
    report = {
        "status": "ok",
        "simulation": simulation,
        "probe_type": f"real_gui_{mode.lower()}_speed_and_interval_change",
        "mode": mode,
        "device": {
            "manufacturer": "FLUKE",
            "model": "8845A",
            "serial_number": "PRIVATE-SERIAL-123",
            "idn_raw": "FLUKE,8845A,PRIVATE-SERIAL-123,02/14/08-08:54",
            "firmware": "02/14/08-08:54",
        },
        "baudrate": 115200,
        "experiment_directory": str(experiment),
        "sample_count": 2,
        "invalid_count": 0,
        "collection_duration_s": 8.0,
        "displayed_actual_interval": "200 ms",
        "csv_reconstructed_actual_rate_hz": 5.0,
        "operator_wiring_confirmation": confirmation,
        "pre_configuration": {"function": function, "input_terminals": "FRON"},
        "initial_configuration_readback": {
            "function": function, "input_terminals": "FRON", "range_response": repr(selected_range),
            "ac_bandwidth_response": "20" if mode in {"ACV", "ACI"} else None,
        },
        "communication_log": ["> SYST:LOC"],
        "runtime_source_identity": {
            "kind": "source_tree_files",
            "source_file_sha256": {
                source: hashlib.sha256((root / source).read_bytes()).hexdigest().upper()
                for source in manifest_module.SOURCE_FILES
            },
        },
        "runtime_source_identity_verified_at_finish": True,
    }
    if mode == "ACV":
        report["acv_source"] = {
            "nominal_voltage_rms_v": 120.0, "declared_voltage_peak_v": 170.0,
            "frequency_hz": 1000.0, "range_v": 750.0, "ac_bandwidth_hz": 20,
        }
    if mode == "RES4":
        report["res4_dut"] = {
            "range_ohm": 100e6, "operator_confirmed_unpowered_isolated_discharged": True,
            "four_wire_topology_independently_verified": False,
        }
    report_path = root / "build" / "probe.json"
    report_path.write_text(json.dumps(report), encoding="utf-8")
    return report_path


def test_manifest_hashes_successful_real_probe_without_identifying_device(monkeypatch, tmp_path):
    monkeypatch.setattr(manifest_module, "ROOT", tmp_path)
    report_path = prepare_probe(tmp_path)

    output = manifest_module.create_manifest(report_path)

    content = output.read_text(encoding="utf-8")
    payload = json.loads(content)
    assert payload["simulation"] is False
    assert payload["mode"] == "DCI"
    assert payload["model"] == "8845A"
    assert payload["sample_count"] == 2
    assert "measurements_csv" in payload["evidence_files"]
    assert "PRIVATE-SERIAL-123" not in content
    assert "serial_number" not in payload
    assert "idn_raw" not in payload
    assert payload["runtime_source_identity"]["source_file_sha256"] == json.loads(
        report_path.read_text(encoding="utf-8")
    )["runtime_source_identity"]["source_file_sha256"]


def test_manifest_preserves_source_identity_captured_at_probe_time(monkeypatch, tmp_path):
    monkeypatch.setattr(manifest_module, "ROOT", tmp_path)
    report_path = prepare_probe(tmp_path)
    report = json.loads(report_path.read_text(encoding="utf-8"))
    captured_hashes = report["runtime_source_identity"]["source_file_sha256"]
    (tmp_path / "src" / "fluke8845a" / "app.py").write_text("later source revision\n", encoding="utf-8")
    report_path.write_text(json.dumps(report), encoding="utf-8")

    output = manifest_module.create_manifest(report_path)

    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["runtime_source_identity"]["source_file_sha256"] == captured_hashes


def test_manifest_requires_probe_time_source_identity(monkeypatch, tmp_path):
    monkeypatch.setattr(manifest_module, "ROOT", tmp_path)
    report_path = prepare_probe(tmp_path)
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report.pop("runtime_source_identity")
    report_path.write_text(json.dumps(report), encoding="utf-8")

    with pytest.raises(ValueError, match="runtime source identity"):
        manifest_module.create_manifest(report_path)


def test_manifest_hashes_only_the_frozen_exe_recorded_at_probe_time(monkeypatch, tmp_path):
    monkeypatch.setattr(manifest_module, "ROOT", tmp_path)
    report_path = prepare_probe(tmp_path)
    executable = tmp_path / "dist" / "Fluke8845A.exe"
    executable.parent.mkdir()
    executable.write_bytes(b"the exact running binary")
    executable_digest = hashlib.sha256(executable.read_bytes()).hexdigest().upper()
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report["runtime_source_identity"] = {
        "kind": "frozen_executable",
        "head_commit": "a" * 40,
        "worktree_dirty": False,
        "source_tree_sha256": "B" * 64,
        "executable_sha256": executable_digest,
    }
    report_path.write_text(json.dumps(report), encoding="utf-8")

    output = manifest_module.create_manifest(report_path, exe_path=executable)

    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["runtime_source_identity"]["executable_sha256"] == executable_digest
    assert payload["evidence_files"]["executed_exe"]["sha256"] == executable_digest


def test_manifest_rejects_frozen_exe_that_differs_from_probe_time(monkeypatch, tmp_path):
    monkeypatch.setattr(manifest_module, "ROOT", tmp_path)
    report_path = prepare_probe(tmp_path)
    executable = tmp_path / "dist" / "Fluke8845A.exe"
    executable.parent.mkdir()
    executable.write_bytes(b"a later different binary")
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report["runtime_source_identity"] = {
        "kind": "frozen_executable",
        "head_commit": "a" * 40,
        "worktree_dirty": False,
        "source_tree_sha256": "B" * 64,
        "executable_sha256": "C" * 64,
    }
    report_path.write_text(json.dumps(report), encoding="utf-8")

    with pytest.raises(ValueError, match="does not match the executable recorded"):
        manifest_module.create_manifest(report_path, exe_path=executable)


def test_manifest_refuses_mock_capture(monkeypatch, tmp_path):
    monkeypatch.setattr(manifest_module, "ROOT", tmp_path)
    report_path = prepare_probe(tmp_path, simulation=True)

    with pytest.raises(ValueError, match="unsuccessful or simulated"):
        manifest_module.create_manifest(report_path)


@pytest.mark.parametrize("mode", ["ACV", "RES4"])
def test_manifest_accepts_acv_and_res4_probe_types(monkeypatch, tmp_path, mode):
    monkeypatch.setattr(manifest_module, "ROOT", tmp_path)
    report_path = prepare_probe(tmp_path, mode=mode)

    output = manifest_module.create_manifest(report_path)

    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["mode"] == mode
    assert payload["probe_type"] == f"real_gui_{mode.lower()}_speed_and_interval_change"


@pytest.mark.parametrize("bad_bandwidth", [20.9, 3.5, float("inf")])
def test_manifest_rejects_nonintegral_or_nonfinite_acv_bandwidth(
    monkeypatch, tmp_path, bad_bandwidth,
):
    monkeypatch.setattr(manifest_module, "ROOT", tmp_path)
    report_path = prepare_probe(tmp_path, mode="ACV")
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report["acv_source"]["ac_bandwidth_hz"] = bad_bandwidth
    report["initial_configuration_readback"]["ac_bandwidth_response"] = str(bad_bandwidth)
    report_path.write_text(json.dumps(report), encoding="utf-8")

    with pytest.raises((ValueError, OverflowError), match="bandwidth|finite|documented"):
        manifest_module.create_manifest(report_path)


def test_manifest_rejects_nonintegral_acv_bandwidth_readback(monkeypatch, tmp_path):
    monkeypatch.setattr(manifest_module, "ROOT", tmp_path)
    report_path = prepare_probe(tmp_path, mode="ACV")
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report["initial_configuration_readback"]["ac_bandwidth_response"] = "20.9"
    report_path.write_text(json.dumps(report), encoding="utf-8")

    with pytest.raises(ValueError, match="bandwidth readback"):
        manifest_module.create_manifest(report_path)


def test_manifest_rejects_nonmonotonic_csv_receipt_timestamps(monkeypatch, tmp_path):
    monkeypatch.setattr(manifest_module, "ROOT", tmp_path)
    report_path = prepare_probe(tmp_path)
    csv_path = tmp_path / "build" / "experiment" / "measurements.csv"
    with csv_path.open("r", encoding="utf-8", newline="") as stream:
        rows = list(csv.DictReader(stream))
    rows[1]["read_duration_s"] = "1.0"
    rows.append({
        **rows[-1], "raw_reading": "+5.002E-3", "value_si": "0.005002",
        "elapsed_s": "0.4", "read_duration_s": "0.02",
    })
    with csv_path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report["sample_count"] = 3
    report_path.write_text(json.dumps(report), encoding="utf-8")

    with pytest.raises(ValueError, match="receipt timestamps"):
        manifest_module.create_manifest(report_path)


def test_manifest_refuses_evidence_outside_project(monkeypatch, tmp_path):
    monkeypatch.setattr(manifest_module, "ROOT", tmp_path / "project")
    (tmp_path / "project").mkdir()
    with pytest.raises(ValueError, match="inside the project"):
        manifest_module.create_manifest(tmp_path / "outside.json")


@pytest.mark.parametrize(
    ("column", "value"),
    [("value_si", "NaN"), ("raw_reading", "NaN"), ("elapsed_s", "inf")],
)
def test_manifest_refuses_nonfinite_measurement_values(monkeypatch, tmp_path, column, value):
    monkeypatch.setattr(manifest_module, "ROOT", tmp_path)
    report_path = prepare_probe(tmp_path)
    csv_path = tmp_path / "build" / "experiment" / "measurements.csv"
    with csv_path.open("r", encoding="utf-8", newline="") as stream:
        rows = list(csv.DictReader(stream))
    rows[0][column] = value
    with csv_path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)

    with pytest.raises(ValueError, match="finite"):
        manifest_module.create_manifest(report_path)


def test_manifest_requires_csv_invalid_count_to_match_report(monkeypatch, tmp_path):
    monkeypatch.setattr(manifest_module, "ROOT", tmp_path)
    report_path = prepare_probe(tmp_path)
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report["invalid_count"] = 1
    report_path.write_text(json.dumps(report), encoding="utf-8")

    with pytest.raises(ValueError, match="invalid count"):
        manifest_module.create_manifest(report_path)


@pytest.mark.parametrize(
    ("mode", "field", "value"),
    [
        ("ACV", "operator_wiring_confirmation", "front_panel_voltage_input_adjustable_source"),
        ("RES4", "operator_wiring_confirmation", "front_panel_2w_30mohm"),
    ],
)
def test_manifest_requires_mode_specific_confirmation(monkeypatch, tmp_path, mode, field, value):
    monkeypatch.setattr(manifest_module, "ROOT", tmp_path)
    report_path = prepare_probe(tmp_path, mode=mode)
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report[field] = value
    report_path.write_text(json.dumps(report), encoding="utf-8")

    with pytest.raises(ValueError, match="wiring confirmation"):
        manifest_module.create_manifest(report_path)
