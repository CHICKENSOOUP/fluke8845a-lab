from __future__ import annotations

import csv
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from fluke8845a.modes import Mode
from fluke8845a.scpi import ModeSettings, Reading
from fluke8845a.storage import ExperimentRecorder


def make_reading(value=1.2345678901234567, raw="+1.2345678901234567E+00"):
    now = datetime.now(timezone.utc)
    stamp = now.isoformat(timespec="microseconds")
    return Reading(raw, value, 10, 1_000_000_000, 1_025_000_000, stamp, now.astimezone().isoformat(timespec="microseconds"))


def test_experiment_writes_csv_incrementally_with_full_value_and_time(tmp_path):
    recorder = ExperimentRecorder.create(tmp_path, "long term", {"simulation": True, "device": {"model": "MOCK"}}, ModeSettings(Mode.DCV))
    first = make_reading()
    row = recorder.append_reading(first, mode=Mode.DCV)
    assert row["value_si"] == repr(1.2345678901234567)
    with (recorder.directory / "measurements.csv").open(newline="", encoding="utf-8-sig") as stream:
        rows = list(csv.DictReader(stream))
    assert len(rows) == 1  # visible before experiment completion
    assert rows[0]["raw_reading"] == "+1.2345678901234567E+00"
    assert rows[0]["timestamp_utc"].endswith("+00:00")
    assert rows[0]["timestamp_local"] == first.timestamp_local
    assert rows[0]["elapsed_s"] == "0.0"
    assert rows[0]["actual_interval_s"] == ""
    assert rows[0]["read_duration_s"] == "0.025"
    assert rows[0]["range_si"] == "10"
    assert rows[0]["nplc"] == "1.0"
    assert rows[0]["voltage_v"] == repr(1.2345678901234567)
    second_stamp = datetime.now(timezone.utc)
    second = Reading(
        "+1.2345678901234568E+00", 1.2345678901234568, 10,
        1_050_000_000, 1_075_000_000,
        second_stamp.isoformat(timespec="microseconds"),
        second_stamp.astimezone().isoformat(timespec="microseconds"),
    )
    recorder.append_reading(second, mode=Mode.DCV)
    with (recorder.directory / "measurements.csv").open(newline="", encoding="utf-8-sig") as stream:
        rows = list(csv.DictReader(stream))
    assert rows[1]["sample_index"] == "2"
    assert rows[1]["elapsed_s"] == "0.05"
    assert rows[1]["actual_interval_s"] == "0.05"
    assert rows[1]["raw_reading"] == "+1.2345678901234568E+00"
    recorder.finish("stopped")
    config = json.loads((recorder.directory / "config.json").read_text(encoding="utf-8"))
    assert config["simulation"] is True
    assert config["sample_count"] == 2
    assert config["completion_status"] == "stopped"


def test_failed_attempt_stays_blank_and_event_is_saved(tmp_path):
    recorder = ExperimentRecorder.create(tmp_path, "fault test", {"simulation": False}, ModeSettings(Mode.ACI, nplc=None, ac_bandwidth_hz=20))
    failed = recorder.append_reading(None, mode=Mode.ACI, status="timeout", error="response timeout", raw_reading="partial")
    recorder.append_event("load disconnected")
    with (recorder.directory / "measurements.csv").open(newline="", encoding="utf-8-sig") as stream:
        rows = list(csv.DictReader(stream))
    with (recorder.directory / "events.csv").open(newline="", encoding="utf-8-sig") as stream:
        events = list(csv.DictReader(stream))
    assert failed["value_si"] == ""
    assert rows[0]["status"] == "timeout"
    assert rows[0]["raw_reading"] == "partial"
    assert events[0]["event"] == "load disconnected"
    recorder.finish("test")


def test_generated_local_and_utc_timestamps_share_the_same_instant(tmp_path):
    recorder = ExperimentRecorder.create(tmp_path, "timestamp pair", {"simulation": True}, ModeSettings(Mode.DCV))
    sample = recorder.append_reading(None, mode=Mode.DCV, status="invalid", error="no reading")
    event = recorder.append_event("timestamp check")

    sample_local = datetime.fromisoformat(sample["timestamp_local"])
    sample_utc = datetime.fromisoformat(sample["timestamp_utc"])
    event_local = datetime.fromisoformat(event["timestamp_local"])
    event_utc = datetime.fromisoformat(event["timestamp_utc"])
    assert sample_local.astimezone(timezone.utc) == sample_utc
    assert event_local.astimezone(timezone.utc) == event_utc
    recorder.finish("stopped")


def test_constructor_closes_earlier_streams_if_a_later_open_fails(tmp_path, monkeypatch):
    opened_streams = []
    original_open = Path.open

    def fail_open_events(path, *args, **kwargs):
        if path.name == "events.csv":
            raise OSError("injected open failure")
        stream = original_open(path, *args, **kwargs)
        opened_streams.append(stream)
        return stream

    monkeypatch.setattr(Path, "open", fail_open_events)
    with pytest.raises(OSError, match="injected open failure"):
        ExperimentRecorder.create(tmp_path, "open failure", {"simulation": True}, ModeSettings(Mode.DCV))

    assert len(opened_streams) == 1
    assert opened_streams[0].closed


def test_finish_closes_all_files_even_if_metadata_write_fails(tmp_path, monkeypatch):
    recorder = ExperimentRecorder.create(tmp_path, "finish failure", {"simulation": True}, ModeSettings(Mode.DCV))
    streams = (recorder._data_stream, recorder._event_stream, recorder._log_stream)

    def fail_metadata_write():
        raise OSError("injected metadata failure")

    monkeypatch.setattr(recorder, "_write_metadata", fail_metadata_write)
    with pytest.raises(OSError, match="injected metadata failure"):
        recorder.finish("stopped")

    assert all(stream.closed for stream in streams)
    recorder.finish("stopped")  # a second close remains safe and does not repeat failed I/O

