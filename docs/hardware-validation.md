# Hardware verification status

[简体中文](#简体中文) | [English](#english)

## 简体中文

本说明只记录已取得证据的范围。不会发布工作站标识、串口日志、原始真机测量数据或仪器序列号。

### 最新源码与软件包验证记录

- 本记录编写时，最近一次已完成推送验证的提交为 [`676db181ced9dd89a654b790e1d2ad4bd2bb49f1`](https://github.com/CHICKENSOOUP/fluke8845a-lab/commit/676db181ced9dd89a654b790e1d2ad4bd2bb49f1)。[GitHub Actions 运行 38651850396](https://github.com/CHICKENSOOUP/fluke8845a-lab/actions/runs/38651850396) 中 Python 3.11 和 3.12 的测试均成功；工作流还从新解压的源码 ZIP 首次创建环境、安装锁定依赖并启动 Mock 桌面界面。每个 pytest 步骤及首次启动冒烟步骤均设有 60 秒上限。
- [该提交的 GitHub Release](https://github.com/CHICKENSOOUP/fluke8845a-lab/releases/tag/main-build-676db181ced9) 已成功发布 `Fluke8845A_Source_Project.zip` 和 `SHA256SUMS.txt`。源码 ZIP SHA-256 为 `49ca6311b8b710756e0c0687bb601e1cb7b70aa3ddc4ff99efd48dd695ac4dd6`。
- 同一提交的公开 GHCR OCI 包为 `ghcr.io/chickensooup/fluke8845a-lab-source:main-build-676db181ced9`。匿名 manifest 请求返回 HTTP 200，摘要为 `sha256:b285f6a4e20942c312631d4fb846273b85ccf42673adce539f5563fd631a41ef`。
- 更早的 [运行 38651276730](https://github.com/CHICKENSOOUP/fluke8845a-lab/actions/runs/38651276730) 也在 Python 3.11/3.12 及源码 ZIP 首次启动检查中通过，并发布了对应 Release 和 GHCR 包。
- 更早的本地便携候选包由源码树 `bc51e64a0a8ab33fcaf4d1757c08364dc78d6827` 构建，包含 277 个文件；其 ZIP SHA-256 为 `a637dc1b92f19132e7342ae4266ff17e8fbd66116ecf4af4c69ce15442b9a374`。它不是公开 Release 的资产。Qt/PySide 和其他运行库审查门槛仍未关闭，因此公开 Release 仅发布源码。

### 真机验证与发布限制

- 本次没有连接或操作 Fluke 8845A。六种模式的真机逐项通信、接线和读数验证，以及测量准确度验证，仍未完成。Mock 结果不能替代这些验证。
- 当前公开 Release 仅含源码 ZIP。Windows 便携程序尚未发布；Qt/PySide LGPL 替换与重新链接验证、完整第三方声明核对，以及 Microsoft Visual C++ 运行库的再分发来源/条款审查仍未关闭。相关审查记录见 [Windows 便携包许可证审查](licenses/windows-portable-review-2026-10-15.md)。
- 本项目不宣称已经校准、通过安全认证、完成六种模式真机验收，或证明当前构建可在真机上连续稳定运行数小时/数天。

连接仪表前，请核对仪表模式、输入端子、接线、串口参数和适用的安全注意事项。详细操作见[使用指南](user-guide.md)；软件不能替代仪器手册或安全测量规范。

## English

This record reports only evidence that was obtained. It does not publish workstation identifiers, serial logs, raw hardware measurements, or the instrument serial number.

### Latest source and package verification record

- When this record was written, the most recent push verification to complete was commit [`676db181ced9dd89a654b790e1d2ad4bd2bb49f1`](https://github.com/CHICKENSOOUP/fluke8845a-lab/commit/676db181ced9dd89a654b790e1d2ad4bd2bb49f1). Python 3.11 and 3.12 tests passed in [GitHub Actions run 38651850396](https://github.com/CHICKENSOOUP/fluke8845a-lab/actions/runs/38651850396). The workflow also created a fresh environment from an extracted source ZIP, installed its locked dependencies, and launched the Mock desktop UI. Each pytest step and the extracted-source first-launch smoke step has a 60-second limit.
- The [GitHub Release for that commit](https://github.com/CHICKENSOOUP/fluke8845a-lab/releases/tag/main-build-676db181ced9) contains `Fluke8845A_Source_Project.zip` and `SHA256SUMS.txt`. The source ZIP SHA-256 is `49ca6311b8b710756e0c0687bb601e1cb7b70aa3ddc4ff99efd48dd695ac4dd6`.
- The public GHCR OCI package for that commit is `ghcr.io/chickensooup/fluke8845a-lab-source:main-build-676db181ced9`. An anonymous manifest request returned HTTP 200 with digest `sha256:b285f6a4e20942c312631d4fb846273b85ccf42673adce539f5563fd631a41ef`.
- The earlier [run 38651276730](https://github.com/CHICKENSOOUP/fluke8845a-lab/actions/runs/38651276730) also passed on Python 3.11/3.12 and the extracted-source first-launch check, and published its matching Release and GHCR package.
- An earlier local portable candidate was built from source tree `bc51e64a0a8ab33fcaf4d1757c08364dc78d6827` and contains 277 files. Its ZIP SHA-256 is `a637dc1b92f19132e7342ae4266ff17e8fbd66116ecf4af4c69ce15442b9a374`. It is not an asset of the public Release. Qt/PySide and other runtime redistribution review gates remain open, so the public Release contains source only.

### Hardware validation and release limits

- The Fluke 8845A was not connected or operated for this verification. Mode-by-mode hardware communication, wiring, and reading checks across all six modes, as well as measurement-accuracy validation, remain incomplete. Mock results do not replace those checks.
- The current public Release contains only the source ZIP. A Windows portable application has not been released. Qt/PySide LGPL replacement and relinking verification, full third-party attribution review, and review of Microsoft Visual C++ runtime redistribution sources and terms remain open. See the [Windows portable package license review](licenses/windows-portable-review-2026-10-15.md).
- The project does not claim calibration, safety certification, six-mode hardware acceptance, or hours-to-days stability of the current build on a real meter.

Before connecting the meter, verify its mode, input terminals, wiring, serial settings, and applicable safety precautions. See the [user guide](user-guide.md). The software is not a substitute for the instrument manual or safe measurement practice.

