# Hardware verification status

[简体中文](#简体中文) | [English](#english)

## 简体中文

本说明只记录已取得证据的范围。不会发布工作站标识、串口日志、原始真机测量数据或仪器序列号。

### 当前源码与软件包验证

- 最后一次应用代码变更在 `1d4889eec7071672555b6419febce6676b556433`；本地便携候选包以源码树 `bc51e64a0a8ab33fcaf4d1757c08364dc78d6827` 构建，此后的改动只更新验证记录，不改变应用代码。
- [GitHub Actions 运行 38647820886](https://github.com/CHICKENSOOUP/fluke8845a-lab/actions/runs/38647820886) 在 Windows 上通过完整 pytest：Python 3.11 为 226 项、12.61 秒；Python 3.12 为 226 项、10.10 秒。两个 pytest 步骤各自设置 60 秒上限。源码 ZIP 构建与校验、GHCR Package 和 GitHub Release 发布也均成功。
- [当前提交的 Release](https://github.com/CHICKENSOOUP/fluke8845a-lab/releases/tag/main-build-bc51e64a0a8a) 附有 `Fluke8845A_Source_Project.zip`，GitHub 记录的 ZIP SHA-256 为 `68a7f1eb5f13dac8d148e090b5c5baf0670189cda2e2a19a39109cb639103204`，另附 `SHA256SUMS.txt`。
- 同一提交的公开 GHCR 包为 `ghcr.io/chickensooup/fluke8845a-lab-source:main-build-bc51e64a0a8a`，manifest 摘要为 `sha256:faefd1129c2f01ac7ea92c7f6626f1e4ff6a487bd8040043f0037317a7fa8a43`。
- 当前源码在 Windows 11 x64、CPython 3.12.10、Nuitka 4.1.1 环境下通过 `build_windows.ps1` 构建。完整 226 项本地 pytest 用时 11.88 秒；桌面 EXE 与 localhost EXE 均生成。冻结版 Mock 启动、串口枚举、六模式模拟采集、解压后桌面启动和 localhost `/api/health` 检查均通过。六模式报告明确标记为模拟数据。
- 本地便携候选 ZIP 共 277 个文件，SHA-256 为 `a637dc1b92f19132e7342ae4266ff17e8fbd66116ecf4af4c69ce15442b9a374`；桌面 EXE SHA-256 为 `3e51f8c78410290655756ead70cf9d841531f76de20349a9717758a45c4d4002`。候选包留在本地 `dist`，尚未加入公开 Release，因为下文列出的许可证与来源审查门槛仍未关闭。
- 这次本地构建补足了当前源码的 Windows 冷启动证据；它不代表公开发布了便携程序，也不构成真实仪表验证。

### 真机验证与发布限制

- 本次没有连接或操作 Fluke 8845A。六种模式的真机逐项通信、接线和读数验证，以及测量准确度验证，仍未完成。Mock 结果不能替代这些验证。
- 当前公开 Release 仅含源码 ZIP。Windows 便携程序尚未发布；Qt/PySide LGPL 替换与重新链接验证、完整第三方声明核对，以及 Microsoft Visual C++ 运行库的再分发来源/条款审查仍未关闭。相关审查记录见 [Windows 便携包许可证审查](licenses/windows-portable-review-2026-10-15.md)。
- 本项目不宣称已经校准、通过安全认证、完成六种模式真机验收，或证明当前构建可在真机上连续稳定运行数小时/数天。

连接仪表前，请核对仪表模式、输入端子、接线、串口参数和适用的安全注意事项。详细操作见[使用指南](user-guide.md)；软件不能替代仪器手册或安全测量规范。

## English

This record reports only evidence that was obtained. It does not publish workstation identifiers, serial logs, raw hardware measurements, or the instrument serial number.

### Current source and package checks

- The last application-code change is `1d4889eec7071672555b6419febce6676b556433`. The local portable candidate was built from source tree `bc51e64a0a8ab33fcaf4d1757c08364dc78d6827`; subsequent changes only update verification notes and do not change application code.
- [GitHub Actions run 38647820886](https://github.com/CHICKENSOOUP/fluke8845a-lab/actions/runs/38647820886) passed the full pytest suite on Windows: 226 cases in 12.61 seconds on Python 3.11 and 226 cases in 10.10 seconds on Python 3.12. Each pytest step has a 60-second limit. Source ZIP build and verification, GHCR Package publication, and GitHub Release publication also succeeded.
- The [current commit-specific Release](https://github.com/CHICKENSOOUP/fluke8845a-lab/releases/tag/main-build-bc51e64a0a8a) contains `Fluke8845A_Source_Project.zip`. GitHub reports its SHA-256 as `68a7f1eb5f13dac8d148e090b5c5baf0670189cda2e2a19a39109cb639103204`; `SHA256SUMS.txt` is attached as well.
- The public GHCR package for the same commit is `ghcr.io/chickensooup/fluke8845a-lab-source:main-build-bc51e64a0a8a`, with manifest digest `sha256:faefd1129c2f01ac7ea92c7f6626f1e4ff6a487bd8040043f0037317a7fa8a43`.
- The current source tree was built locally on Windows 11 x64 with CPython 3.12.10 and Nuitka 4.1.1 using `build_windows.ps1`. All 226 pytest cases passed in 11.88 seconds. Both the desktop and localhost executables were created. Frozen Mock startup, serial enumeration, six-mode simulated acquisition, extracted-ZIP desktop startup, and localhost `/api/health` checks passed. The six-mode report explicitly identifies simulated data.
- The local portable candidate contains 277 files. Its ZIP SHA-256 is `a637dc1b92f19132e7342ae4266ff17e8fbd66116ecf4af4c69ce15442b9a374`; the desktop EXE SHA-256 is `3e51f8c78410290655756ead70cf9d841531f76de20349a9717758a45c4d4002`. The candidate remains in local `dist` and is not attached to a public Release because the license and source-delivery review gates below remain open.
- This local build supplies current-source Windows cold-start evidence. It does not mean a portable application has been published or that the instrument has been validated.

### Hardware validation and release limits

- The Fluke 8845A was not connected or operated for this verification. Mode-by-mode hardware communication, wiring, and reading checks across all six modes, as well as measurement-accuracy validation, remain incomplete. Mock results do not replace those checks.
- The current public Release contains only the source ZIP. A Windows portable application has not been released. Qt/PySide LGPL replacement and relinking verification, full third-party attribution review, and review of Microsoft Visual C++ runtime redistribution sources and terms remain open. See the [Windows portable package license review](licenses/windows-portable-review-2026-10-15.md).
- The project does not claim calibration, safety certification, six-mode hardware acceptance, or hours-to-days stability of the current build on a real meter.

Before connecting the meter, verify its mode, input terminals, wiring, serial settings, and applicable safety precautions. See the [user guide](user-guide.md). The software is not a substitute for the instrument manual or safe measurement practice.

