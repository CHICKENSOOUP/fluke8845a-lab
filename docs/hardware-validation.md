# Hardware verification status

[简体中文](#简体中文) | [English](#english)

## 简体中文

本说明概述已记录的验证范围，不发布工作站标识、串口日志、原始测量数据或仪器序列号。

### 当前源码版本

- 当前代码提供六种模式的 Mock 流程，可在无仪器情况下体验 GUI 采集与保存。
- 当前已推送提交为 `853e8c6f072eb60beeb3c25a2e9f1572b4647f7d`。GitHub Actions 在 Windows / Python 3.11 下 209 项 pytest 用时 10.29 秒，在 Python 3.12 下 209 项用时 13.81 秒；两个 pytest 步骤各自有 60 秒上限。该提交的源码 ZIP、SHA-256 文件、GitHub Release 和 GHCR OCI 源码包发布均成功。
- 此提交的本地 Windows 冻结程序构建及便携 ZIP 校验通过。构建修复显式打包 pyqtgraph 需要的 `PySide6.QtOpenGL` 和 `PySide6.QtOpenGLWidgets`。冻结 GUI Mock 检查、串口枚举检查、解压后启动检查、localhost 健康检查均通过；六种 Mock 模式的端到端报告为 `status=ok`，每种模式保存 9 条模拟读数及 `measurements.csv`、`config.json`、`events.csv` 和 `run.log`。这些结果验证软件包和模拟流程，不证明真实仪器通信或测量准确度。
- 提交 `853e8c6` 尚未连接真实仪器逐模式验证。早期版本的真机检查不能作为当前源码快照的验证证据；Windows EXE 也尚未公开发布，Qt/PySide 二进制分发审查仍未关闭。

### 限制

当前版本不宣称经过校准的测量准确度、六种模式均已完成真机验证、当前构建具有长期稳定性，或已通过安全认证。Mock 数据是模拟数据。连接仪表前，请核对测量模式、端子、接线、串口设置和适用的安全注意事项。

详细操作流程见[使用指南](user-guide.md)。本软件不能替代仪器手册或安全测量规范。

## English

This file summarizes the scope of verification without publishing workstation identifiers, serial-port logs, raw measurements, or instrument serial numbers.

## Current source release

- The current code has a six-mode Mock workflow for exercising GUI acquisition and file saving without an instrument.
- The current pushed commit is `853e8c6f072eb60beeb3c25a2e9f1572b4647f7d`. GitHub Actions passed 209 pytest tests on Windows / Python 3.11 in 10.29 seconds and Python 3.12 in 13.81 seconds; each pytest step has a 60-second limit. The source ZIP, SHA-256 file, GitHub Release, and GHCR OCI source package were published successfully.
- The local frozen Windows build and portable ZIP verification passed. The build explicitly includes `PySide6.QtOpenGL` and `PySide6.QtOpenGLWidgets`, which pyqtgraph loads dynamically. Frozen GUI Mock startup, serial enumeration, extracted-package startup, and localhost health checks passed. The six-mode frozen Mock end-to-end report is `status=ok`; each mode saved 9 simulated readings plus `measurements.csv`, `config.json`, `events.csv`, and `run.log`. These results validate the software package and simulated workflows, not real-meter communication or measurement accuracy.
- Commit `853e8c6` has not been checked mode-by-mode against a real instrument. Earlier real-instrument checks apply to older builds and are not evidence for this source snapshot. The Windows EXE has not been publicly released because the Qt/PySide binary-distribution review remains open.

## Limits

The current release does not claim calibrated accuracy, validation of all six modes on hardware, long-duration stability of this exact build, or safety certification. Mock data is simulated. Before connecting a meter, verify its mode, terminals, wiring, serial settings, and applicable safety precautions.

For the detailed operating workflow, see the [user guide](user-guide.md). Do not use the app as a substitute for the instrument manual or safe measurement practice.
