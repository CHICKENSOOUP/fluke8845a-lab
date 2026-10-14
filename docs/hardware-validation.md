# Hardware verification status

[简体中文](#简体中文) | [English](#english)

## 简体中文

本说明概述已记录的验证范围，不发布工作站标识、串口日志、原始测量数据或仪器序列号。

### 最近已验证的应用代码版本

- 当前代码提供六种模式的 Mock 流程，可在无仪器情况下体验 GUI 采集与保存。
- 当前源码提交为 `721b7c49a748ffce48d8984636181f6b47111605`。该版本为 SCPI 界面日志增加了有界缓冲（最多 5000 行）和合并通知；界面落后时会提示较早显示日志被丢弃，测量 CSV 和全程统计仍完整保存。
- [Actions 运行 38640315091](https://github.com/CHICKENSOOUP/fluke8845a-lab/actions/runs/38640315091) 在 Windows 上通过 224 项 pytest：Python 3.11 用时 14.30 秒，Python 3.12 用时 14.79 秒；两个测试步骤均设有 60 秒超时。该运行随后成功生成源码 ZIP、SHA-256 清单并发布 Release 与 GHCR 包。本机 Python 3.11 全量 pytest 也通过 224 项，用时约 12 秒；本机源码打包校验通过，共 111 个文件。
- 源码 [Release `main-build-721b7c49a748`](https://github.com/CHICKENSOOUP/fluke8845a-lab/releases/tag/main-build-721b7c49a748) 提供 `Fluke8845A_Source_Project.zip` 和 `SHA256SUMS.txt`。同标签的公开 GHCR OCI 包为 `ghcr.io/chickensooup/fluke8845a-lab-source:main-build-721b7c49a748`；匿名拉取令牌读取其 manifest 返回 HTTP 200，digest 为 `sha256:b74996eddb017cea1020ab259f8f603293d4c05deca96225038c792283297498`。
- 对最新源码 [Release `main-build-70b60a096d99`](https://github.com/CHICKENSOOUP/fluke8845a-lab/releases/tag/main-build-70b60a096d99) 的 ZIP 做了独立冷启动验证：下载后 SHA-256 与发布校验文件一致（`915282C3D28A44FD331263EB5EE84CD66A1E7C3C235D8D6DEB77E482EFD1EBCC`），解压到新目录并用 `run_windows.bat --mock-e2e` 完成桌面 Mock 流程。报告状态为 `ok`，DCV/ACV/DCI/ACI/2W/4W 分别保存 9/9/8/9/9/9 个模拟样本；每种模式均验证 `measurements.csv`、`config.json`、`events.csv` 和 `run.log`。首次启动联网安装依赖成功。此项仅证明发布源码包可冷启动运行并完成模拟采集，不是 8845A 真机验证。
- 本次没有操作真机。当前源码尚未完成六种模式的逐项真机验收；Mock 数据和软件包检查不能证明真实仪器通信或测量准确度。GitHub Release 目前只发布源码 ZIP；Windows 二进制的 Qt/PySide 与 Microsoft Visual C++ 分发审查仍未关闭。

### 限制

当前版本不宣称经过校准的测量准确度、六种模式均已完成真机验证、当前构建具有长期稳定性，或已通过安全认证。Mock 数据是模拟数据。连接仪表前，请核对测量模式、端子、接线、串口设置和适用的安全注意事项。

详细操作流程见[使用指南](user-guide.md)。本软件不能替代仪器手册或安全测量规范。

## English

This file summarizes the scope of verification without publishing workstation identifiers, serial-port logs, raw measurements, or instrument serial numbers.

## Latest verified application code

- The current code has a six-mode Mock workflow for exercising GUI acquisition and file saving without an instrument.
- The current source commit is `721b7c49a748ffce48d8984636181f6b47111605`. It adds a bounded SCPI GUI log mailbox (maximum 5,000 lines) with coalesced notifications. When the GUI falls behind, it reports dropped older display logs; measurement CSV and full-run statistics remain complete.
- [Actions run 38640315091](https://github.com/CHICKENSOOUP/fluke8845a-lab/actions/runs/38640315091) passed 224 pytest cases on Windows: Python 3.11 in 14.30 seconds and Python 3.12 in 14.79 seconds; each pytest step has a 60-second timeout. The run then built the source ZIP and checksum and published both the Release and GHCR package. The complete local Python 3.11 suite also passed 224 cases in about 12 seconds, and local source-package verification passed with 111 files.
- The source [Release `main-build-721b7c49a748`](https://github.com/CHICKENSOOUP/fluke8845a-lab/releases/tag/main-build-721b7c49a748) contains `Fluke8845A_Source_Project.zip` and `SHA256SUMS.txt`. Its public GHCR OCI package is `ghcr.io/chickensooup/fluke8845a-lab-source:main-build-721b7c49a748`; an anonymous pull-token request fetched the manifest successfully (HTTP 200), with digest `sha256:b74996eddb017cea1020ab259f8f603293d4c05deca96225038c792283297498`.
- The latest source [Release `main-build-70b60a096d99`](https://github.com/CHICKENSOOUP/fluke8845a-lab/releases/tag/main-build-70b60a096d99) passed a fresh-extraction cold-start check. Its ZIP SHA-256 matched the published checksum (`915282C3D28A44FD331263EB5EE84CD66A1E7C3C235D8D6DEB77E482EFD1EBCC`); `run_windows.bat --mock-e2e` completed with status `ok`. DCV/ACV/DCI/ACI/2W/4W saved 9/9/8/9/9/9 simulated readings, and each mode's `measurements.csv`, `config.json`, `events.csv`, and `run.log` was verified. First-launch dependency installation succeeded with network access. This establishes source-package startup and simulated acquisition only, not real-meter validation.
- No real instrument was operated in this verification. The current source has not completed real-hardware validation mode by mode. Mock data and package checks do not establish real-meter communication or measurement accuracy. GitHub Releases currently contain only the source ZIP because the Qt/PySide and Microsoft Visual C++ distribution review for a Windows binary remains open.

## Limits

The current release does not claim calibrated accuracy, validation of all six modes on hardware, long-duration stability of this exact build, or safety certification. Mock data is simulated. Before connecting a meter, verify its mode, terminals, wiring, serial settings, and applicable safety precautions.

For the detailed operating workflow, see the [user guide](user-guide.md). Do not use the app as a substitute for the instrument manual or safe measurement practice.
