# Hardware verification status

[简体中文](#简体中文) | [English](#english)

## 简体中文

本说明概述已记录的验证范围，不发布工作站标识、串口日志、原始测量数据或仪器序列号。

### 当前源码版本

- 当前代码提供六种模式的 Mock 流程，可在无仪器情况下体验 GUI 采集与保存。
- 源码提交 `cb0893c` 的 209 项 pytest 已在 Windows / Python 3.11（10.57 秒）和 Python 3.12（10.93 秒）通过；对应 GitHub Actions 发布也成功，每个 pytest 步骤设有 60 秒上限。另有 Python 3.11 Mock GUI 启停检查通过。这些是软件自动化/Mock 检查，不是仪器准确度或真机兼容性验证。
- 提交 `cb0893c` 尚未连接真实仪器逐模式验证。早期版本的真机检查不能作为当前源码快照的验证证据。

### 限制

当前版本不宣称经过校准的测量准确度、六种模式均已完成真机验证、当前构建具有长期稳定性，或已通过安全认证。Mock 数据是模拟数据。连接仪表前，请核对测量模式、端子、接线、串口设置和适用的安全注意事项。

详细操作流程见[使用指南](user-guide.md)。本软件不能替代仪器手册或安全测量规范。

## English

This file summarizes the scope of verification without publishing workstation identifiers, serial-port logs, raw measurements, or instrument serial numbers.

## Current source release

- The current code has a six-mode Mock workflow for exercising GUI acquisition and file saving without an instrument.
- Source commit `cb0893c` passed 209 pytest tests on Windows / Python 3.11 (10.57 seconds) and Python 3.12 (10.93 seconds); the corresponding GitHub Actions release also succeeded, with a 60-second limit on each pytest step. A Python 3.11 Mock GUI startup/shutdown check also passed. These are software automation/Mock checks, not meter-accuracy or real-hardware validation.
- Commit `cb0893c` has not been checked mode-by-mode against a real instrument. Earlier real-instrument checks apply to older builds and are not evidence for this source snapshot.

## Limits

The current release does not claim calibrated accuracy, validation of all six modes on hardware, long-duration stability of this exact build, or safety certification. Mock data is simulated. Before connecting a meter, verify its mode, terminals, wiring, serial settings, and applicable safety precautions.

For the detailed operating workflow, see the [user guide](user-guide.md). Do not use the app as a substitute for the instrument manual or safe measurement practice.
