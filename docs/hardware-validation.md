# Hardware verification status

[简体中文](#简体中文) | [English](#english)

## 简体中文

本说明概述已记录的验证范围，不发布工作站标识、串口日志、原始测量数据或仪器序列号。

### 当前源码版本

- 当前代码提供六种模式的 Mock 流程，可在无仪器情况下体验 GUI 采集与保存。
- 最近已验证并公开发布的源码基线为 `b173c29265287d95a9b990103b0aecd6b1d8e279`，对应 [GitHub Release](https://github.com/CHICKENSOOUP/fluke8845a-lab/releases/tag/main-build-b173c2926528) 和 [Actions 运行 38624606820](https://github.com/CHICKENSOOUP/fluke8845a-lab/actions/runs/38624606820)。该提交在 Windows / Python 3.11、3.12 下各通过 217 项 pytest；源码 ZIP SHA-256 为 `D9426691C1D017E41A04C0211755678F4B0EB2276D9752E46B5389D44503D130`，下载包校验一致，解压后的 Mock 启动检查通过。
- 基于该基线的当前工作树本地通过 219 项 pytest，用时 9.26 秒；本次本地运行设了 55 秒超时，发布工作流中的每个 pytest 步骤有 60 秒上限。改动包括小窗口 GUI 布局、删除重复的绘图行缓存，以及让样本和事件的本地/UTC 时间戳来自同一时刻。每次推送到 `main` 都先运行测试；通过后，同一工作流会为该提交发布专属 GitHub 预发布版和对应 GHCR 源码包。推送 `v*` 标签会发布正式版和匹配的 GHCR 包。
- 当前源码快照没有完成六种模式的真实仪器逐项验证。Mock 数据和软件包检查不能证明真实仪器通信或测量准确度。GitHub Release 目前只发布源码 ZIP；Windows 二进制的 Qt/PySide 与 Microsoft Visual C++ 分发审查仍未关闭。

### 限制

当前版本不宣称经过校准的测量准确度、六种模式均已完成真机验证、当前构建具有长期稳定性，或已通过安全认证。Mock 数据是模拟数据。连接仪表前，请核对测量模式、端子、接线、串口设置和适用的安全注意事项。

详细操作流程见[使用指南](user-guide.md)。本软件不能替代仪器手册或安全测量规范。

## English

This file summarizes the scope of verification without publishing workstation identifiers, serial-port logs, raw measurements, or instrument serial numbers.

## Current source release

- The current code has a six-mode Mock workflow for exercising GUI acquisition and file saving without an instrument.
- The most recently verified and published source baseline is `b173c29265287d95a9b990103b0aecd6b1d8e279`, with its [GitHub Release](https://github.com/CHICKENSOOUP/fluke8845a-lab/releases/tag/main-build-b173c2926528) and [Actions run 38624606820](https://github.com/CHICKENSOOUP/fluke8845a-lab/actions/runs/38624606820). Windows / Python 3.11 and 3.12 each passed 217 pytest tests. The source ZIP SHA-256 is `D9426691C1D017E41A04C0211755678F4B0EB2276D9752E46B5389D44503D130`; the downloaded asset matched, and its extracted Mock startup check passed.
- The current worktree, based on that commit, passes 219 local pytest tests in 9.26 seconds; this local run had a 55-second timeout, and each release-workflow pytest step has a 60-second limit. Changes cover compact-window GUI layout, removal of a duplicate full-row plotting cache, and paired sample/event local and UTC timestamps. Every push to `main` runs tests first; on success, that workflow publishes a commit-specific GitHub prerelease and matching GHCR source package. Pushing a `v*` tag publishes a stable release and matching GHCR package.
- The current source snapshot has not been checked mode-by-mode against a real instrument. Mock data and package checks do not establish real-meter communication or measurement accuracy. GitHub Releases currently contain the source ZIP only because the Qt/PySide and Microsoft Visual C++ distribution review for a Windows binary remains open.

## Limits

The current release does not claim calibrated accuracy, validation of all six modes on hardware, long-duration stability of this exact build, or safety certification. Mock data is simulated. Before connecting a meter, verify its mode, terminals, wiring, serial settings, and applicable safety precautions.

For the detailed operating workflow, see the [user guide](user-guide.md). Do not use the app as a substitute for the instrument manual or safe measurement practice.
