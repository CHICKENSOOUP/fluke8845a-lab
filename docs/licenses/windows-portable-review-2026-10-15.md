# Windows portable package review — 2026-10-15

[简体中文](#简体中文) | [English](#english)

## English

### Snapshot reviewed

- Candidate: `Fluke8845A_Essentials_6_12_7_Windows_x64.zip`, built on Windows 11 x64 with CPython 3.12.10.
- Qt/PySide runtime: Qt 6.12.0, `PySide6-Essentials` 6.12.0, and `shiboken6` 6.12.0.
- The rebuilt ZIP contains 277 files. The [Qt/PySide inventory](windows-qt-runtime-inventory-2026-10-15.csv) records 59 binding, library, plugin, and Shiboken files with sizes, PE versions where present, and SHA-256 hashes.
- The frozen desktop executable SHA-256 is `B4D83AF40B85B6E5B0B5C60582F488BFEB8279FEADCEE6A868D485497608C1B7` (also recorded in the six-mode Mock report).
- The ZIP includes a generated runtime license manifest with 25 file records for CPython 3.12.10, NumPy 2.5.4 (including its OpenBLAS/LAPACK/GCC notices), pyqtgraph 0.14.0, pySerial 3.5, OpenSSL 3.0.16, and the Nuitka 4.1.1 runtime exception/notice. It records package versions, archive-relative paths, byte counts, and SHA-256 values; the package verifier checks each archived license file against the manifest.

### Qt components

The package contains Qt Core, Gui, Network, OpenGL, OpenGLWidgets, SVG, Test, and Widgets, plus the Windows platform, style, icon, image-format, and TLS plugins listed in the inventory. The desktop package explicitly removes the unused `qpdf.dll` image-format plugin. The localhost package excludes Qt image-format plugins. A scan of the ZIP found no `qpdf.dll`, `qt6pdf.dll`, `QtPdf.pyd`, or Qt WebEngine binary.

The Qt 6.12.0 module and PySide6/Shiboken source versions match the files recorded in the inventory. Official source archives and their published SHA-256 values are listed in the [source delivery register](windows-source-offer.md). Qt's licensing page identifies LGPLv3 as the open-source path for LGPL-eligible modules; Qt for Python is offered under LGPLv3/GPLv3, and the included Qt modules do not appear in the Qt 6.12 GPL-only module list. The Qt Test module's page separately lists LGPLv3 and GPLv2. These references do not replace a full license review.

### Verification

The frozen package passed the bounded Mock desktop startup, serial-port enumeration, six-mode Mock acquisition, extracted-ZIP startup, and localhost health checks. The Mock report identifies all six modes as simulated and verifies nine saved rows per mode. No real 8845A measurement is claimed.

### Remaining release gates

- This is a candidate audit, not legal advice or approval to distribute the binary.
- The copied license texts cover the listed CPython, NumPy/OpenBLAS, pyqtgraph, pySerial, OpenSSL, and Nuitka runtime materials. The complete mapping of every bundled native component still needs final review, especially the Microsoft Visual C++ DLL source and applicable redistribution terms.
- Qt/PySide module and embedded third-party attribution must be reconciled against this exact bundle.
- The Qt/PySide replacement and relaunch instructions have not been exercised with a modified/relinked library against this exact ZIP.
- The official Qt and PySide source archives are identified in the source register, but wheel patches/provenance still need confirmation and a durable, release-specific source offer for binary recipients has not been published.

Until those items are closed, the GitHub workflow continues to publish source-only Releases and GHCR packages; it does not attach the Windows ZIP.

### References

- [Qt 6.12 licensing](https://doc.qt.io/qt-6/licensing.html)
- [Third-party code used in Qt 6.12](https://doc.qt.io/qt-6/licenses-used-in-qt.html)
- [Qt Test licensing](https://doc.qt.io/qt-6/qttest-index.html)
- [Qt for Python licensing](https://doc.qt.io/qtforpython-6/)
- [Qt's LGPL/GPL distribution obligations](https://www.qt.io/development/open-source-lgpl-obligations)

## 简体中文

### 审查快照

- 候选包：`Fluke8845A_Essentials_6_12_7_Windows_x64.zip`，Windows 11 x64、CPython 3.12.10 构建。
- Qt/PySide 运行时：Qt 6.12.0、`PySide6-Essentials` 6.12.0、`shiboken6` 6.12.0。
- 重新生成的 ZIP 共 277 个文件。[Qt/PySide 组件清单](windows-qt-runtime-inventory-2026-10-15.csv)记录了 59 个绑定、库、插件和 Shiboken 文件，并包含文件大小、可读取的 PE 版本及 SHA-256。
- 冻结桌面 EXE 的 SHA-256：`B4D83AF40B85B6E5B0B5C60582F488BFEB8279FEADCEE6A868D485497608C1B7`（六模式 Mock 报告中也记录了此值）。
- ZIP 内含生成的运行时许可清单，共 25 条文件记录，涉及 CPython 3.12.10、NumPy 2.5.4（含 OpenBLAS/LAPACK/GCC 声明）、pyqtgraph 0.14.0、pySerial 3.5、OpenSSL 3.0.16 和 Nuitka 4.1.1 运行库例外/声明。清单记录包版本、相对包内路径、文件大小和 SHA-256；打包校验器会逐项核对归档中的实际内容。

### Qt 组件

软件包包含 Qt Core、Gui、Network、OpenGL、OpenGLWidgets、SVG、Test 和 Widgets，以及清单所列 Windows 平台、样式、图标、图像格式和 TLS 插件。桌面包会删除未使用的 `qpdf.dll` 图像格式插件；localhost 子包不包含 Qt 图像格式插件。对 ZIP 的扫描未发现 `qpdf.dll`、`qt6pdf.dll`、`QtPdf.pyd` 或 Qt WebEngine 二进制文件。

Qt 6.12.0 和 PySide6/Shiboken 6.12.0 的源码版本与组件清单匹配。官方源码包和发布的 SHA-256 见[源码交付登记表](windows-source-offer.md)。Qt 6.12 许可页面将 LGPLv3 列为适用组件的开源发行路径；Qt for Python 提供 LGPLv3/GPLv3 选项，清单中的 Qt 模块不在 Qt 6.12 的 GPL-only 模块列表中。Qt Test 页面也单独列出 LGPLv3 和 GPLv2。这些参考资料不替代完整许可证审查。

### 验证

冻结包通过了有界 Mock 桌面启动、串口枚举、六模式 Mock 采集、解压后启动和 localhost 健康检查。Mock 报告将六种模式都明确标记为模拟，并核验每种模式保存了 9 行数据。这里不声称已完成真实 8845A 测量。

### 仍未关闭的发布门槛

- 本文件是候选包审查记录，不构成法律意见或二进制发布批准。
- 已为清单所列 CPython、NumPy/OpenBLAS、pyqtgraph、pySerial、OpenSSL 和 Nuitka 运行库复制许可文本。所有本机文件与许可的完整映射仍需最终核对，特别是 Microsoft Visual C++ DLL 的来源及适用再分发条款。
- Qt/PySide 模块和嵌入式第三方组件仍需按这个确切候选包逐项核对声明。
- 尚未使用修改或重新链接过的库，在此确切 ZIP 上完成 Qt/PySide 替换和重启验证。
- Qt 和 PySide 官方源码包已在登记表列出，但仍需确认 wheel 的补丁/来源，并为二进制接收者提供稳定、对应版本的源码交付要约。

这些事项关闭之前，GitHub 工作流继续只发布源码 Release 和 GHCR 包，不附加 Windows ZIP。
