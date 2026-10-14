# Hardware verification status

[简体中文](#简体中文) | [English](#english)

## 简体中文

本说明概述已记录的验证范围，不发布工作站标识、串口日志、原始测量数据或仪器序列号。

### 最近已验证的应用代码版本

- 当前代码提供六种模式的 Mock 流程，可在无仪器情况下体验 GUI 采集与保存。
- 最近的应用代码提交为 `c09337e85714e3eb553d91a86140cd0c0ea5abc6`，修复了关闭窗口时等待后台采集线程完成读数保存和串口清理的问题；对应 [GitHub Release](https://github.com/CHICKENSOOUP/fluke8845a-lab/releases/tag/main-build-c09337e85714) 和 [Actions 运行 38636996019](https://github.com/CHICKENSOOUP/fluke8845a-lab/actions/runs/38636996019) 均成功。随后仅增加回归测试的提交 `f33d7f407da527ccf8ec6c4daeeb4c642473ba79` 的 [Actions 运行 38637555236](https://github.com/CHICKENSOOUP/fluke8845a-lab/actions/runs/38637555236) 在 Windows / Python 3.11、3.12 均通过 222 项测试；新增测试在 Mock 慢速读数进行时请求退出，并核对该读数先写入 CSV 再关闭窗口。
- 源码 [Release `main-build-f33d7f407da5`](https://github.com/CHICKENSOOUP/fluke8845a-lab/releases/tag/main-build-f33d7f407da5) 的 ZIP 与 `SHA256SUMS.txt` 匹配，SHA-256 为 `845D273A27ADFE294B0E6973DA04540CF71808BA9E80D161A650B4E2BC31F3A5`。从第二个全新解压目录设置 `PIP_NO_CACHE_DIR=1` 后首次运行 `run_windows.bat --mock-e2e`，网络依赖安装及六模式 Mock GUI 验收完整用时 45.21 秒；六种模式各保存 9 个有效模拟读数及配置、事件和运行日志。该结果验证源码包冷启动，不代表真机验证。
- 此前本机生成的 Windows x64 ZIP（含桌面 EXE 和 localhost EXE）为 `dist/Fluke8845A_Main_StopFix_20261015_Windows_x64.zip`，SHA-256 为 `9560E105955C465FC90B6D2C45A22575A3C6274858294E26BCA2D62F81F8AF6C`。其冻结程序 Mock 验收与 localhost 检查通过；这是基于早期应用代码的本机候选包，并未上传公开 Release。
- 本次没有操作真机。当前源码尚未完成六种模式的逐项真机验收；Mock 数据和软件包检查不能证明真实仪器通信或测量准确度。GitHub Release 目前只发布源码 ZIP；Windows 二进制的 Qt/PySide 与 Microsoft Visual C++ 分发审查仍未关闭。

### 限制

当前版本不宣称经过校准的测量准确度、六种模式均已完成真机验证、当前构建具有长期稳定性，或已通过安全认证。Mock 数据是模拟数据。连接仪表前，请核对测量模式、端子、接线、串口设置和适用的安全注意事项。

详细操作流程见[使用指南](user-guide.md)。本软件不能替代仪器手册或安全测量规范。

## English

This file summarizes the scope of verification without publishing workstation identifiers, serial-port logs, raw measurements, or instrument serial numbers.

## Latest verified application code

- The current code has a six-mode Mock workflow for exercising GUI acquisition and file saving without an instrument.
- The latest application-code commit is `c09337e85714e3eb553d91a86140cd0c0ea5abc6`; it fixes window shutdown so the app waits for the acquisition thread to finish saving the current reading and closing the serial session. Its [GitHub Release](https://github.com/CHICKENSOOUP/fluke8845a-lab/releases/tag/main-build-c09337e85714) and [Actions run 38636996019](https://github.com/CHICKENSOOUP/fluke8845a-lab/actions/runs/38636996019) succeeded. The subsequent test-only commit `f33d7f407da527ccf8ec6c4daeeb4c642473ba79` passed 222 tests on Windows / Python 3.11 and 3.12 in [Actions run 38637555236](https://github.com/CHICKENSOOUP/fluke8845a-lab/actions/runs/38637555236). A new test verifies that an active Mock reading is saved to CSV before shutdown completes.
- The source [Release `main-build-f33d7f407da5`](https://github.com/CHICKENSOOUP/fluke8845a-lab/releases/tag/main-build-f33d7f407da5) ZIP matched `SHA256SUMS.txt` with SHA-256 `845D273A27ADFE294B0E6973DA04540CF71808BA9E80D161A650B4E2BC31F3A5`. From a second fresh extraction with pip cache disabled (`PIP_NO_CACHE_DIR=1`), the first `run_windows.bat --mock-e2e` completed network dependency installation and six-mode GUI Mock acceptance in 45.21 seconds. Each mode saved nine valid simulated readings plus configuration, event, and run-log files. This verifies a cold source-package startup; it is not a hardware test.
- An earlier local Windows x64 ZIP containing desktop and localhost executables is `dist/Fluke8845A_Main_StopFix_20261015_Windows_x64.zip`, SHA-256 `9560E105955C465FC90B6D2C45A22575A3C6274858294E26BCA2D62F81F8AF6C`. Its frozen-app Mock and localhost checks passed, but it is a local candidate built from an earlier application revision and is not attached to the public Release.
- No real instrument was operated during this verification. The current source has not completed real-hardware validation mode by mode. Mock data and package checks do not establish real-meter communication or measurement accuracy. GitHub Releases currently contain only the source ZIP because the Qt/PySide and Microsoft Visual C++ distribution review for a Windows binary remains open.

## Limits

The current release does not claim calibrated accuracy, validation of all six modes on hardware, long-duration stability of this exact build, or safety certification. Mock data is simulated. Before connecting a meter, verify its mode, terminals, wiring, serial settings, and applicable safety precautions.

For the detailed operating workflow, see the [user guide](user-guide.md). Do not use the app as a substitute for the instrument manual or safe measurement practice.
