# Hardware verification status

[简体中文](#简体中文) | [English](#english)

## 简体中文

本说明只记录已取得证据的范围。不会发布工作站标识、串口日志、原始真机测量数据或仪器序列号。

### 当前源码与软件包验证

- 当前源码提交为 [`61560824b8944b548f75e44b8a55e468356f2fc2`](https://github.com/CHICKENSOOUP/fluke8845a-lab/commit/61560824b8944b548f75e44b8a55e468356f2fc2)。源码 ZIP 启动器现在只接受有对应锁文件的 64 位 Python 3.11/3.12，并按版本选择锁定依赖。
- 本机完整 pytest 套件在 45 秒硬停止限制内通过。源码打包器随后创建并校验了 111 个文件，且确认两份依赖锁均在归档内。这些检查验证锁文件选择逻辑和发布归档内容，不等同于全新 Windows 用户目录中的首次联网安装。
- [GitHub Actions 运行 38650054326](https://github.com/CHICKENSOOUP/fluke8845a-lab/actions/runs/38650054326) 成功完成 Python 3.11 和 3.12 测试、源码 ZIP 构建、GHCR 发布与 GitHub Release 发布；每个 pytest 步骤的上限为 60 秒。
- [当前 Release](https://github.com/CHICKENSOOUP/fluke8845a-lab/releases/tag/main-build-61560824b894) 附有 `Fluke8845A_Source_Project.zip` 和 `SHA256SUMS.txt`。源码 ZIP SHA-256 为 `6f8c152495e0202bfd4fae820b1fe275b2c3984355d32b00857aa5d921df4d7b`。
- 同一提交的公开 GHCR 包为 `ghcr.io/chickensooup/fluke8845a-lab-source:main-build-61560824b894`；匿名 manifest 请求返回 HTTP 200，摘要为 `sha256:36c4084a00629a8303d9978cfc5f6148616d683762f378d0fd3a5da2f110b635`。
- 较早的本地便携候选包由源码树 `bc51e64a0a8ab33fcaf4d1757c08364dc78d6827` 构建，包含 277 个文件；其 ZIP SHA-256 为 `a637dc1b92f19132e7342ae4266ff17e8fbd66116ecf4af4c69ce15442b9a374`。它不是当前公开 Release 的资产。Qt/PySide 和其他运行库审查门槛仍未关闭，因此当前 Release 仅发布源码。

### 真机验证与发布限制

- 本次没有连接或操作 Fluke 8845A。六种模式的真机逐项通信、接线和读数验证，以及测量准确度验证，仍未完成。Mock 结果不能替代这些验证。
- 当前公开 Release 仅含源码 ZIP。Windows 便携程序尚未发布；Qt/PySide LGPL 替换与重新链接验证、完整第三方声明核对，以及 Microsoft Visual C++ 运行库的再分发来源/条款审查仍未关闭。相关审查记录见 [Windows 便携包许可证审查](licenses/windows-portable-review-2026-10-15.md)。
- 本项目不宣称已经校准、通过安全认证、完成六种模式真机验收，或证明当前构建可在真机上连续稳定运行数小时/数天。

连接仪表前，请核对仪表模式、输入端子、接线、串口参数和适用的安全注意事项。详细操作见[使用指南](user-guide.md)；软件不能替代仪器手册或安全测量规范。

## English

This record reports only evidence that was obtained. It does not publish workstation identifiers, serial logs, raw hardware measurements, or the instrument serial number.

### Current source and package checks

- The current source commit is [`61560824b8944b548f75e44b8a55e468356f2fc2`](https://github.com/CHICKENSOOUP/fluke8845a-lab/commit/61560824b8944b548f75e44b8a55e468356f2fc2). The source ZIP launcher now accepts only 64-bit Python 3.11/3.12 with a matching dependency lock.
- The complete local pytest suite passed with a 45-second hard-stop limit. The source packager then created and verified a 111-file archive and confirmed that both dependency locks are included. These checks cover lock selection and archive contents; they do not establish a fresh first network install in a clean Windows user directory.
- [GitHub Actions run 38650054326](https://github.com/CHICKENSOOUP/fluke8845a-lab/actions/runs/38650054326) successfully completed the Python 3.11 and 3.12 test jobs, source ZIP build, GHCR publication, and GitHub Release publication. Each pytest step is capped at 60 seconds.
- The [current Release](https://github.com/CHICKENSOOUP/fluke8845a-lab/releases/tag/main-build-61560824b894) contains `Fluke8845A_Source_Project.zip` and `SHA256SUMS.txt`. The source ZIP SHA-256 is `6f8c152495e0202bfd4fae820b1fe275b2c3984355d32b00857aa5d921df4d7b`.
- The public GHCR package for the same commit is `ghcr.io/chickensooup/fluke8845a-lab-source:main-build-61560824b894`. An anonymous manifest request returned HTTP 200 with digest `sha256:36c4084a00629a8303d9978cfc5f6148616d683762f378d0fd3a5da2f110b635`.
- An earlier local portable candidate was built from source tree `bc51e64a0a8ab33fcaf4d1757c08364dc78d6827` and contains 277 files. Its ZIP SHA-256 is `a637dc1b92f19132e7342ae4266ff17e8fbd66116ecf4af4c69ce15442b9a374`. It is not an asset of the current public Release. Qt/PySide and other runtime redistribution review gates remain open, so the current Release contains source only.

### Hardware validation and release limits

- The Fluke 8845A was not connected or operated for this verification. Mode-by-mode hardware communication, wiring, and reading checks across all six modes, as well as measurement-accuracy validation, remain incomplete. Mock results do not replace those checks.
- The current public Release contains only the source ZIP. A Windows portable application has not been released. Qt/PySide LGPL replacement and relinking verification, full third-party attribution review, and review of Microsoft Visual C++ runtime redistribution sources and terms remain open. See the [Windows portable package license review](licenses/windows-portable-review-2026-10-15.md).
- The project does not claim calibration, safety certification, six-mode hardware acceptance, or hours-to-days stability of the current build on a real meter.

Before connecting the meter, verify its mode, input terminals, wiring, serial settings, and applicable safety precautions. See the [user guide](user-guide.md). The software is not a substitute for the instrument manual or safe measurement practice.

