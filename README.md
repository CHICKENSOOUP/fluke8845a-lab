# Fluke 8845A Lab

[简体中文](#简体中文) | [English](#english)

## 简体中文

面向 Windows 的桌面应用，通过 RS-232 控制 Fluke 8845A，记录测量数据并显示实时曲线。

> 本项目由社区独立开发，与 Fluke 无隶属关系，也未获 Fluke 认可或背书。

### 功能

- 直流电压、交流电压、直流电流、交流电流、二线电阻和四线电阻
- 串口配置及项目自有驱动实现的 SCPI 通信
- 连续 CSV 记录、时间戳、事件标记、统计和实时曲线
- Mock 模式：无需连接仪器即可体验六种测量流程
- 可选 localhost 浏览器界面，复用桌面版的仪器驱动和采集线程

界面由 Qt/PySide6 提供；串口通信由 pyserial 和项目自有 SCPI 驱动负责。本项目不使用 Qt SerialPort。

### 源码安装

从 GitHub 下载源码 ZIP 后先解压。最简单的启动方法是双击 `run_windows.bat`。需要安装 64 位 Python 3.11 或更高版本；首次启动需要联网，启动器会自动创建项目专用的 `.venv` 并安装运行依赖，之后启动会复用环境。启动本机网页界面请双击 `run_web.bat`。源码 ZIP 不含预编译 EXE。

也可以手动在 PowerShell 中安装：

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m pip install -e .
python -m fluke8845a
```

启动本机网页界面：运行 `python -m fluke8845a.webapp` 或双击 `run_web.bat`。服务只绑定 `127.0.0.1:8765`。连接仪器前先断开桌面版占用的串口；同一 COM 端口不能同时由两个程序使用。

开发测试依赖可用 `python -m pip install -e ".[dev]"` 安装；运行测试：`python -m pytest`。本地独立目录构建使用 `build_windows.ps1`，还需安装 `.[build]`。当前 GitHub 版本仅发布源码，不含预编译 Windows 程序。发布便携程序前，仍需完成精确的第三方组件清单、Qt/PySide 对应源码交付和 Qt 库替换说明。

推送到 `main` 后会自动运行不超过 60 秒的测试，并为该提交发布带源码 ZIP 和 SHA-256 校验文件的预发布版本；推送 `v*` 标签会发布正式版本。自动发布流程见[发布说明](docs/releasing.md)。

### 仪器与验证状态

连接前，请将仪器设为 RS-232 / COMPUTER 模式，并在仪器上核对串口参数。当前验证范围见[硬件验证说明](docs/hardware-validation.md)。Mock 数据为模拟数据，不是仪器测量结果。接线与操作要求见[使用指南](docs/user-guide.md)。

### 许可证

项目自有代码和图稿按 [MIT 许可证](LICENSE)发布。PySide6/Qt 和其他运行依赖分别适用各自许可证，详见[第三方声明](THIRD-PARTY-NOTICES.md)及[Qt 许可说明](docs/qt-licensing-and-third-party-notices.md)。本源码发布不包含 Qt 运行库；MIT 许可证不涵盖第三方组件。

### 文档

- [SCPI 命令与技术依据](docs/scpi-reference.md)
- [使用指南](docs/user-guide.md)
- [已知限制](docs/known-limitations.md)
- [v1.0.1 发布说明](docs/release-notes-v1.0.1.md)
- [v1.0.0 发布说明](docs/release-notes-v1.0.0.md)
- [Qt 与第三方许可说明](docs/qt-licensing-and-third-party-notices.md)
- [参考项目调研](docs/open-source-project-research.md)

## English

Windows desktop application for controlling a Fluke 8845A over RS-232, recording measurements, and viewing live plots.

> This is an independent community project and is not affiliated with or endorsed by Fluke.

## Features

- DC voltage, AC voltage, DC current, AC current, 2-wire resistance, and 4-wire resistance
- Serial-port configuration and SCPI communication through the project's own driver
- Continuous CSV logging, timestamps, event markers, statistics, and live plots
- Mock mode for exercising all six measurement workflows without connecting an instrument
- Optional localhost browser interface sharing the desktop app's instrument driver and acquisition worker

Qt/PySide6 supplies the graphical interface; pyserial and the project's SCPI driver handle serial communication. The application does not use Qt SerialPort.

## Source setup

Extract the GitHub source ZIP, then double-click `run_windows.bat`. It requires 64-bit Python 3.11 or later and an internet connection on first launch. The launcher creates a project-local `.venv`, installs runtime dependencies, and reuses that environment on later starts. Double-click `run_web.bat` to start the localhost browser interface. The source ZIP does not contain a prebuilt EXE.

For a manual PowerShell setup:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m pip install -e .
python -m fluke8845a
```

Start the local browser interface with `python -m fluke8845a.webapp` or `run_web.bat`. It binds only to `127.0.0.1:8765`. Disconnect the desktop app from the instrument before connecting the browser app; one COM port cannot be opened by both programs at once.

Install developer dependencies with `python -m pip install -e ".[dev]"`. To run the test suite:

```powershell
python -m pytest
```

Build a local standalone package after installing `python -m pip install -e ".[build]"`. By default `build_windows.ps1` uses `.venv\Scripts\python.exe` and caches its compiler under `build/nuitka-cache`; `-PythonPath` selects another installed Python environment, `-PortablePythonPath` selects an existing matching portable runtime, and `$env:NUITKA_CACHE_DIR` in PowerShell reuses an existing Nuitka compiler cache. The current GitHub source release does not include a prebuilt Windows executable. A binary release can be considered after its exact third-party license inventory, Qt/PySide source delivery, and Qt replacement instructions are complete and verified.

Pushes to `main` automatically run tests capped at 60 seconds and publish a commit-specific prerelease with the source ZIP and SHA-256 checksum. Pushing a `v*` tag publishes a stable release. See the [release guide](docs/releasing.md).

## Hardware and verification status

Use the instrument's RS-232 / COMPUTER mode and verify the serial settings on the instrument before connecting. Current test and hardware verification limits are summarized in [docs/hardware-validation.md](docs/hardware-validation.md). Mock results are simulated and are not instrument measurements. Follow the safety and wiring instructions in [docs/user-guide.md](docs/user-guide.md).

## Licensing

Project-authored code and artwork are licensed under MIT; see [LICENSE](LICENSE). PySide6/Qt and the other runtime dependencies retain their own licenses. See [THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md) and [docs/qt-licensing-and-third-party-notices.md](docs/qt-licensing-and-third-party-notices.md).

This source release does not bundle Qt libraries. Do not treat the project MIT license as a license for third-party components.

## References

- [SCPI commands and technical basis](docs/scpi-reference.md)
- [Usage guide](docs/user-guide.md)
- [Known limitations](docs/known-limitations.md)
- [v1.0.1 release notes](docs/release-notes-v1.0.1.md)
- [v1.0.0 release notes](docs/release-notes-v1.0.0.md)
- [Qt and third-party licensing notes](docs/qt-licensing-and-third-party-notices.md)
