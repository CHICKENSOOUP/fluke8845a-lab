# Known limitations

[简体中文](#简体中文) | [English](#english)

## 简体中文

- 本项目是独立的控制和记录软件，不是仪器校准工具，也不能替代仪器手册。
- Mock 模式使用模拟读数演示软件流程，不能证明仪器准确度或硬件兼容性。
- 当前源码快照的真机验证尚未完成；范围见[硬件验证说明](hardware-validation.md)。
- 实际采集速度受仪器设置、测量模式、量程、串口参数和测量环境影响。
- ACI 400 mA 档的官方软件版本门槛为 1.0.700.18，但扩展规格没有指出它对应 OutG SW 还是 InG SW。软件会在 ACI 界面提示操作者核对仪表面板版本，不会根据 OutG SW 或 InG SW 自动拒绝 400 mA 固定档或自动量程。DCI 400 mA 仍按用户手册明确规定的 OutG SW ≥ 2.0 执行限制。
- 测量准确度取决于仪器、校准状态、接线、环境及所选设置；本项目不保证准确度。
- 不要将电流输入端并联到电压源。每次测量前请遵守仪器安全说明并核对接线和端子。
- 当前 GitHub 源码版本不含预编译 Windows 程序。源码 ZIP 首次启动需要 64 位 Python 3.11 或 3.12 和网络连接，以创建本地虚拟环境并按随包锁文件安装依赖。发布便携程序前尚有待完成事项，见 [Qt 许可说明](qt-licensing-and-third-party-notices.md)。

## English

- This project is an independent control and logging application. It is not an instrument calibration tool or a substitute for the instrument manuals.
- Mock mode exercises software flows with simulated readings; it does not establish instrument accuracy or hardware compatibility.
- Hardware verification is incomplete for the current source snapshot. See the [hardware verification notes](hardware-validation.md) for this release's recorded scope.
- Actual acquisition speed depends on instrument settings, measurement mode, range, serial configuration, and the measurement setup.
- The ACI 400 mA software threshold is 1.0.700.18, but the official extended specifications do not identify it as OutG SW or InG SW. The application prompts the operator to verify the panel version and does not automatically reject the 400 mA fixed range or autorange based on either field. The DCI 400 mA gate remains tied to OutG SW ≥ 2.0 as explicitly stated in the Users Manual.
- Measurement accuracy depends on the instrument, calibration status, wiring, environment, and selected settings. No accuracy guarantee is made.
- Do not connect the current input in parallel with a voltage source. Follow the instrument's safety instructions and check wiring and terminals before every measurement.
- The current GitHub source release does not include a prebuilt Windows executable. First launch from the source ZIP requires 64-bit Python 3.11 or 3.12 and an internet connection to create a local environment and install the dependencies from the included lock file. See the [Qt licensing notes](qt-licensing-and-third-party-notices.md) for the remaining requirements before publishing a bundled binary.
