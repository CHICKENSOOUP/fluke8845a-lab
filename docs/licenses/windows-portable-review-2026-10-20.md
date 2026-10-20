# Windows portable package review — 2026-10-20

[简体中文](#简体中文) | [English](#english)

## English

This is the current local review record for Windows portable candidates. It is an engineering inventory, not legal advice or approval to distribute a binary. The exact build is identified by the archived `BUILD-PROVENANCE.json`; the generated runtime manifest and Qt inventory are the per-build file records. The 2026-10-15 review and CSV inventory are historical snapshots and must not be used as the current candidate inventory.

### Mechanical checks

- The build records a SHA-256, byte count, and family hint for every DLL, PYD, and EXE in the frozen application directory. The package verifier compares the inventory against the final archive contents.
- The build generates `windows-qt-runtime-inventory.csv` from that same native-file inventory so the Qt/PySide/Shiboken subset can be reviewed separately.
- The desktop Nuitka command excludes the optional `PySide6.QtTest` import. The package verifier rejects `QtTest.pyd` or `qt6test.dll` if either remains in the final archive. This does not remove the PySide6 Essentials dependency or any other Qt module.
- Mock startup and six-mode Mock acquisition are software checks only. They do not establish real 8845A hardware validation.

### Release gates still open

- Reconcile the exact Qt/PySide modules, plugins, and embedded third-party components in each candidate with their applicable notices.
- Confirm the source, patches, and build provenance corresponding to the exact PySide6/Shiboken wheels; establish and maintain a version-specific source offer before distributing binaries.
- Verify replacement/relinking of the Qt libraries and successful startup of the exact portable candidate.
- Identify the source and applicable redistribution terms for every Microsoft Visual C++ runtime binary.

Until these items are closed with candidate-specific evidence, the Windows portable package remains a local validation candidate and is not approved for public release. A complete hash inventory proves correspondence between recorded files and archive contents; it does not by itself establish redistribution rights.

## 简体中文

本文件是 Windows 便携候选包的当前本地审查记录，属于工程库存审查，不构成法律意见或二进制公开发布批准。每个确切构建由归档内的 `BUILD-PROVENANCE.json` 标识；运行时清单和 Qt 清单按构建生成。2026-10-15 的审查记录和 CSV 是历史快照，不能当作当前候选包的清单。

### 已执行的机械核对

- 构建会为冻结目录中的每个 DLL、PYD、EXE 记录 SHA-256、字节数和家族提示；打包校验会将该清单与最终归档逐项比对。
- 构建会从同一份本机文件清单生成 `windows-qt-runtime-inventory.csv`，单独列出 Qt/PySide/Shiboken 文件。
- 桌面 Nuitka 构建排除可选的 `PySide6.QtTest` 导入；如果最终归档仍含 `QtTest.pyd` 或 `qt6test.dll`，打包校验会失败。这不会移除 PySide6 Essentials 依赖或其他 Qt 模块。
- Mock 启动和六模式 Mock 采集只验证软件模拟功能，不代表完成真实 8845A 硬件验收。

### 仍未关闭的发布条件

- 按每个候选包的实际文件，核对 Qt/PySide 模块、插件和嵌入式第三方组件的适用声明。
- 确认确切 PySide6/Shiboken wheel 对应的源码、补丁和构建来源；公开二进制前建立并持续维护版本对应的源码提供方式。
- 在确切便携包上验证替换/重新链接 Qt 库后可以成功启动。
- 查明每个 Microsoft Visual C++ 运行库文件的来源和适用再分发条款。

在这些事项都有针对确切候选包的证据前，Windows 便携包仍是本地验证候选包，不能作为已获准公开发布的软件。完整哈希清单只能证明记录与归档文件相符，不能单独证明再分发权利。
