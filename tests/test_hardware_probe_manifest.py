from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from scripts import create_hardware_probe_manifest as manifest_module


def prepare_probe(root: Path, *, simulation: bool = False) -> Path:
    (root / "build").mkdir(parents=True, exist_ok=True)
    for source in manifest_module.SOURCE_FILES:
        source_path = root / source
        source_path.parent.mkdir(parents=True, exist_ok=True)
        source_path.write_text("test source reference\n", encoding="utf-8")
    experiment = root / "build" / "experiment"
    experiment.mkdir()
    (experiment / "config.json").write_text(json.dumps({"simulation": simulation, "mode": "DCI"}), encoding="utf-8")
    (experiment / "events.csv").write_text("timestamp,event\n", encoding="utf-8")
    (experiment / "run.log").write_text("probe finished\n", encoding="utf-8")
    with (experiment / "measurements.csv").open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=("status", "raw_reading", "elapsed_s"))
        writer.writeheader()
        writer.writerow({"status": "ok", "raw_reading": "+5.000E-3", "elapsed_s": "0.0"})
        writer.writerow({"status": "ok", "raw_reading": "+5.001E-3", "elapsed_s": "0.2"})
    report = {
        "status": "ok",
        "simulation": simulation,
        "probe_type": "real_gui_dci_speed_and_interval_change",
        "mode": "DCI",
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
        "displayed_actual_rate": "5 Hz",
        "csv_reconstructed_actual_rate_hz": 5.0,
        "communication_log": ["> SYST:LOC"],
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


def test_manifest_refuses_mock_capture(monkeypatch, tmp_path):
    monkeypatch.setattr(manifest_module, "ROOT", tmp_path)
    report_path = prepare_probe(tmp_path, simulation=True)

    with pytest.raises(ValueError, match="unsuccessful or simulated"):
        manifest_module.create_manifest(report_path)


def test_manifest_refuses_evidence_outside_project(monkeypatch, tmp_path):
    monkeypatch.setattr(manifest_module, "ROOT", tmp_path / "project")
    (tmp_path / "project").mkdir()
    with pytest.raises(ValueError, match="inside the project"):
        manifest_module.create_manifest(tmp_path / "outside.json")
