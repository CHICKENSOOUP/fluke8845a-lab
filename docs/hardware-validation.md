# Hardware verification status

[简体中文](#简体中文) | [English](#english)

## 简体中文

本说明只记录已取得证据的范围。不会发布工作站标识、串口日志、原始真机测量数据或仪器序列号。

### 当前源码与软件包验证

- 当前源码提交：`4b5144490bb5f1125b82e840ea1fdbc8941abb1c`。新增的长运行回归测试向 GUI 输入 110,000 个模拟读数，确认绘图缓存只保留最近 100,000 点，同时全程统计仍计入全部样本。
- [GitHub Actions 运行 38644381286](https://github.com/CHICKENSOOUP/fluke8845a-lab/actions/runs/38644381286) 在 Windows 上通过完整 pytest：Python 3.11 为 225 项、12.01 秒；Python 3.12 为 225 项、12.60 秒。两个 pytest 步骤各自设置 60 秒超时。源码 ZIP 构建与校验、GHCR Package 发布和 GitHub Release 发布也均成功。
- [提交对应的 Release](https://github.com/CHICKENSOOUP/fluke8845a-lab/releases/tag/main-build-4b5144490bb5) 附有 `Fluke8845A_Source_Project.zip`，GitHub 记录的 ZIP SHA-256 为 `4683a358412b17986e53368fc9971ad8cfacbf315ca3a850c569ac347feb6e4c`，另附 `SHA256SUMS.txt`。
- 同一提交的公开 GHCR 包为 `ghcr.io/chickensooup/fluke8845a-lab-source:main-build-4b5144490bb5`，推送摘要为 `sha256:5eeb84d490f2715c9af04b1a83b1f3808eb53bc1b08aa017c0e996b98d589b9d`。
- Actions 的源码包构建/校验成功不等于在新 Windows 环境完成解压冷启动。本记录不把早期提交的启动验证外推到这个提交。

### 真机验证与发布限制

- 本次没有连接或操作 Fluke 8845A。六种模式的真机逐项通信、接线和读数验证，以及测量准确度验证，仍未完成。Mock 结果不能替代这些验证。
- 当前公开 Release 仅含源码 ZIP。Windows 便携程序尚未发布；Qt/PySide LGPL 替换与重新链接验证、完整第三方声明核对，以及 Microsoft Visual C++ 运行库的再分发来源/条款审查仍未关闭。相关审查记录见 [Windows 便携包许可证审查](licenses/windows-portable-review-2026-10-15.md)。
- 本项目不宣称已经校准、通过安全认证、完成六种模式真机验收，或证明当前构建可在真机上连续稳定运行数小时/数天。

连接仪表前，请核对仪表模式、输入端子、接线、串口参数和适用的安全注意事项。详细操作见[使用指南](user-guide.md)；软件不能替代仪器手册或安全测量规范。

## English

This record reports only evidence that was obtained. It does not publish workstation identifiers, serial logs, raw hardware measurements, or the instrument serial number.

### Current source and package checks

- Current source commit: `4b5144490bb5f1125b82e840ea1fdbc8941abb1c`. The long-run regression test feeds 110,000 simulated readings into the GUI and verifies that the plot cache retains only the newest 100,000 points while full-run statistics still include every sample.
- [GitHub Actions run 38644381286](https://github.com/CHICKENSOOUP/fluke8845a-lab/actions/runs/38644381286) passed the full pytest suite on Windows: 225 cases in 12.01 seconds on Python 3.11 and 225 cases in 12.60 seconds on Python 3.12. Each pytest step has a 60-second timeout. Source ZIP build and verification, GHCR Package publication, and GitHub Release publication also succeeded.
- The [commit-specific Release](https://github.com/CHICKENSOOUP/fluke8845a-lab/releases/tag/main-build-4b5144490bb5) contains `Fluke8845A_Source_Project.zip`. GitHub reports its SHA-256 as `4683a358412b17986e53368fc9971ad8cfacbf315ca3a850c569ac347feb6e4c`; `SHA256SUMS.txt` is attached as well.
- The public GHCR package for the same commit is `ghcr.io/chickensooup/fluke8845a-lab-source:main-build-4b5144490bb5`, with push digest `sha256:5eeb84d490f2715c9af04b1a83b1f3808eb53bc1b08aa017c0e996b98d589b9d`.
- Successful source-package build and verification in Actions do not establish a fresh Windows extraction/startup check. This record does not extend an earlier commit's startup evidence to this commit.

### Hardware validation and release limits

- The Fluke 8845A was not connected or operated for this verification. Mode-by-mode hardware communication, wiring, and reading checks across all six modes, as well as measurement-accuracy validation, remain incomplete. Mock results do not replace those checks.
- The current public Release contains only the source ZIP. A Windows portable application has not been released. Qt/PySide LGPL replacement and relinking verification, full third-party attribution review, and review of Microsoft Visual C++ runtime redistribution sources and terms remain open. See the [Windows portable package license review](licenses/windows-portable-review-2026-10-15.md).
- The project does not claim calibration, safety certification, six-mode hardware acceptance, or hours-to-days stability of the current build on a real meter.

Before connecting the meter, verify its mode, input terminals, wiring, serial settings, and applicable safety precautions. See the [user guide](user-guide.md). The software is not a substitute for the instrument manual or safe measurement practice.

