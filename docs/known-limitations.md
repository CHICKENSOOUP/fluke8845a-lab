# Known limitations

[简体中文](#简体中文) | [English](#english)

## 简体中文

- 本项目是独立的控制和记录软件，不是仪器校准工具，也不能替代仪器手册。
- Mock 模式使用模拟读数演示软件流程，不能证明仪器准确度或硬件兼容性。
- 当前源码快照的真机验证尚未完成；范围见[硬件验证说明](hardware-validation.md)。
- 实际采集速度受仪器设置、测量模式、量程、串口参数和测量环境影响。
- 测量准确度取决于仪器、校准状态、接线、环境及所选设置；本项目不保证准确度。
- 不要将电流输入端并联到电压源。每次测量前请遵守仪器安全说明并核对接线和端子。
- 当前 GitHub 源码版本不含预编译 Windows 程序。发布便携程序前尚有待完成事项，见 [Qt 许可说明](qt-licensing-and-third-party-notices.md)。

## English

- This project is an independent control and logging application. It is not an instrument calibration tool or a substitute for the instrument manuals.
- Mock mode exercises software flows with simulated readings; it does not establish instrument accuracy or hardware compatibility.
- Hardware verification is incomplete for the current source snapshot. See the [hardware verification notes](hardware-validation.md) for this release's recorded scope.
- Actual acquisition speed depends on instrument settings, measurement mode, range, serial configuration, and the measurement setup.
- Measurement accuracy depends on the instrument, calibration status, wiring, environment, and selected settings. No accuracy guarantee is made.
- Do not connect the current input in parallel with a voltage source. Follow the instrument's safety instructions and check wiring and terminals before every measurement.
- The current GitHub source release does not include a prebuilt Windows executable. See the [Qt licensing notes](qt-licensing-and-third-party-notices.md) for the remaining requirements before publishing a bundled binary.
