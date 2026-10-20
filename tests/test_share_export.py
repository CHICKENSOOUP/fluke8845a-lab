from __future__ import annotations

import csv
import io
import json
import os
import stat
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from zipfile import ZipFile

import pytest

from fluke8845a import path_security, share_export
from fluke8845a.share_export import export_share_package


MEASUREMENT_BYTES = (
    b"\xef\xbb\xbfexperiment_id,sample_index,elapsed_s,value_si,raw_reading,error\r\n"
    b'run-1,1,0,1.0,=1+2,"COM3 SN-TEST8845A001 C:\\Users\\Alice\\run.csv \\\\lab-server\\share\\run.csv /home/alice/run.csv"\r\n'
)
EVENT_BYTES = (
    b"\xef\xbb\xbfexperiment_id,event_index,elapsed_s,event\r\n"
    b'run-1,1,0,"+event COM3 SN-TEST8845A001 D:\\Lab Data\\Alice\\run.csv \\\\lab-server\\share\\run.csv /home/alice/run.csv"\r\n'
)


def _config() -> dict:
    return {
        "experiment_id": "run-1",
        "mode": "DCV",
        "unit_si": "V",
        "mode_settings": {"autorange": False, "range_si": 10, "nplc": 10},
        "device": {
            "manufacturer": "FLUKE",
            "model": "8845A",
            "serial_number": "SN-TEST8845A001",
            "firmware": "02/14/08-08:54",
            "idn_raw": "FLUKE,8845A,SN-TEST8845A001,02/14/08-08:54",
        },
        "connection": {
            "port": "COM3",
            "serial_settings": {"baudrate": 115200, "bytesize": 8, "parity": "N", "stopbits": 1},
        },
        "output_root": "D:\\Lab Data\\Alice Smith\\Experiments\\run-1",
        "diagnostics": {
            "last_port": "connected through COM3",
            "message": "Saved in C:\\Users\\Alice Smith\\Documents\\Fluke runs\\run-1",
            "unc_path": "\\\\lab-server\\share\\Alice Smith\\run-1",
            "posix_path": "/home/alice/data/run-1",
            "serial_echo": "Instrument SN-TEST8845A001 was identified on COM3",
        },
    }


def _experiment(directory: Path, *, config: dict | None = None) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "measurements.csv").write_bytes(MEASUREMENT_BYTES)
    (directory / "events.csv").write_bytes(EVENT_BYTES)
    (directory / "config.json").write_text(
        json.dumps(_config() if config is None else config, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (directory / "run.log").write_text(
        "Connected on COM3; serial SN-TEST8845A001; D:\\Lab Data\\Alice Smith\\run-1\n",
        encoding="utf-8",
    )
    (directory / "unexpected.csv").write_text("must not be shared", encoding="utf-8")
    (directory / "nested").mkdir()
    (directory / "nested" / "measurements.csv").write_text("nested trap", encoding="utf-8")
    return directory


def _create_directory_junction(link: Path, target: Path) -> None:
    if sys.platform != "win32":
        pytest.skip("Windows directory junction integration test")
    command = (
        "New-Item -ItemType Junction "
        f"-Path '{str(link).replace(chr(39), chr(39) * 2)}' "
        f"-Target '{str(target).replace(chr(39), chr(39) * 2)}' | Out-Null"
    )
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        pytest.skip(f"could not create a directory junction: {result.stderr or result.stdout}")


def _all_strings(value):
    if isinstance(value, dict):
        for item in value.values():
            yield from _all_strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _all_strings(item)
    elif isinstance(value, str):
        yield value


def test_export_redacts_derived_csv_text_and_config_without_changing_sources(tmp_path):
    source = _experiment(tmp_path / "experiment")
    destination_marker = "DESTINATION_PATH_MUST_NOT_APPEAR_4891"
    output = tmp_path / destination_marker / "share.zip"
    output.parent.mkdir()

    result = export_share_package(source, output)

    assert result == output
    with ZipFile(output) as archive:
        assert set(archive.namelist()) == {"measurements.csv", "events.csv", "config.json"}
        shared_measurements = archive.read("measurements.csv")
        shared_events = archive.read("events.csv")
        shared_config_payload = archive.read("config.json")
        shared_config = json.loads(shared_config_payload)

    assert (source / "measurements.csv").read_bytes() == MEASUREMENT_BYTES
    assert (source / "events.csv").read_bytes() == EVENT_BYTES
    assert shared_measurements.startswith(b"\xef\xbb\xbf")
    assert shared_events.startswith(b"\xef\xbb\xbf")
    assert b"\r\n" in shared_measurements and b"\r\n" in shared_events
    measurement_rows = list(csv.DictReader(io.StringIO(shared_measurements.decode("utf-8-sig"), newline="")))
    event_rows = list(csv.DictReader(io.StringIO(shared_events.decode("utf-8-sig"), newline="")))
    assert measurement_rows[0]["value_si"] == "1.0"
    assert measurement_rows[0]["raw_reading"] == "'=1+2"
    assert measurement_rows[0]["sample_index"] == "1"
    assert "COM3" not in measurement_rows[0]["error"]
    assert "SN-TEST8845A001" not in measurement_rows[0]["error"]
    assert "C:\\Users" not in measurement_rows[0]["error"]
    assert "\\\\lab-server" not in measurement_rows[0]["error"]
    assert "/home/alice" not in measurement_rows[0]["error"]
    assert event_rows[0]["event_index"] == "1"
    assert event_rows[0]["event"].startswith("'+event")
    assert "COM3" not in event_rows[0]["event"]
    assert "SN-TEST8845A001" not in event_rows[0]["event"]
    assert "D:\\Lab Data" not in event_rows[0]["event"]
    assert "\\\\lab-server" not in event_rows[0]["event"]
    assert "/home/alice" not in event_rows[0]["event"]

    assert shared_config["mode"] == "DCV"
    assert shared_config["mode_settings"] == {"autorange": False, "range_si": 10, "nplc": 10}
    assert shared_config["device"]["manufacturer"] == "FLUKE"
    assert shared_config["device"]["model"] == "8845A"
    assert shared_config["device"]["firmware"] == "02/14/08-08:54"
    assert shared_config["device"]["serial_number"] != "SN-TEST8845A001"
    assert shared_config["connection"]["port"] != "COM3"
    assert shared_config["connection"]["serial_settings"]["baudrate"] == 115200

    all_values = "\n".join(_all_strings(shared_config))
    for secret in (
        "SN-TEST8845A001", "COM3", "C:\\Users", "D:\\Lab Data", "\\\\lab-server\\share", "/home/alice",
    ):
        assert secret not in all_values
    shared_archive_text = shared_measurements + shared_events + shared_config_payload
    for secret in (b"SN-TEST8845A001", b"COM3", b"C:\\Users", b"D:\\Lab Data", b"\\\\lab-server\\share", b"/home/alice"):
        assert secret.lower() not in shared_archive_text.lower()
    assert destination_marker.encode() not in output.read_bytes()


def test_export_escapes_formula_like_text_but_preserves_numeric_readings(tmp_path):
    source = _experiment(tmp_path / "experiment")
    measurement_bytes = (
        b"experiment_id,sample_index,elapsed_s,value_si,raw_reading,error\r\n"
        b"run-1,1,0,1.0,-1.25,\r\n"
        b"run-1,2,1,2.0,+2.0,\r\n"
        b"run-1,3,2,3.0,=1+2,\r\n"
    )
    event_rows = [
        ["experiment_id", "event_index", "elapsed_s", "event"],
        ["run-1", "1", "0", "=1+2"],
        ["run-1", "2", "1", "+command"],
        ["run-1", "3", "2", "-command"],
        ["run-1", "4", "3", "@SUM(1,2)"],
        ["run-1", "5", "4", " \t=HYPERLINK(\"https://example.invalid\")"],
    ]
    event_buffer = io.StringIO(newline="")
    csv.writer(event_buffer, lineterminator="\r\n").writerows(event_rows)
    event_bytes = b"\xef\xbb\xbf" + event_buffer.getvalue().encode("utf-8")
    (source / "measurements.csv").write_bytes(measurement_bytes)
    (source / "events.csv").write_bytes(event_bytes)
    output = tmp_path / "safe-share.zip"

    export_share_package(source, output)

    with ZipFile(output) as archive:
        measurements = list(csv.DictReader(io.StringIO(archive.read("measurements.csv").decode("utf-8-sig"))))
        events = list(csv.DictReader(io.StringIO(archive.read("events.csv").decode("utf-8-sig"))))
    assert [row["raw_reading"] for row in measurements] == ["-1.25", "+2.0", "'=1+2"]
    assert [row["event"] for row in events] == [
        "'=1+2",
        "'+command",
        "'-command",
        "'@SUM(1,2)",
        "' \t=HYPERLINK(\"https://example.invalid\")",
    ]
    assert (source / "measurements.csv").read_bytes() == measurement_bytes
    assert (source / "events.csv").read_bytes() == event_bytes


def test_share_package_preserves_numeric_raw_cells_but_redacts_text_in_raw_columns(tmp_path):
    source = _experiment(tmp_path / "experiment")
    measurements = source / "measurements.csv"
    measurements.write_text(
        "sample_index,elapsed_s,value_si,raw_reading,error\r\n"
        "1,0,1.0,1.00000000E+00,\r\n"
        '2,1,"COM3 SN-TEST8845A001 C:\\Users\\Alice\\secret.csv",2.0,\r\n',
        encoding="utf-8-sig",
        newline="",
    )
    output = tmp_path / "share.zip"

    export_share_package(source, output)

    with ZipFile(output) as archive:
        payload = archive.read("measurements.csv")
        rows = list(csv.DictReader(io.StringIO(payload.decode("utf-8-sig"), newline="")))

    assert rows[0]["value_si"] == "1.0"
    assert rows[0]["raw_reading"] == "1.00000000E+00"
    assert rows[1]["value_si"] == "[已脱敏]"
    shared_text = payload.decode("utf-8-sig")
    assert "COM3" not in shared_text
    assert "SN-TEST8845A001" not in shared_text
    assert "C:\\Users\\Alice" not in shared_text


def test_export_redacts_identity_and_path_text_inside_raw_reading(tmp_path):
    source = _experiment(tmp_path / "experiment")
    measurement_bytes = (
        b"experiment_id,sample_index,elapsed_s,value_si,raw_reading,error\r\n"
        b'run-1,1,0,1.0,"SN-TEST8845A001 observed on COM3 at C:\\Users\\Alice\\raw.txt",\r\n'
    )
    (source / "measurements.csv").write_bytes(measurement_bytes)
    output = tmp_path / "redacted-share.zip"

    export_share_package(source, output)

    with ZipFile(output) as archive:
        rows = list(csv.DictReader(io.StringIO(archive.read("measurements.csv").decode("utf-8-sig"))))
    raw_reading = rows[0]["raw_reading"]
    assert "SN-TEST8845A001" not in raw_reading
    assert "COM3" not in raw_reading
    assert "C:\\Users" not in raw_reading
    assert "[已脱敏]" in raw_reading
    assert "[本地路径已隐藏]" in raw_reading
    assert (source / "measurements.csv").read_bytes() == measurement_bytes


def test_export_never_overwrites_experiment_inputs_or_existing_destination(tmp_path):
    source = _experiment(tmp_path / "experiment")
    original_measurements = (source / "measurements.csv").read_bytes()

    with pytest.raises((FileExistsError, ValueError)):
        export_share_package(source, source / "measurements.csv")
    assert (source / "measurements.csv").read_bytes() == original_measurements

    existing = tmp_path / "existing.zip"
    existing.write_bytes(b"keep this output")
    with pytest.raises(FileExistsError):
        export_share_package(source, existing)
    assert existing.read_bytes() == b"keep this output"


def test_export_rejects_a_known_input_that_is_a_symlink(tmp_path):
    source = tmp_path / "experiment"
    source.mkdir()
    (source / "config.json").write_text(json.dumps(_config()), encoding="utf-8")
    outside = tmp_path / "private_measurements.csv"
    outside.write_bytes(b"private data")
    try:
        os.symlink(outside, source / "measurements.csv")
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation is unavailable in this Windows environment")

    with pytest.raises((OSError, ValueError), match="(?i)(symlink|symbolic|regular|安全|链接)"):
        export_share_package(source, tmp_path / "share.zip")


def test_export_rejects_a_hard_linked_measurement_input(tmp_path):
    source = _experiment(tmp_path / "experiment")
    external = tmp_path / "measurements-outside-experiment.csv"
    external.write_bytes(MEASUREMENT_BYTES)
    measurement = source / "measurements.csv"
    measurement.unlink()
    try:
        os.link(external, measurement)
    except (OSError, NotImplementedError):
        pytest.skip("hard-link creation is unavailable on this filesystem")

    with pytest.raises(ValueError, match="(?i)(hard link|硬链接|多个链接)"):
        export_share_package(source, tmp_path / "share.zip")
    assert not (tmp_path / "share.zip").exists()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows pinned-directory integration test")
def test_export_reuses_and_closes_one_pinned_directory_handle(tmp_path, monkeypatch):
    source = _experiment(tmp_path / "experiment")
    output = tmp_path / "share.zip"
    observed_pins = []
    open_verified = share_export._open_verified

    def record_pinned_open(*args, **kwargs):
        pinned = kwargs.get("pinned_directory")
        assert pinned is not None
        observed_pins.append(pinned)
        return open_verified(*args, **kwargs)

    monkeypatch.setattr(share_export, "_open_verified", record_pinned_open)

    export_share_package(source, output)

    assert len(observed_pins) == 3
    pinned = observed_pins[0]
    assert all(candidate is pinned for candidate in observed_pins)
    assert pinned.windows_handle is not None
    with pytest.raises(OSError):
        share_export._windows_file_id(pinned.windows_handle)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows pinned-directory cleanup test")
def test_export_closes_pinned_directory_handle_when_csv_copy_fails(tmp_path, monkeypatch):
    source = _experiment(tmp_path / "experiment")
    output = tmp_path / "share.zip"
    observed_pins = []
    open_verified = share_export._open_verified

    def record_pinned_open(*args, **kwargs):
        pinned = kwargs.get("pinned_directory")
        assert pinned is not None
        observed_pins.append(pinned)
        return open_verified(*args, **kwargs)

    def fail_csv_copy(*_args, **_kwargs):
        raise OSError("injected CSV copy failure")

    monkeypatch.setattr(share_export, "_open_verified", record_pinned_open)
    monkeypatch.setattr(share_export, "_copy_csv_member", fail_csv_copy)

    with pytest.raises(OSError, match="injected CSV copy failure"):
        export_share_package(source, output)

    assert len(observed_pins) == 1
    assert observed_pins[0].windows_handle is not None
    with pytest.raises(OSError):
        share_export._windows_file_id(observed_pins[0].windows_handle)
    assert not output.exists()


def test_export_rejects_a_reparse_point_experiment_directory(tmp_path, monkeypatch):
    source = _experiment(tmp_path / "experiment")
    monkeypatch.setattr(
        share_export,
        "_is_symlink_or_junction",
        lambda path: Path(path) == source,
        raising=False,
    )

    with pytest.raises(ValueError, match="(?i)(reparse|junction|symbolic|链接)"):
        export_share_package(source, tmp_path / "share.zip")
    assert not (tmp_path / "share.zip").exists()


def test_export_rejects_a_real_windows_junction_as_experiment_directory(tmp_path):
    target = _experiment(tmp_path / "real-experiment")
    junction = tmp_path / "junction-experiment"
    _create_directory_junction(junction, target)
    try:
        with pytest.raises(ValueError, match="(?i)(reparse|junction|symbolic|链接)"):
            export_share_package(junction, tmp_path / "share.zip")
        assert not (tmp_path / "share.zip").exists()
    finally:
        junction.rmdir()


def test_export_rejects_a_real_windows_junction_in_known_input_slot(tmp_path):
    source = tmp_path / "experiment"
    source.mkdir()
    (source / "config.json").write_text(json.dumps(_config()), encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "measurements.csv").write_bytes(MEASUREMENT_BYTES)
    junction = source / "measurements.csv"
    _create_directory_junction(junction, outside)
    try:
        with pytest.raises(ValueError, match="(?i)(reparse|junction|symbolic|链接)"):
            export_share_package(source, tmp_path / "share.zip")
        assert not (tmp_path / "share.zip").exists()
    finally:
        junction.rmdir()


def test_resolved_directory_detects_root_replacement_during_resolution(tmp_path, monkeypatch):
    source = _experiment(tmp_path / "experiment")
    preserved = tmp_path / "validated-before-race"
    original_resolve = Path.resolve
    replaced = False

    def replace_root_before_resolve(path, *args, **kwargs):
        nonlocal replaced
        if path == source and not replaced:
            replaced = True
            source.rename(preserved)
            source.mkdir()
        return original_resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", replace_root_before_resolve)

    with pytest.raises(ValueError, match="(?i)(changed|变化|替换|身份)"):
        share_export._resolved_directory(source)


def test_input_preflight_detects_directory_replaced_after_resolution(tmp_path):
    source = _experiment(tmp_path / "experiment")
    root, identity = share_export._resolved_directory(source)
    preserved = tmp_path / "validated-root"
    source.rename(preserved)
    source.mkdir()

    with pytest.raises(ValueError, match="(?i)(changed|变化|替换|身份)"):
        share_export._input_path(
            root, "measurements.csv", required=True, root_identity=identity,
        )


def test_export_rejects_a_reparse_point_known_input_file(tmp_path, monkeypatch):
    source = _experiment(tmp_path / "experiment")
    reparse_file = source / "measurements.csv"
    monkeypatch.setattr(
        share_export,
        "_is_symlink_or_junction",
        lambda path: Path(path) == reparse_file,
        raising=False,
    )

    with pytest.raises(ValueError, match="(?i)(reparse|junction|symbolic|链接)"):
        export_share_package(source, tmp_path / "share.zip")
    assert not (tmp_path / "share.zip").exists()


def test_open_verified_rejects_a_reparse_point_that_appears_after_preflight(tmp_path, monkeypatch):
    source = _experiment(tmp_path / "experiment")
    reparse_file = source / "measurements.csv"
    monkeypatch.setattr(
        share_export,
        "_is_symlink_or_junction",
        lambda path: Path(path) == reparse_file,
        raising=False,
    )

    with pytest.raises(ValueError, match="(?i)(reparse|junction|symbolic|链接)"):
        share_export._open_verified(reparse_file)


def test_reparse_point_detector_checks_windows_file_attribute(tmp_path, monkeypatch):
    details = SimpleNamespace(
        st_mode=stat.S_IFDIR,
        st_file_attributes=getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400),
    )
    monkeypatch.setattr(Path, "lstat", lambda _path: details)

    assert path_security.is_symlink_or_junction(tmp_path)


def test_export_requires_the_known_config_and_measurement_files(tmp_path):
    source = tmp_path / "experiment"
    source.mkdir()
    (source / "config.json").write_text(json.dumps(_config()), encoding="utf-8")
    (source / "notes.txt").write_text("not an experiment file", encoding="utf-8")

    with pytest.raises((FileNotFoundError, ValueError), match="(?i)(measurements|实验|缺少)"):
        export_share_package(source, tmp_path / "share.zip")
    assert not (tmp_path / "share.zip").exists()


def test_export_falls_back_when_hard_links_are_unavailable(tmp_path, monkeypatch):
    source = _experiment(tmp_path / "experiment")

    def no_hard_links(*_args, **_kwargs):
        raise OSError("hard links unavailable")

    monkeypatch.setattr(share_export.os, "link", no_hard_links)
    output = tmp_path / "share.zip"
    export_share_package(source, output)

    with ZipFile(output) as archive:
        assert "measurements.csv" in archive.namelist()


def test_export_never_overwrites_destination_created_after_preflight(tmp_path, monkeypatch):
    source = _experiment(tmp_path / "experiment")
    output = tmp_path / "share.zip"
    marker = b"another process created this destination"

    def destination_won_race(_temporary, destination):
        Path(destination).write_bytes(marker)
        raise FileExistsError("destination won race")

    monkeypatch.setattr(share_export.os, "link", destination_won_race)
    with pytest.raises(FileExistsError):
        export_share_package(source, output)
    assert output.read_bytes() == marker


def test_copy_csv_member_closes_input_if_newline_detection_fails(tmp_path, monkeypatch):
    source = _experiment(tmp_path / "experiment")
    resolved_root, root_identity = share_export._resolved_directory(source)
    opened_streams = []
    open_verified = share_export._open_verified

    def track_opened_stream(*args, **kwargs):
        stream, details = open_verified(*args, **kwargs)
        opened_streams.append(stream)
        return stream, details

    def fail_newline_detection(_stream):
        raise OSError("injected CSV read failure")

    monkeypatch.setattr(share_export, "_open_verified", track_opened_stream)
    monkeypatch.setattr(share_export, "_csv_line_terminator", fail_newline_detection)

    with ZipFile(tmp_path / "partial.zip", "w") as archive:
        with pytest.raises(OSError, match="injected CSV read failure"):
            share_export._copy_csv_member(
                archive, resolved_root, "measurements.csv", set(), root_identity
            )

    assert len(opened_streams) == 1
    assert opened_streams[0].closed


@pytest.mark.skipif(sys.platform != "win32", reason="Windows handle lifetime integration test")
def test_windows_relative_open_closes_stream_when_handle_inspection_fails(
    tmp_path, monkeypatch
):
    source = _experiment(tmp_path / "experiment")
    opened_streams = []
    real_fdopen = share_export.os.fdopen

    def track_fdopen(fd, mode):
        stream = real_fdopen(fd, mode)
        opened_streams.append(stream)
        return stream

    with share_export._windows_directory_handle(source) as (root_handle, _details, _file_id):
        monkeypatch.setattr(share_export.os, "fdopen", track_fdopen)

        def fail_handle_inspection(_handle):
            raise OSError("injected handle inspection failure")

        monkeypatch.setattr(share_export, "_windows_handle_attributes", fail_handle_inspection)
        with pytest.raises(OSError, match="injected handle inspection failure"):
            share_export._open_windows_relative_file(root_handle, "measurements.csv")

    assert len(opened_streams) == 1
    assert opened_streams[0].closed


@pytest.mark.skipif(sys.platform != "win32", reason="Windows directory-handle integration test")
def test_open_verified_reads_pinned_directory_when_path_is_replaced_after_preflight(
    tmp_path, monkeypatch
):
    root = _experiment(tmp_path / "experiment-a")
    alternate = _experiment(tmp_path / "experiment-b")
    input_path = root / "measurements.csv"
    (root / "measurements.csv").write_bytes(b"trusted A")
    (alternate / "measurements.csv").write_bytes(b"untrusted B")
    resolved_root, root_identity = share_export._resolved_directory(root)
    open_relative = getattr(share_export, "_open_windows_relative_file", None)
    opened_during_replacement = False
    opened_bytes = None
    displaced_root = tmp_path / "displaced-a"
    displaced_alternate = tmp_path / "displaced-b"

    def racing_relative_open(root_handle, filename):
        nonlocal opened_during_replacement, opened_bytes
        resolved_root.rename(displaced_root)
        alternate.rename(resolved_root)
        stream = open_relative(root_handle, filename)
        opened_bytes = stream.read()
        stream.seek(0)
        opened_during_replacement = True
        return stream

    monkeypatch.setattr(
        share_export,
        "_open_windows_relative_file",
        racing_relative_open,
        raising=False,
    )
    try:
        try:
            stream, _opened = share_export._open_verified(
                resolved_root / "measurements.csv",
                root=resolved_root,
                root_identity=root_identity,
            )
        except ValueError as exc:
            assert "变化" in str(exc)
        else:
            with stream:
                assert stream.read() == b"trusted A"
        assert opened_during_replacement
        assert opened_bytes == b"trusted A"
    finally:
        if displaced_root.exists():
            if resolved_root.exists() and not alternate.exists():
                resolved_root.rename(alternate)
            displaced_root.rename(resolved_root)
        if displaced_alternate.exists():
            displaced_alternate.rename(alternate)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows reparse-point integration test")
def test_open_verified_rejects_a_junction_that_appears_after_preflight(tmp_path, monkeypatch):
    root = _experiment(tmp_path / "experiment")
    outside = _experiment(tmp_path / "outside")
    input_path = root / "measurements.csv"
    saved_input = tmp_path / "measurements-original.csv"
    resolved_root, root_identity = share_export._resolved_directory(root)
    validated_path = share_export._input_path(
        resolved_root,
        "measurements.csv",
        required=True,
        root_identity=root_identity,
    )
    open_relative = share_export._open_windows_relative_file
    injected = False

    def replace_with_junction(root_handle, filename):
        nonlocal injected
        input_path.rename(saved_input)
        _create_directory_junction(input_path, outside)
        injected = True
        try:
            return open_relative(root_handle, filename)
        finally:
            if input_path.exists():
                input_path.rmdir()
            saved_input.rename(input_path)

    monkeypatch.setattr(share_export, "_open_windows_relative_file", replace_with_junction)
    try:
        with pytest.raises((OSError, ValueError)):
            share_export._open_verified(
                validated_path,
                root=resolved_root,
                root_identity=root_identity,
            )
        assert injected
    finally:
        if saved_input.exists() and not input_path.exists():
            saved_input.rename(input_path)
