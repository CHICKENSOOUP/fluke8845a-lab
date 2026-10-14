# Hardware verification status

[简体中文](#简体中文) | [English](#english)

## 简体中文

本说明概述已记录的验证范围，不发布工作站标识、串口日志、原始测量数据或仪器序列号。

### 当前源码版本

- 当前代码提供六种模式的 Mock 流程，可在无仪器情况下体验 GUI 采集与保存。
- 上一份已验证并公开发布的源码提交为 `e64a63e81c0856f9d83e3fd3790e8130366b2958`。GitHub Actions [运行 38621816795](https://github.com/CHICKENSOOUP/fluke8845a-lab/actions/runs/38621816795) 在 Windows / Python 3.11 下 213 项 pytest 用时 11.21 秒，在 Python 3.12 下 213 项用时 11.01 秒；两个 pytest 步骤各自有 60 秒上限。该提交成功发布了 commit 专属 GitHub Release 和 GHCR OCI 源码包。
- 当前候选工作树基于该提交，Windows 冻结程序为 `Fluke8845A_Essentials_6_12_7`。本地 pytest 为 217 项通过，用时 9.10 秒；冻结 GUI Mock 检查、串口枚举检查、解压后启动检查、localhost 健康检查均通过。当前便携 ZIP 有 277 个文件，运行时许可清单的 25 个文件记录均与包内大小及 SHA-256 相符。六种 Mock 模式的端到端报告为 `status=ok`，每种模式保存 9 条模拟读数及 `measurements.csv`、`config.json`、`events.csv` 和 `run.log`。这些结果只验证软件包和模拟流程，不证明真实仪器通信或测量准确度。
- 当前 Windows 候选包没有附加到 GitHub Release。当前代码快照仍未连接真实仪器逐模式验证；早期版本的真机检查不能作为当前源码快照的验证证据。Qt/PySide 与 Microsoft Visual C++ 分发审查尚未关闭，因此工作流目前只发布源码 Release 和 GHCR 包。

### 限制

当前版本不宣称经过校准的测量准确度、六种模式均已完成真机验证、当前构建具有长期稳定性，或已通过安全认证。Mock 数据是模拟数据。连接仪表前，请核对测量模式、端子、接线、串口设置和适用的安全注意事项。

详细操作流程见[使用指南](user-guide.md)。本软件不能替代仪器手册或安全测量规范。

## English

This file summarizes the scope of verification without publishing workstation identifiers, serial-port logs, raw measurements, or instrument serial numbers.

## Current source release

- The current code has a six-mode Mock workflow for exercising GUI acquisition and file saving without an instrument.
- The previous verified and publicly published source commit is `e64a63e81c0856f9d83e3fd3790e8130366b2958`. GitHub Actions [run 38621816795](https://github.com/CHICKENSOOUP/fluke8845a-lab/actions/runs/38621816795) passed 213 pytest tests on Windows / Python 3.11 in 11.21 seconds and Python 3.12 in 11.01 seconds; each pytest step has a 60-second limit. That commit successfully published a commit-specific GitHub Release and GHCR OCI source package.
- The current candidate worktree is based on that commit and built the `Fluke8845A_Essentials_6_12_7` frozen Windows application. Local pytest passed 217 tests in 9.10 seconds. Frozen GUI Mock startup, serial enumeration, extracted-package startup, and localhost health checks passed. The portable ZIP has 277 files; all 25 runtime-license manifest records match the archived file sizes and SHA-256 values. The six-mode frozen Mock end-to-end report is `status=ok`; each mode saved 9 simulated readings plus `measurements.csv`, `config.json`, `events.csv`, and `run.log`. These results validate the software package and simulated workflows, not real-meter communication or measurement accuracy.
- The current Windows candidate is not attached to a GitHub Release. The current code snapshot has not been checked mode-by-mode against a real instrument. Earlier real-instrument checks apply to older builds and are not evidence for this source snapshot. Qt/PySide and Microsoft Visual C++ distribution reviews remain open, so the workflow publishes the source ZIP only.

## Limits

The current release does not claim calibrated accuracy, validation of all six modes on hardware, long-duration stability of this exact build, or safety certification. Mock data is simulated. Before connecting a meter, verify its mode, terminals, wiring, serial settings, and applicable safety precautions.

For the detailed operating workflow, see the [user guide](user-guide.md). Do not use the app as a substitute for the instrument manual or safe measurement practice.
