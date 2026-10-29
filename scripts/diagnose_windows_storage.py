"""Collect rename and handle evidence in isolated test directories; no instrument I/O."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import uuid


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--handle", type=Path)
    args = parser.parse_args()
    output = ROOT / "build" / "test-artifacts" / "windows-storage-diagnostic" / uuid.uuid4().hex
    output.mkdir(parents=True)
    print(json.dumps({"python": sys.version, "platform": platform.platform(), "output": str(output)}), flush=True)

    from fluke8845a.storage import ExperimentRecorder, _WindowsDirectoryLease
    from fluke8845a.modes import Mode
    from fluke8845a.scpi import ModeSettings

    def handles(directory: Path) -> None:
        if args.handle is None:
            return
        result = subprocess.run(
            [str(args.handle), "-accepteula", "-nobanner", str(directory)],
            capture_output=True, text=True, errors="replace", timeout=15,
        )
        print(result.stdout, result.stderr, flush=True)

    replace = os.replace

    def traced_replace(source, destination, *replace_args, **replace_kwargs):
        try:
            return replace(source, destination, *replace_args, **replace_kwargs)
        except OSError as exc:
            print(json.dumps({"replace_error": str(exc), "source": str(source), "destination": str(destination)}), flush=True)
            handles(Path(source).parent)
            raise

    os.replace = traced_replace
    for leased in (False, True):
        for existing in (False, True):
            directory = output / f"lease-{leased}-existing-{existing}"
            directory.mkdir()
            lease = _WindowsDirectoryLease(directory) if leased else None
            errors = []
            try:
                for i in range(5):
                    source = directory / f"{i}.tmp"
                    target = directory / f"{i}.json"
                    if existing:
                        target.write_text("old", encoding="utf-8")
                    with source.open("x", encoding="utf-8") as stream:
                        stream.write("new")
                        stream.flush()
                        os.fsync(stream.fileno())
                    try:
                        os.replace(source, target)
                    except OSError as exc:
                        errors.append(str(exc))
            finally:
                if lease is not None:
                    lease.close()
            print(json.dumps({"leased": leased, "existing": existing, "errors": errors}), flush=True)

    try:
        recorder = ExperimentRecorder.create(output, "recorder", {"simulation": True}, ModeSettings(Mode.DCV))
        recorder.finish("diagnostic")
        print("recorder create/finish: passed", flush=True)
    except Exception as exc:
        print(f"recorder create/finish: {exc!r}", flush=True)

    import pytest
    return pytest.main([
        "-vv", "-s", "--tb=long", "--maxfail=3",
        "--basetemp", str(output / "pytest"),
        "tests/test_mock.py::test_mock_read_duration_and_recorded_interval_use_high_resolution_clock",
        "tests/test_gui.py::test_simple_workspace_mock_connect_start_stop_saves_csv",
        "tests/test_app.py::test_dci_gui_probe_confirms_target_and_restores_prior_range_with_fake_transport",
    ])


if __name__ == "__main__":
    raise SystemExit(main())
