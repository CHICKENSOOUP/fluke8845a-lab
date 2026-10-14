# Hardware verification status

[简体中文](#简体中文) | [English](#english)

## 简体中文

本说明只记录已取得证据的范围。不会发布工作站标识、串口日志、原始真机测量数据或仪器序列号。

### 最新源码与软件包验证记录

- 2026-10-15 已完成推送验证的最新提交为 [`1c625ee75e954d262c80a662da9527f07bf2e199`](https://github.com/CHICKENSOOUP/fluke8845a-lab/commit/1c625ee75e954d262c80a662da9527f07bf2e199)。[GitHub Actions 运行 38653695800](https://github.com/CHICKENSOOUP/fluke8845a-lab/actions/runs/38653695800) 的 Python 3.11/3.12 测试及源码 ZIP 解压后首次 Mock 启动均成功；Release 和 GHCR 发布步骤也成功。pytest 与首次启动步骤各自限制在 60 秒内。
- [该提交的 Release](https://github.com/CHICKENSOOUP/fluke8845a-lab/releases/tag/main-build-1c625ee75e95) 含源码 ZIP 和 `SHA256SUMS.txt`；源码 ZIP SHA-256 为 `03e401fc06ff33c1d71662d64c7b5575f7ef221e71be8f60c93129f854207b40`。对应的公开 GHCR 包 `ghcr.io/chickensooup/fluke8845a-lab-source:main-build-1c625ee75e95` 的匿名 manifest 返回 HTTP 200，摘要为 `sha256:342d8ef1a7dd0a1ecc9c898082165f238e1dec0f7b1dfea020166e590479e7bd`。
- 2026-10-15 在 Windows 本机启动了 `Fluke8845A_Essentials_6_12_7` 便携候选包的桌面程序，并目视检查 Mock 界面布局；未观察到控件重叠。随后以 `--mock-e2e` 在 2 秒内完成六模式模拟采集与保存核验：DCV 9、ACV 8、DCI 9、ACI 9、RES2 9、RES4 9 条有效样本；每种模式均确认生成 `measurements.csv`、`config.json`、`events.csv` 和 `run.log`。报告标明 `simulation=true`、`status=ok`，EXE SHA-256 为 `b4d83af40b85b6e5b0b5c60582f488bfeb8279feadcee6a868d485497608c1b7`。这是本机 Mock 验证，不是真机验证，也不是公开 Release 资产。
- 更早的提交 [`676db181ced9dd89a654b790e1d2ad4bd2bb49f1`](https://github.com/CHICKENSOOUP/fluke8845a-lab/commit/676db181ced9dd89a654b790e1d2ad4bd2bb49f1) 的 [GitHub Actions 运行 38651850396](https://github.com/CHICKENSOOUP/fluke8845a-lab/actions/runs/38651850396) 中，Python 3.11/3.12 测试、源码 ZIP 首次启动和发布步骤均成功；pytest 与首次启动冒烟各自限制为 60 秒。
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

- The latest completed push verification on 2026-10-15 is commit [`1c625ee75e954d262c80a662da9527f07bf2e199`](https://github.com/CHICKENSOOUP/fluke8845a-lab/commit/1c625ee75e954d262c80a662da9527f07bf2e199). Python 3.11/3.12 tests, first Mock launch from an extracted source ZIP, and both publication steps succeeded in [GitHub Actions run 38653695800](https://github.com/CHICKENSOOUP/fluke8845a-lab/actions/runs/38653695800). Each pytest and first-launch step is capped at 60 seconds.
- The [Release for that commit](https://github.com/CHICKENSOOUP/fluke8845a-lab/releases/tag/main-build-1c625ee75e95) contains the source ZIP and `SHA256SUMS.txt`; the ZIP SHA-256 is `03e401fc06ff33c1d71662d64c7b5575f7ef221e71be8f60c93129f854207b40`. The public GHCR package `ghcr.io/chickensooup/fluke8845a-lab-source:main-build-1c625ee75e95` returned HTTP 200 to an anonymous manifest request with digest `sha256:342d8ef1a7dd0a1ecc9c898082165f238e1dec0f7b1dfea020166e590479e7bd`.
- On Windows on 2026-10-15, the `Fluke8845A_Essentials_6_12_7` portable candidate was launched and its Mock desktop layout was visually checked; no overlapping controls were observed. Its `--mock-e2e` flow completed six simulated modes and storage checks in under two seconds: DCV 9, ACV 8, DCI 9, ACI 9, RES2 9, and RES4 9 valid samples. Each mode verified `measurements.csv`, `config.json`, `events.csv`, and `run.log`. The report marked `simulation=true` and `status=ok`; the EXE SHA-256 was `b4d83af40b85b6e5b0b5c60582f488bfeb8279feadcee6a868d485497608c1b7`. This is local Mock evidence, not hardware verification or a public Release asset.
- An earlier commit, [`676db181ced9dd89a654b790e1d2ad4bd2bb49f1`](https://github.com/CHICKENSOOUP/fluke8845a-lab/commit/676db181ced9dd89a654b790e1d2ad4bd2bb49f1), passed Python 3.11/3.12 tests, extracted-source first launch, and publication in [GitHub Actions run 38651850396](https://github.com/CHICKENSOOUP/fluke8845a-lab/actions/runs/38651850396). Each pytest and first-launch smoke step is capped at 60 seconds.
- The [GitHub Release for that commit](https://github.com/CHICKENSOOUP/fluke8845a-lab/releases/tag/main-build-676db181ced9) contains `Fluke8845A_Source_Project.zip` and `SHA256SUMS.txt`. The source ZIP SHA-256 is `49ca6311b8b710756e0c0687bb601e1cb7b70aa3ddc4ff99efd48dd695ac4dd6`.
- The public GHCR OCI package for that commit is `ghcr.io/chickensooup/fluke8845a-lab-source:main-build-676db181ced9`. An anonymous manifest request returned HTTP 200 with digest `sha256:b285f6a4e20942c312631d4fb846273b85ccf42673adce539f5563fd631a41ef`.
- The earlier [run 38651276730](https://github.com/CHICKENSOOUP/fluke8845a-lab/actions/runs/38651276730) also passed on Python 3.11/3.12 and the extracted-source first-launch check, and published its matching Release and GHCR package.
- An earlier local portable candidate was built from source tree `bc51e64a0a8ab33fcaf4d1757c08364dc78d6827` and contains 277 files. Its ZIP SHA-256 is `a637dc1b92f19132e7342ae4266ff17e8fbd66116ecf4af4c69ce15442b9a374`. It is not an asset of the public Release. Qt/PySide and other runtime redistribution review gates remain open, so the public Release contains source only.

### Hardware validation and release limits

- The Fluke 8845A was not connected or operated for this verification. Mode-by-mode hardware communication, wiring, and reading checks across all six modes, as well as measurement-accuracy validation, remain incomplete. Mock results do not replace those checks.
- The current public Release contains only the source ZIP. A Windows portable application has not been released. Qt/PySide LGPL replacement and relinking verification, full third-party attribution review, and review of Microsoft Visual C++ runtime redistribution sources and terms remain open. See the [Windows portable package license review](licenses/windows-portable-review-2026-10-15.md).
- The project does not claim calibration, safety certification, six-mode hardware acceptance, or hours-to-days stability of the current build on a real meter.

Before connecting the meter, verify its mode, input terminals, wiring, serial settings, and applicable safety precautions. See the [user guide](user-guide.md). The software is not a substitute for the instrument manual or safe measurement practice.

