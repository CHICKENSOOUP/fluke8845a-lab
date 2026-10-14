# Hardware verification status

[简体中文](#简体中文) | [English](#english)

## 简体中文

本说明概述已记录的验证范围，不发布工作站标识、串口日志、原始测量数据或仪器序列号。

### 最近已验证的应用代码版本

- 当前代码提供六种模式的 Mock 流程，可在无仪器情况下体验 GUI 采集与保存。
- 最近已完整验收的应用代码提交为 `885d80bace9690657252a00cd5bf1a2007966781`，对应 [GitHub Release](https://github.com/CHICKENSOOUP/fluke8845a-lab/releases/tag/main-build-885d80bace96) 和 [Actions 运行 38632084566](https://github.com/CHICKENSOOUP/fluke8845a-lab/actions/runs/38632084566)。Windows / Python 3.11、3.12 两个 CI 测试步骤均通过 220 项测试；本地全套 pytest 也通过 220 项，用时 9.24 秒。
- 已从 GitHub Release 下载源码 ZIP，并核对附件 `SHA256SUMS.txt` 与 ZIP 内容一致：`3DC26A6EB98467CE084B40150F0289ADDA55C9C0BFB90B49844BC66AFAE85910`。从该下载 ZIP 解压后直接启动六模式 GUI Mock 验收，六种模式均成功保存有效读数、配置、事件与运行日志。对应 GHCR 标签为 `ghcr.io/chickensooup/fluke8845a-lab-source:main-build-885d80bace96`；匿名读取该标签的 OCI manifest 返回 HTTP 200。
- 后续文档更新提交 `b993b32a05dd8ffdcefa78f79db5812ca0d7067a` 的[源码 Release](https://github.com/CHICKENSOOUP/fluke8845a-lab/releases/tag/main-build-b993b32a05dd)也通过 Windows / Python 3.11、3.12 的 220 项测试。该 Release 源码 ZIP SHA-256 为 `37D8A77B64010B21F54EB9311A7398B9979E98B1009A02021B9FCB2DFB700C73`，下载后通过校验；在新解压目录首次运行 `run_windows.bat`，自动创建 `.venv` 并安装依赖，再执行六模式 GUI Mock 验收，全部通过，进程总时限为 50 秒。随后运行同一目录下的 `run_web.bat`，健康检查、主页及CSS/JS资源均返回成功；服务只绑定 `127.0.0.1`，并通过同源退出接口正常关闭。依赖安装使用了主机已有的 pip 缓存，因此不能替代无缓存网络安装验证。
- 随后的 `main-build-b36ff0b10790` 源码 ZIP 已在全新解压目录完成无 pip 缓存首次启动验收：设置 `PIP_NO_CACHE_DIR=1` 后，启动器从网络下载并安装依赖，六种模式的 GUI Mock 流程各保存 9 个有效样本以及配置、事件和运行日志。完整进程用时 42 秒，低于 50 秒验收上限；报告明确标记为模拟数据，不代表真机验证。
- 同一源码提交还在本机生成了完整 Windows x64 ZIP（含桌面 EXE 和 localhost EXE）：`dist/Fluke8845A_Main_StopFix_20261015_Windows_x64.zip`，SHA-256 为 `9560E105955C465FC90B6D2C45A22575A3C6274858294E26BCA2D62F81F8AF6C`。冻结桌面程序的六模式 Mock 验收、串口枚举、解压包启动，以及 localhost 健康检查均通过；Windows ZIP 内含 `output/mock_validation/latest_frozen_exe_six_mode_e2e.json` 验收报告。此 Windows ZIP 目前是本机候选包，未上传到公开 Release。
- 本次没有操作真机。当前源码尚未完成六种模式的逐项真机验收；Mock 数据和软件包检查不能证明真实仪器通信或测量准确度。GitHub Release 目前只发布源码 ZIP；Windows 二进制的 Qt/PySide 与 Microsoft Visual C++ 分发审查仍未关闭。

### 限制

当前版本不宣称经过校准的测量准确度、六种模式均已完成真机验证、当前构建具有长期稳定性，或已通过安全认证。Mock 数据是模拟数据。连接仪表前，请核对测量模式、端子、接线、串口设置和适用的安全注意事项。

详细操作流程见[使用指南](user-guide.md)。本软件不能替代仪器手册或安全测量规范。

## English

This file summarizes the scope of verification without publishing workstation identifiers, serial-port logs, raw measurements, or instrument serial numbers.

## Latest verified application code

- The current code has a six-mode Mock workflow for exercising GUI acquisition and file saving without an instrument.
- The latest fully verified application-code commit is `885d80bace9690657252a00cd5bf1a2007966781`, with its [GitHub Release](https://github.com/CHICKENSOOUP/fluke8845a-lab/releases/tag/main-build-885d80bace96) and [Actions run 38632084566](https://github.com/CHICKENSOOUP/fluke8845a-lab/actions/runs/38632084566). Both Windows CI test steps, on Python 3.11 and 3.12, passed 220 tests. The complete local pytest suite also passed 220 tests in 9.24 seconds.
- The source ZIP was downloaded from the GitHub Release and its contents matched the accompanying `SHA256SUMS.txt`: `3DC26A6EB98467CE084B40150F0289ADDA55C9C0BFB90B49844BC66AFAE85910`. After extraction, the six-mode GUI Mock acceptance launched from the downloaded source and saved valid readings, configuration, events, and run logs in all modes. The corresponding GHCR tag is `ghcr.io/chickensooup/fluke8845a-lab-source:main-build-885d80bace96`; an anonymous read of its OCI manifest returned HTTP 200.
- The later documentation commit `b993b32a05dd8ffdcefa78f79db5812ca0d7067a` also passed the 220-test Windows CI suites on Python 3.11 and 3.12. Its [source Release](https://github.com/CHICKENSOOUP/fluke8845a-lab/releases/tag/main-build-b993b32a05dd) ZIP has SHA-256 `37D8A77B64010B21F54EB9311A7398B9979E98B1009A02021B9FCB2DFB700C73`, matching the downloaded checksum file. From a fresh extraction, the first `run_windows.bat` launch created `.venv`, installed dependencies, then completed the six-mode GUI Mock acceptance; the complete process was capped at 50 seconds. The same extracted folder's `run_web.bat` then passed health, page, and CSS/JS checks, bound only to `127.0.0.1`, and shut down through its same-origin endpoint. Dependency installation reused the host's existing pip cache, so this does not verify an uncached network install.
- The subsequent `main-build-b36ff0b10790` source ZIP passed a first-launch acceptance from a fresh extraction with pip caching disabled (`PIP_NO_CACHE_DIR=1`). The launcher downloaded and installed dependencies from the network, then completed GUI Mock acquisition in all six modes with nine valid samples per mode and saved configuration, event, and run-log files. The complete process took 42 seconds, below the 50-second acceptance cap. The report identifies the data as simulated and does not claim hardware validation.
- A complete Windows x64 ZIP containing the desktop and localhost executables was also built locally from the same source commit: `dist/Fluke8845A_Main_StopFix_20261015_Windows_x64.zip`, SHA-256 `9560E105955C465FC90B6D2C45A22575A3C6274858294E26BCA2D62F81F8AF6C`. The frozen desktop passed six-mode Mock acceptance, serial enumeration, and extracted-package startup; the localhost health check passed. The Windows ZIP contains the six-mode acceptance report at `output/mock_validation/latest_frozen_exe_six_mode_e2e.json`. This Windows ZIP is a local candidate and is not attached to the public Release.
- No real instrument was operated during this verification. The current source has not completed real-hardware validation mode by mode. Mock data and package checks do not establish real-meter communication or measurement accuracy. GitHub Releases currently contain only the source ZIP because the Qt/PySide and Microsoft Visual C++ distribution review for a Windows binary remains open.

## Limits

The current release does not claim calibrated accuracy, validation of all six modes on hardware, long-duration stability of this exact build, or safety certification. Mock data is simulated. Before connecting a meter, verify its mode, terminals, wiring, serial settings, and applicable safety precautions.

For the detailed operating workflow, see the [user guide](user-guide.md). Do not use the app as a substitute for the instrument manual or safe measurement practice.
