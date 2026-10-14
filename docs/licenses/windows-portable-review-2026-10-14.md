# Windows portable build: Qt licensing review

> Historical audit for the 2026-10-14 candidate only. It included a Qt PDF plugin that is absent from the later 2026-10-15 candidate. For the newer snapshot, use [windows-portable-review-2026-10-15.md](windows-portable-review-2026-10-15.md).

[简体中文](#简体中文) | [English](#english)

## English

Review snapshot: `Fluke8845A_DCVGui_20261014/Fluke8845A.dist`, inspected read-only on 2026-10-14. The original build directory was not edited. This is an inventory of that candidate, not a declaration that it is ready to publish.

The inspected folder contained 185 files totaling 160,420,235 bytes. The accompanying [CSV inventory](windows-qt-runtime-inventory-2026-10-14.csv) records 38 Qt/PySide bindings, libraries, and plugins with file versions, sizes, and SHA-256 hashes.

## Qt/PySide components found

- Qt 6.12.0 runtime libraries: Core, Gui, Network, OpenGL, OpenGLWidgets, SVG, Test, and Widgets.
- PySide6 bindings: QtCore, QtGui, QtNetwork, QtOpenGL, QtOpenGLWidgets, QtSvg, QtTest, and QtWidgets; plus the PySide6/Shiboken runtime DLLs.
- Plugins: Windows platform, Direct2D/minimal/offscreen platforms, icon engine, GIF/ICNS/ICO/JPEG/PDF/SVG/TGA/TIFF/WBMP/WebP image formats, Windows style, and TLS backends.
- Qt PDF is a separate component in this build: `qt6pdf.dll` and `qpdf.dll` report file version `6.140.0.0`. Qt's release notes explain that Qt PDF moved with Qt WebEngine to independent versioning at 6.140. The matching public source ref located during this review is only `v6.140.0-rc`; an exact stable source package/ref for the shipped `6.140.0.0` binary was not verified.

The project source imports do not establish which of these files Nuitka actually needs at runtime. Until a new package is built and validated, treat every listed Qt/PySide file as shipped and include it in the licensing review. In particular, do not silently omit Qt PDF or Qt Test from notices or source coverage.

## Intended license path

The release plan is to use the LGPLv3 option for Qt/PySide components that offer it. The PySide6 6.12.0 wheel metadata also lists GPL options; this review does not elect those alternatives or a commercial license. Qt's module licensing pages identify the alternatives for each module. The application itself is separately MIT-licensed.

The full LGPLv3 text is included at [LGPL-3.0.txt](LGPL-3.0.txt). Include a prominent Qt/PySide notice and the applicable third-party notices with the portable ZIP. Retain this file and the CSV as the build-specific audit record; regenerate both for every new build.

## Status

- Inventory and SHA-256 manifest: prepared for this snapshot.
- LGPLv3 text and Qt/PySide notice: prepared in the repository; the bundle must include them.
- Package-specific third-party notice: drafted in [windows-third-party-notices.md](windows-third-party-notices.md); notices for the exact Python/OpenSSL/OpenBLAS/MSVC files and an exact build SBOM remain open.
- Source register: prepared in [windows-source-offer.md](windows-source-offer.md), but Qt PDF source matching is unresolved.
- Replace/relink guide: drafted in [windows-replace-qt.md](windows-replace-qt.md); it has not been verified against a rebuilt package.
- Third-party attribution: still requires the exact wheel SBOM/license files for all bundled dependencies, the selected Qt plugin/component attribution, and the Python/OpenSSL/OpenBLAS/MSVC runtime notices.

**Release gate:** do not attach this candidate to a public release yet. The Qt PDF source match and complete non-Qt runtime notice set are open items, and the replacement procedure needs a successful user-side replacement run on the final ZIP.

## Official references

- [Qt 6.12 licensing and SBOM guidance](https://doc.qt.io/qt-6/licensing.html)
- [Qt 6.12 third-party components](https://doc.qt.io/qt-6/licenses-used-in-qt.html)
- [Qt PDF licensing](https://doc.qt.io/qt-6/qtpdf-licensing.html)
- [Qt's LGPL obligations](https://www.qt.io/development/open-source-lgpl-obligations)
- [Qt WebEngine separate versioning announcement](https://www.qt.io/blog/qt-webengine-will-release-separate)

## 简体中文

### Windows 便携构建 Qt 许可审查

审查对象为 `Fluke8845A_DCVGui_20261014/Fluke8845A.dist`，于 2026-10-14 只读检查。未修改原构建目录。该清单描述候选包，不代表可直接发布。检查目录共含 185 个文件、160,420,235 字节；配套 CSV 记录了 38 个 Qt/PySide 绑定、库和插件的版本、大小及 SHA-256。

发现的组件包括 Qt 6.12.0 Core、Gui、Network、OpenGL、OpenGLWidgets、SVG、Test、Widgets 运行库；对应 PySide6 绑定和 Shiboken 运行库；Windows、Direct2D、minimal、offscreen 等平台插件、图标引擎、图像格式插件、Windows 风格和 TLS 后端。该构建的 Qt PDF 是独立组件，`qt6pdf.dll` 和 `qpdf.dll` 文件版本为 6.140.0.0。审查时仅找到 `v6.140.0-rc` 源码 ref，未核实与该稳定二进制完全匹配的源码包/ref。

源码中的 import 不能证明 Nuitka 运行时实际需要哪些文件。重新构建并验证前，许可审查应将清单中的所有 Qt/PySide 文件视为已分发；不能遗漏 Qt PDF 或 Qt Test。

计划对可选用 LGPLv3 的 Qt/PySide 组件采用 LGPLv3 路径；PySide6 6.12.0 wheel 元数据也列有 GPL 选项。本审查未选择 GPL 或商业许可。应用自身另行采用 MIT。随便携包提供完整 LGPLv3 文本、显著的 Qt/PySide 声明和适用第三方声明，并为每个构建重新生成审计记录。

### 状态与发布门槛

- 本快照的组件与 SHA-256 清单已准备。
- 仓库已准备 LGPLv3 文本及 Qt/PySide 声明，但实际包仍须包含它们。
- 第三方声明仍缺最终构建中 Python/OpenSSL/OpenBLAS/MSVC 文件的精确声明和完整 SBOM。
- 源码登记表尚未解决 Qt PDF 源码对应关系。
- Qt 替换指南尚未在重建后的最终包上验证。
- 仍需核对随包依赖、Qt 插件/组件归属以及 Python 等运行时声明。

**发布门槛：**在 Qt PDF 对应源码、非 Qt 运行库声明和最终 ZIP 上的替换验证完成之前，不要将候选程序附加到公开版本。
