"""Run a guarded real-hardware RES2 throughput check at a selected NPLC.

This is a focused validation utility, not a replacement for the GUI. It refuses
to change instrument mode and requires an explicit confirmation that the user
has wired a 30 MOhm resistor to the front-panel 2-wire resistance terminals.
It uses the production SCPI driver and experiment recorder and restores 1 NPLC
before releasing the instrument to local control.
"""

from __future__ import annotations

import argparse
import math
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from fluke8845a.modes import Mode
from fluke8845a.scpi import (
    InstrumentError,
    ModeSettings,
    ReadingError,
    ScpiTimeout,
    Serial8845A,
    SerialSettings,
)
from fluke8845a.storage import ExperimentRecorder


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", default="COM3", help="8845A RS-232 COM port")
    parser.add_argument("--duration", type=float, default=30.0, help="capture duration in seconds")
    parser.add_argument("--output", type=Path, default=Path("output/hardware_validation"))
    parser.add_argument("--nplc", type=float, choices=(0.02, 0.2, 1, 10, 100), default=0.02,
                        help="documented 8845A integration setting to measure (default: 0.02)")
    parser.add_argument(
        "--confirm-front-2w-30mohm",
        action="store_true",
        help="confirm the front-panel 2W inputs are connected to the user's 30 MOhm resistor",
    )
    args = parser.parse_args()
    if not args.confirm_front_2w_30mohm:
        parser.error("must confirm the currently wired front-panel 2W 30 MOhm resistor")
    if not math.isfinite(args.duration) or args.duration <= 0:
        parser.error("duration must be positive")
    return args


def main() -> int:
    args = _arguments()
    settings = SerialSettings(
        baudrate=9600, bytesize=8, parity="N", stopbits=1,
        eol="CRLF", timeout_s=2.0,
    )
    test_settings = ModeSettings(
        Mode.RES2, autorange=False, range_si=100e6, nplc=args.nplc,
        analog_filter=False, autozero=True,
    )
    restore_settings = ModeSettings(
        Mode.RES2, autorange=False, range_si=100e6, nplc=1,
        analog_filter=False, autozero=True,
    )
    recorder: ExperimentRecorder | None = None
    instrument: Serial8845A | None = None
    pending_log: list[str] = []
    valid_receipts_ns: list[int] = []
    valid_values: list[float] = []
    attempt_count = 0
    consecutive_timeouts = 0
    error_text = ""
    completion = "script_error"
    restore_details = None

    def communication_log(line: str) -> None:
        if recorder is None:
            pending_log.append(line)
        elif line.startswith("! ") or (line.startswith("> ") and line != "> READ?"):
            recorder.log("SCPI", line)

    try:
        instrument = Serial8845A(args.port, settings, communication_log)
        info = instrument.connect()
        initial_state = instrument.read_mode_state()
        function = initial_state["function"].strip().strip('"').upper()
        terminals = initial_state["input_terminals"].strip().strip('"').upper()
        if function != "RES":
            raise InstrumentError(f"拒绝改动当前仪表功能：期望RES，实际{initial_state['function']}")
        if not terminals.startswith("FRON"):
            raise InstrumentError(f"拒绝测试：期望前面板端子，实际{initial_state['input_terminals']}")

        metadata = {
            "simulation": False,
            "validation_type": "real_8845a_res2_nplc_throughput",
            "requested_nplc": args.nplc,
            "device": {
                "manufacturer": info.manufacturer,
                "model": info.model,
                "serial_number": info.serial_number,
                "firmware": info.firmware,
                "idn_raw": info.idn_raw,
            },
            "connection": {
                "port": args.port,
                "serial_settings": {
                    "baudrate": settings.baudrate,
                    "bytesize": settings.bytesize,
                    "parity": settings.parity,
                    "stopbits": settings.stopbits,
                    "flow_control": settings.flow_control.value,
                    "eol": settings.eol,
                    "timeout_s": settings.timeout_s,
                },
            },
            "pre_configuration_instrument_state": initial_state,
            "target_interval_s": None,
            "initial_target_interval_s": None,
            "requested_duration_s": args.duration,
            "actual_interval_method": "PC monotonic timestamp delta from consecutive measurement attempts; frequency uses valid sample receipt timestamps.",
            "timestamp_definition": "timestamp_local/timestamp_utc and elapsed_s mark host command start. The instrument conversion is not hardware-timestamped; read_duration_s bounds serial command/read time.",
            "acquisition_method": "one READ? per point; the host reads every response before sending the next query.",
            "safety_confirmation": "User confirmed a 30 MOhm resistor is connected to the front-panel two-wire resistance terminals.",
        }
        recorder = ExperimentRecorder.create(
            args.output,
            f"COM3_RES2_{args.nplc:g}NPLC_fastest_{args.duration:g}s",
            metadata,
            test_settings,
        )
        for line in pending_log:
            if line.startswith("! ") or (line.startswith("> ") and line != "> READ?"):
                recorder.log("SCPI", line)
        pending_log.clear()

        actual = instrument.configure(test_settings)
        recorder.metadata["actual_device_configuration"] = actual
        recorder._write_metadata()
        applied_nplc = float(str(actual["nplc_response"]).strip().strip('"'))
        if abs(applied_nplc - args.nplc) > 1e-12:
            raise InstrumentError(f"仪表NPLC回读不匹配：期望{args.nplc:g}，实际{actual['nplc_response']}")

        completion = "stopped"
        deadline = time.monotonic() + args.duration
        while time.monotonic() < deadline:
            attempt_count += 1
            attempt_started_ns = time.monotonic_ns()
            try:
                reading = instrument.read()
            except Exception as exc:
                if isinstance(exc, (TimeoutError, ScpiTimeout)):
                    status = "timeout"
                    consecutive_timeouts += 1
                elif isinstance(exc, ReadingError):
                    status = "overflow" if "溢出" in str(exc) else "invalid"
                    consecutive_timeouts = 0
                else:
                    status = "communication_error"
                    consecutive_timeouts = 0
                error_text = str(exc)
                now = datetime.now(timezone.utc)
                recorder.append_reading(
                    None,
                    mode=Mode.RES2,
                    started_monotonic_ns=attempt_started_ns,
                    timestamp_local=now.astimezone().isoformat(timespec="microseconds"),
                    timestamp_utc=now.isoformat(timespec="microseconds"),
                    read_duration_s=(time.monotonic_ns() - attempt_started_ns) / 1e9,
                    status=status,
                    error=error_text,
                    raw_reading=exc.raw if isinstance(exc, ReadingError) else "",
                )
                recorder.log("ERROR", error_text)
                if not instrument.connected or consecutive_timeouts >= 3:
                    completion = "repeated_timeouts" if consecutive_timeouts >= 3 else "disconnected"
                    break
                if status == "communication_error":
                    completion = "instrument_error"
                    break
            else:
                recorder.append_reading(reading, mode=Mode.RES2)
                valid_receipts_ns.append(reading.received_monotonic_ns)
                valid_values.append(reading.value_si)
                consecutive_timeouts = 0

    except Exception as exc:
        error_text = str(exc)
        completion = "validation_error"
        if recorder is not None:
            recorder.log("ERROR", error_text)
        else:
            print(f"Hardware validation failed before recording: {error_text}", file=sys.stderr)
    finally:
        if instrument is not None and instrument.connected:
            try:
                restore_details = instrument.configure(restore_settings)
                if recorder is not None:
                    recorder.metadata["post_validation_restored_configuration"] = restore_details
                    recorder.log("INFO", "恢复RES2固定100 MΩ、1 NPLC、自动零点开启、模拟滤波关闭")
            except Exception as exc:
                if recorder is not None:
                    recorder.log("ERROR", f"恢复1 NPLC失败：{exc}")
                error_text = error_text or f"恢复1 NPLC失败：{exc}"
                if completion == "stopped":
                    completion = "restore_error"
            finally:
                instrument.close()

        if recorder is not None:
            if len(valid_receipts_ns) >= 2:
                intervals = [
                    (right - left) / 1e9
                    for left, right in zip(valid_receipts_ns, valid_receipts_ns[1:])
                ]
                actual_hz = (len(valid_receipts_ns) - 1) / (
                    (valid_receipts_ns[-1] - valid_receipts_ns[0]) / 1e9
                )
                median_interval = statistics.median(intervals)
            else:
                actual_hz = None
                median_interval = None
            recorder.metadata["validation_summary"] = {
                "attempt_count": attempt_count,
                "valid_count": len(valid_receipts_ns),
                "error_count": attempt_count - len(valid_receipts_ns),
                "actual_elapsed_s": (
                    (valid_receipts_ns[-1] - valid_receipts_ns[0]) / 1e9
                    if len(valid_receipts_ns) >= 2 else 0.0
                ),
                "actual_rate_hz_from_valid_receipt_timestamps": actual_hz,
                "median_valid_receipt_interval_s": median_interval,
                "mean_resistance_ohm": statistics.mean(valid_values) if valid_values else None,
                "restored_to_1_nplc": restore_details is not None,
                "error": error_text,
            }
            try:
                recorder.finish(completion)
            except OSError as exc:
                print(f"Failed to finalize experiment files: {exc}", file=sys.stderr)

    if recorder is not None:
        print(f"Experiment directory: {recorder.directory}")
        print(recorder.metadata.get("validation_summary", {}))
    return 0 if completion == "stopped" and restore_details is not None and not error_text else 1


if __name__ == "__main__":
    raise SystemExit(main())
