"""Create six explicitly simulated, small example experiments."""

from __future__ import annotations

import argparse
import sys
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fluke8845a.modes import Mode
from fluke8845a.mock import Mock8845A, MockScenario
from fluke8845a.scpi import ModeSettings
from fluke8845a.storage import ExperimentRecorder


def generate(output: Path) -> None:
    if output.exists():
        raise FileExistsError(f"示例目录已存在，拒绝覆盖：{output}")
    output.mkdir(parents=True)

    for mode in Mode:
        settings = ModeSettings(mode)
        scenario = MockScenario(
            name=f"{mode.value} 阶跃与恢复示例（模拟）",
            noise_fraction=1e-4,
            drift_fraction_per_minute=1e-3,
            step_at_s=0.06,
            step_fraction=0.03,
            recovery_at_s=0.16,
            response_tau_s=0.04,
            read_latency_s=0.01,
            seed=8845 + len(mode.value),
        )
        device = Mock8845A(scenario)
        info = device.connect()
        actual = device.configure(settings)
        directory = output / mode.value
        directory.mkdir()
        recorder = ExperimentRecorder(
            directory,
            {
                "simulation": True,
                "device": {
                    "manufacturer": info.manufacturer,
                    "model": info.model,
                    "serial_number": info.serial_number,
                    "firmware": info.firmware,
                    "idn_raw": info.idn_raw,
                },
                "connection": {"port": "MOCK", "serial_settings": None},
                "target_interval_s": 0.01,
                "mock_scenario": asdict(scenario),
                "actual_device_configuration": actual,
                "example_data_notice": "仅用于演示软件界面和分析，不是8845A实测数据。",
            },
            settings,
        )
        recorder.append_event("模拟实验开始")
        last_value = None
        for index in range(30):
            reading = device.read()
            recorder.append_reading(reading, mode=mode)
            last_value = reading.value_si
            if index == 8:
                recorder.append_event("模拟阶跃负载接入", value_si=last_value)
            elif index == 19:
                recorder.append_event("模拟负载恢复", value_si=last_value)
        recorder.append_event("模拟实验结束", value_si=last_value)
        recorder.finish("demo_complete")
        device.close()

    (output / "README.md").write_text(
        "# 示例实验数据\n\n"
        "本目录下六种模式的 CSV、JSON、事件和日志全部由内置 MOCK 后端生成。"
        "它们只用于演示导入、曲线、统计和文件格式，不代表真实 Fluke 8845A 测量结果。\n",
        encoding="utf-8",
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("examples/mock_experiments"))
    args = parser.parse_args()
    generate(args.output.resolve())
    print(f"已生成六种明确标记的模拟示例：{args.output.resolve()}")


if __name__ == "__main__":
    main()
