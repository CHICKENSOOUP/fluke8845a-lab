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

从 GitHub 下载源码 ZIP 后先解压。最简单的启动方法是双击 `run_windows.bat`。需要安装 64 位 Python 3.11 或 3.12；首次启动需要联网，启动器会按 Python 版本使用随包提供的锁定依赖文件，自动创建项目专用的 `.venv`，之后启动会复用环境。启动本机网页界面请双击 `run_web.bat`。源码 ZIP 不含预编译 EXE。

也可以手动在 PowerShell 中安装：

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements-lock.txt
python -m pip install --no-deps --no-build-isolation -e .
python -m fluke8845a
```

启动本机网页界面：运行 `python -m fluke8845a.webapp` 或双击 `run_web.bat`。服务只绑定 `127.0.0.1:8765`。连接仪器前先断开桌面版占用的串口；同一 COM 端口不能同时由两个程序使用。

开发测试依赖可用 `python -m pip install -e ".[dev]"` 安装；在 PowerShell 中运行 `powershell -ExecutionPolicy Bypass -File .\scripts\run_tests_bounded.ps1`。该入口会并行运行完整 pytest 分片套件，共用 45 秒分片限时和 50 秒外层看门狗，并把每片日志保留在 `build\pytest-shards`。若依赖装在其他 Python 环境，可加 `-PythonPath .\.venv-build\Scripts\python.exe`。Windows 便携构建需要 64 位 Python 3.12 环境；每次构建也会调用相同的分片测试入口。

```powershell
py -3.12 -m venv .venv-build
.\.venv-build\Scripts\Activate.ps1
python -m pip install -r requirements-lock.txt
python -m pip install --no-deps --no-build-isolation -e .
$buildName = "Fluke8845A_LocalBuild_{0}" -f (Get-Date -Format "yyyyMMdd_HHmmss")
.\build_windows.ps1 -SideBySide -BuildName $buildName -PythonPath ".venv-build\Scripts\python.exe"
```

当前 GitHub 版本仅发布源码，不含预编译 Windows 程序。发布便携程序前，仍需完成精确的第三方组件清单、Qt/PySide 对应源码交付和 Qt 库替换说明。

每次推送到 `main` 都会在 Python 3.11 和 3.12 Windows 环境分别运行完整 pytest 分片套件（分片总限时 50 秒，工作流步骤上限 60 秒），并从新解压的源码 ZIP 首次创建环境、安装锁定依赖和启动 Mock 桌面界面（最多 60 秒）；全部通过后，同一工作流会为该提交创建 GitHub 预发布版本，并同时发布源码 ZIP、SHA-256 校验文件和 GHCR Packages 源码包。推送 `v*` 标签也会同时发布正式 GitHub Release 和 Packages。自动发布流程见[发布说明](docs/releasing.md)。

### 仪器与验证状态

连接前，请将仪器设为 RS-232 / COMPUTER 模式，并在仪器上核对串口参数。当前验证范围见[硬件验证说明](docs/hardware-validation.md)。Mock 数据为模拟数据，不是仪器测量结果。接线与操作要求见[使用指南](docs/user-guide.md)。

### 许可证

项目自有代码和图稿按 [MIT 许可证](LICENSE)发布。PySide6/Qt 和其他运行依赖分别适用各自许可证，详见[第三方声明](THIRD-PARTY-NOTICES.md)及[Qt 许可说明](docs/qt-licensing-and-third-party-notices.md)。本源码发布不包含 Qt 运行库；MIT 许可证不涵盖第三方组件。

### 文档

- [SCPI 命令与技术依据](docs/scpi-reference.md)
- [使用指南](docs/user-guide.md)
- [短时真机 GUI 探测](docs/hardware-probes.md)
- [已知限制](docs/known-limitations.md)
- [v1.0.5 发布说明](docs/release-notes-v1.0.5.md)
- [v1.0.4 发布说明](docs/release-notes-v1.0.4.md)
- [v1.0.3 发布说明](docs/release-notes-v1.0.3.md)
- [v1.0.2 发布说明](docs/release-notes-v1.0.2.md)
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

Extract the GitHub source ZIP, then double-click `run_windows.bat`. It requires 64-bit Python 3.11 or 3.12 and an internet connection on first launch. The launcher selects the matching included dependency lock, creates a project-local `.venv`, and reuses that environment on later starts. Double-click `run_web.bat` to start the localhost browser interface. The source ZIP does not contain a prebuilt EXE.

For a manual PowerShell setup:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements-lock.txt
python -m pip install --no-deps --no-build-isolation -e .
python -m fluke8845a
```

Start the local browser interface with `python -m fluke8845a.webapp` or `run_web.bat`. It binds only to `127.0.0.1:8765`. Disconnect the desktop app from the instrument before connecting the browser app; one COM port cannot be opened by both programs at once.

Install developer dependencies with `python -m pip install -e ".[dev]"`. In PowerShell, run the complete pytest suite in parallel shards; the shared shard limit is 45 seconds and the outer watchdog is 50 seconds. Per-shard logs are retained under `build\pytest-shards`. If the dependencies are installed in another Python environment, pass `-PythonPath .\.venv-build\Scripts\python.exe`:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\run_tests_bounded.ps1
```

For a reproducible local build, use 64-bit Python 3.12 and install the locked test/build dependencies before invoking the script:

```powershell
py -3.12 -m venv .venv-build
.\.venv-build\Scripts\Activate.ps1
python -m pip install -r requirements-lock.txt
python -m pip install --no-deps --no-build-isolation -e .
$buildName = "Fluke8845A_LocalBuild_{0}" -f (Get-Date -Format "yyyyMMdd_HHmmss")
.\build_windows.ps1 -SideBySide -BuildName $buildName -PythonPath ".venv-build\Scripts\python.exe"
```

By default `build_windows.ps1` uses `.venv\Scripts\python.exe` and caches its compiler under `build/nuitka-cache`; `-PythonPath` selects another installed Python environment, `-PortablePythonPath` selects an existing matching portable runtime, and `$env:NUITKA_CACHE_DIR` in PowerShell reuses an existing Nuitka compiler cache. The current GitHub source release does not include a prebuilt Windows executable. A binary release can be considered after its exact third-party license inventory, Qt/PySide source delivery, and Qt replacement instructions are complete and verified.

Every push to `main` runs the complete pytest shard suite on Windows with Python 3.11 and 3.12 (50-second shard limit, 60-second workflow-step limit), then extracts the source ZIP into a fresh directory, installs the locked environment, and launches the Mock desktop UI (60-second limit). After all checks pass, the workflow publishes a versioned, commit-specific GitHub prerelease and matching GHCR OCI source package containing the source ZIP and SHA-256 checksum. Titles and tags include the `pyproject.toml` version and commit; stable `v*` tags must match the project version. See the [release guide](docs/releasing.md) for download instructions and GHCR visibility details.

## Hardware and verification status

Use the instrument's RS-232 / COMPUTER mode and verify the serial settings on the instrument before connecting. Current test and hardware verification limits are summarized in [docs/hardware-validation.md](docs/hardware-validation.md). Mock results are simulated and are not instrument measurements. Follow the safety and wiring instructions in [docs/user-guide.md](docs/user-guide.md).

## Licensing

Project-authored code and artwork are licensed under MIT; see [LICENSE](LICENSE). PySide6/Qt and the other runtime dependencies retain their own licenses. See [THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md) and [docs/qt-licensing-and-third-party-notices.md](docs/qt-licensing-and-third-party-notices.md).

This source release does not bundle Qt libraries. Do not treat the project MIT license as a license for third-party components.

## References

- [SCPI commands and technical basis](docs/scpi-reference.md)
- [Usage guide](docs/user-guide.md)
- [Short hardware GUI probes](docs/hardware-probes.md)
- [Known limitations](docs/known-limitations.md)
- [v1.0.5 release notes](docs/release-notes-v1.0.5.md)
- [v1.0.4 release notes](docs/release-notes-v1.0.4.md)
- [v1.0.3 release notes](docs/release-notes-v1.0.3.md)
- [v1.0.2 release notes](docs/release-notes-v1.0.2.md)
- [v1.0.1 release notes](docs/release-notes-v1.0.1.md)
- [v1.0.0 release notes](docs/release-notes-v1.0.0.md)
- [Qt and third-party licensing notes](docs/qt-licensing-and-third-party-notices.md)
