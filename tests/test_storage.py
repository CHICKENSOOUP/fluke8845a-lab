from __future__ import annotations

import csv
import io
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

import fluke8845a.storage as storage_module
from fluke8845a.modes import Mode
from fluke8845a.scpi import ModeSettings, Reading
from fluke8845a.storage import ExperimentRecorder
from fluke8845a.storage import excel_safe_events_csv_bytes, write_excel_safe_events_csv


def make_reading(value=1.2345678901234567, raw="+1.2345678901234567E+00"):
    now = datetime.now(timezone.utc)
    stamp = now.isoformat(timespec="microseconds")
    return Reading(raw, value, 10, 1_000_000_000, 1_025_000_000, stamp, now.astimezone().isoformat(timespec="microseconds"))


def test_experiment_writes_csv_incrementally_with_full_value_and_time(tmp_path):
    recorder = ExperimentRecorder.create(tmp_path, "long term", {"simulation": True, "device": {"model": "MOCK"}}, ModeSettings(Mode.DCV, input_impedance_auto=True))
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
    assert rows[0]["input_impedance_auto"] == "True"

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
    assert config["mode_settings"]["input_impedance_auto"] is True


def test_speed_change_preserves_all_measurement_mode_settings(tmp_path):
    settings = ModeSettings(
        Mode.DCV, nplc=10, digital_filter=True, input_impedance_auto=True,
        analog_filter=True, autozero=False,
    )
    recorder = ExperimentRecorder.create(
        tmp_path, "speed change settings", {"simulation": True}, settings
    )

    recorder.record_measurement_speed_change(
        nplc=100, ac_bandwidth_hz=None, device_configuration={"nplc": 100}
    )

    assert recorder.settings.nplc == 100
    assert recorder.settings.digital_filter is True
    assert recorder.settings.input_impedance_auto is True
    assert recorder.settings.analog_filter is True
    assert recorder.settings.autozero is False
    recorder.finish()
    config = json.loads((recorder.directory / "config.json").read_text(encoding="utf-8"))
    assert config["mode_settings"]["digital_filter"] is True
    assert config["mode_settings"]["input_impedance_auto"] is True


def test_experiment_periodically_syncs_csv_rows_and_syncs_events_and_finish(
    tmp_path, monkeypatch
):
    sync_calls = []
    monkeypatch.setattr(storage_module, "CSV_FSYNC_INTERVAL_NS", 0, raising=False)
    monkeypatch.setattr(storage_module.os, "fsync", lambda fd: sync_calls.append(fd))
    recorder = ExperimentRecorder.create(
        tmp_path, "durability", {"simulation": True}, ModeSettings(Mode.DCV)
    )
    sync_calls.clear()  # Ignore the initial config.json fsync.

    recorder.append_reading(make_reading(), mode=Mode.DCV)
    assert len(sync_calls) == 2  # measurements.csv and events.csv

    sync_calls.clear()
    recorder.append_event("durable event")
    assert len(sync_calls) >= 2  # Both CSVs are synced; config.json is synced too.

    sync_calls.clear()
    recorder.finish("stopped")
    assert len(sync_calls) >= 2


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


@pytest.mark.parametrize("prefix", ["=", "+", "-", "@", " \t=", "\u00a0+", "\x01@"])
def test_excel_safe_event_export_escapes_formula_like_text_only(prefix):
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow(["experiment_id", "event_index", "value_si", "event"])
    writer.writerow(["id", 1, "-1.25", prefix + "cmd"])
    writer.writerow(["id", 2, "3.5", "ordinary, text"])
    source = buffer.getvalue().encode("utf-8-sig")
    exported = excel_safe_events_csv_bytes(source)
    rows = list(csv.DictReader(io.StringIO(exported.decode("utf-8-sig"), newline="")))
    assert rows[0]["event"] == "'" + prefix + "cmd"
    assert rows[0]["value_si"] == "-1.25"
    assert rows[1]["event"] == "ordinary, text"
    assert rows[1]["value_si"] == "3.5"
    assert source.startswith(b"\xef\xbb\xbf")  # source object is untouched


def test_excel_safe_event_export_preserves_quoted_newlines_and_rejects_wrong_schema():
    source = b'event,elapsed_s\r\n"line one\nline two",0.5\r\n'
    exported = excel_safe_events_csv_bytes(source)
    rows = list(csv.DictReader(io.StringIO(exported.decode("utf-8-sig"), newline="")))
    assert rows == [{"event": "line one\nline two", "elapsed_s": "0.5"}]
    with pytest.raises(ValueError, match="event"):
        excel_safe_events_csv_bytes(b"name,value\na,b\n")
    with pytest.raises(ValueError, match="列数不一致"):
        excel_safe_events_csv_bytes(b"event,elapsed_s\nordinary,0.5,unexpected\n")


def test_streaming_excel_safe_export_preserves_raw_source_and_replaces_only_destination(tmp_path):
    source = tmp_path / "events.csv"
    destination = tmp_path / "events_excel_safe.csv"
    original = (
        b"value_si,event\r\n"
        b"-2.5,=1+1\r\n"
        b"-3.5,+SUM(A1)\r\n"
        b"-4.5,-1+2\r\n"
        b"-5.5,@HYPERLINK\r\n"
    )
    source.write_bytes(original)
    write_excel_safe_events_csv(source, destination)
    assert source.read_bytes() == original
    with destination.open(encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.DictReader(stream))
    assert rows == [
        {"value_si": "-2.5", "event": "'=1+1"},
        {"value_si": "-3.5", "event": "'+SUM(A1)"},
        {"value_si": "-4.5", "event": "'-1+2"},
        {"value_si": "-5.5", "event": "'@HYPERLINK"},
    ]
    assert not list(tmp_path.glob(".events-safe-*.tmp"))


def test_streaming_excel_safe_export_never_overwrites_an_existing_destination(tmp_path):
    source = tmp_path / "events.csv"
    destination = tmp_path / "user-notes.csv"
    source.write_text("event,value\n=1+1,2\n", encoding="utf-8")
    destination.write_bytes(b"user data that must remain unchanged")

    with pytest.raises(FileExistsError, match="目标已存在"):
        write_excel_safe_events_csv(source, destination)

    assert destination.read_bytes() == b"user data that must remain unchanged"
    assert not list(tmp_path.glob(".events-safe-*.tmp"))


def test_streaming_excel_safe_export_does_not_replace_destination_on_malformed_csv(tmp_path):
    source = tmp_path / "events.csv"
    destination = tmp_path / "events_excel_safe.csv"
    source.write_text("event,value\ntext,1,extra\n", encoding="utf-8")
    destination.write_bytes(b"prior export")
    with pytest.raises(ValueError, match="列数不一致"):
        write_excel_safe_events_csv(source, destination)
    assert destination.read_bytes() == b"prior export"
    assert not list(tmp_path.glob(".events-safe-*.tmp"))


def test_streaming_excel_safe_export_refuses_to_overwrite_the_raw_events_file(tmp_path):
    source = tmp_path / "events.csv"
    original = b"event,value\n=1+1,3\n"
    source.write_bytes(original)
    with pytest.raises(ValueError, match="不能覆盖原始事件 CSV"):
        write_excel_safe_events_csv(source, source)
    assert source.read_bytes() == original
    alias = tmp_path / "events-alias.csv"
    alias.hardlink_to(source)
    with pytest.raises(ValueError, match="不能覆盖原始事件 CSV"):
        write_excel_safe_events_csv(source, alias)
    assert source.read_bytes() == original


def test_measurement_and_event_csv_share_a_persisted_experiment_identifier(tmp_path):
    recorder = ExperimentRecorder.create(tmp_path, "linked files", {"simulation": True}, ModeSettings(Mode.DCV))
    recorder.append_reading(make_reading(), mode=Mode.DCV)
    recorder.append_event("paired event")

    with (recorder.directory / "measurements.csv").open(newline="", encoding="utf-8-sig") as stream:
        measurement = next(csv.DictReader(stream))
    with (recorder.directory / "events.csv").open(newline="", encoding="utf-8-sig") as stream:
        event = next(csv.DictReader(stream))
    config = json.loads((recorder.directory / "config.json").read_text(encoding="utf-8"))

    assert recorder.metadata["experiment_id"]
    assert measurement["experiment_id"] == recorder.metadata["experiment_id"]
    assert event["experiment_id"] == recorder.metadata["experiment_id"]
    assert config["experiment_id"] == recorder.metadata["experiment_id"]
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


@pytest.mark.parametrize("name", ["bad/name", "bad\\name", "bad:name", "bad\x00name"])
def test_validate_experiment_name_rejects_windows_path_and_control_characters(name):
    with pytest.raises(ValueError, match="实验名称"):
        storage_module.validate_experiment_name(name)


def test_validate_experiment_name_preserves_valid_name():
    assert storage_module.validate_experiment_name("  阶跃响应 测试  ") == "阶跃响应 测试"


def test_prepare_experiment_storage_probes_and_cleans_writable_directory(tmp_path, monkeypatch):
    root = tmp_path / "new experiments"
    low_water = storage_module.MINIMUM_FREE_SPACE_BYTES
    monkeypatch.setattr(
        storage_module.shutil,
        "disk_usage",
        lambda _path: SimpleNamespace(total=low_water * 2, used=0, free=low_water),
    )

    prepared = storage_module.prepare_experiment_storage(root)

    assert prepared == root
    assert root.is_dir()
    assert list(root.iterdir()) == []


def test_prepare_experiment_storage_rejects_low_free_space(tmp_path, monkeypatch):
    root = tmp_path / "low disk"
    low_water = storage_module.MINIMUM_FREE_SPACE_BYTES
    monkeypatch.setattr(
        storage_module.shutil,
        "disk_usage",
        lambda _path: SimpleNamespace(total=low_water * 2, used=low_water + 1, free=low_water - 1),
    )

    with pytest.raises(OSError, match="可用空间不足"):
        storage_module.prepare_experiment_storage(root)

    assert root.is_dir()
    assert list(root.iterdir()) == []


def test_recorder_stops_appending_when_free_space_drops_below_reserve(tmp_path, monkeypatch):
    recorder = ExperimentRecorder.create(tmp_path, "low-space-run", {"simulation": True}, ModeSettings(Mode.DCV))
    monkeypatch.setattr(
        storage_module.shutil,
        "disk_usage",
        lambda _path: SimpleNamespace(total=100, used=100, free=storage_module.MINIMUM_FREE_SPACE_BYTES - 1),
    )
    try:
        with pytest.raises(OSError, match="已停止继续写入"):
            recorder.append_reading(make_reading(1.0, "+1E+00"), mode=Mode.DCV)
        assert recorder.sample_count == 0
        assert recorder.metadata["sample_count"] == 0
    finally:
        recorder.finish("stopped")


def test_prepare_experiment_storage_reports_write_probe_failure(tmp_path, monkeypatch):
    root = tmp_path / "unwritable"
    low_water = storage_module.MINIMUM_FREE_SPACE_BYTES
    monkeypatch.setattr(
        storage_module.shutil,
        "disk_usage",
        lambda _path: SimpleNamespace(total=low_water * 2, used=0, free=low_water),
    )

    def fail_probe(**_kwargs):
        raise PermissionError("access denied")

    monkeypatch.setattr(storage_module.tempfile, "NamedTemporaryFile", fail_probe)
    with pytest.raises(OSError, match="不可写"):
        storage_module.prepare_experiment_storage(root)


def test_same_experiment_name_creates_distinct_directories_without_overwriting(tmp_path, monkeypatch):
    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 10, 15, 12, 0, 0, tzinfo=tz)

    monkeypatch.setattr(storage_module, "datetime", FrozenDateTime)
    settings = ModeSettings(Mode.DCV)
    first = ExperimentRecorder.create(tmp_path, "same name", {"simulation": True}, settings)
    first.append_reading(make_reading(1.0, "+1E+00"), mode=Mode.DCV)
    second = ExperimentRecorder.create(tmp_path, "same name", {"simulation": True}, settings)

    assert first.directory != second.directory
    assert first.directory.name.endswith("_same_name")
    assert second.directory.name.endswith("_same_name_1")
    assert storage_module.find_existing_experiment_directories(tmp_path, "same name") == [
        first.directory, second.directory,
    ]
    with (first.directory / "measurements.csv").open(newline="", encoding="utf-8-sig") as stream:
        assert len(list(csv.DictReader(stream))) == 1
    with (second.directory / "measurements.csv").open(newline="", encoding="utf-8-sig") as stream:
        assert list(csv.DictReader(stream)) == []
    first.finish("stopped")
    second.finish("stopped")


def test_same_experiment_name_detection_matches_windows_case_insensitive_folders(tmp_path, monkeypatch):
    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 10, 15, 12, 0, 0, tzinfo=tz)

    monkeypatch.setattr(storage_module, "datetime", FrozenDateTime)
    recorder = ExperimentRecorder.create(tmp_path, "Test Run", {"simulation": True}, ModeSettings(Mode.DCV))
    try:
        assert storage_module.find_existing_experiment_directories(tmp_path, "test run") == [recorder.directory]
    finally:
        recorder.finish("stopped")

