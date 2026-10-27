# Third-party notices

[简体中文](#简体中文) | [English](#english)

## 简体中文

本项目的源码 ZIP 不包含第三方运行时二进制文件；源码依赖按 `pyproject.toml` 和 `requirements.txt` 单独安装，各依赖继续适用其自身许可证。本文件也随 Windows 便携包提供，而 Windows 便携包会捆绑 CPython、Qt/PySide、NumPy 等第三方运行时二进制文件，并受各自许可证约束。

2026-10-14、2026-10-15 和 2026-10-20 的记录是对应日期的工程审查，不代表之后生成的每个候选包都已逐包审查。2026-10-20 记录列出的发布条件仍需关闭；它不是 2026-10-21 或更新构建的许可证批准。审查背景和状态见[2026-10-20 便携包审查记录](docs/licenses/windows-portable-review-2026-10-20.md)及[Qt 与第三方许可说明](docs/qt-licensing-and-third-party-notices.md)。每个 Windows 构建的精确运行时清单和 Qt/PySide 清单会随候选包生成；必须根据该包的 `BUILD-IDENTITY.json` 检查 `windows-runtime-license-manifest.json` 与 `windows-qt-runtime-inventory.csv`。清单若为 `partial` 或审查记录仍有未关闭事项，不得据此认定该二进制包获准分发。

### Qt for Python（PySide6）与 Qt

应用使用 PySide6（Qt for Python）构建图形界面。项目计划对具备该选项的 Qt/PySide6 组件采用社区版 LGPLv3 路径。PySide6/Qt 也提供 GPL 和商业许可选项，个别模块或捆绑的第三方组件可能适用不同条款。每次二进制构建都必须按实际组件核实许可证。

发布 Windows 二进制程序时，安装包需包含适用的许可证文本和声明、精确组件清单、对应 Qt/PySide 源码，以及允许接收者替换/重新链接这些库并运行程序的说明。2026-10-20 工程审查记录了尚未关闭的发布条件；其中包含的[源码登记表](docs/licenses/windows-source-offer.md)和[替换指南](docs/licenses/windows-replace-qt.md)仍是草稿，不能单独作为合规证明。每个后续构建都需按自身来源标识和清单完成审查。本源码版本不声称任何 Windows 二进制候选包已符合 LGPL 要求。

- [Qt for Python 许可](https://doc.qt.io/qtforpython-6/)
- [Qt 开源 LGPL/GPL 义务](https://www.qt.io/development/open-source-lgpl-obligations)
- [GNU LGPLv3 文本](https://www.gnu.org/licenses/lgpl-3.0.html)
- [LGPLv3 引用的 GNU GPLv3 正文](docs/licenses/GPL-3.0.txt)

### 其他依赖

项目直接依赖 NumPy、PySide6-Essentials、pyserial 和 pyqtgraph。`PySide6-Essentials` 提供 `PySide6` 导入命名空间；源码版不安装完整 PySide6 元包，因此不会由项目依赖额外拉入 Qt PDF 或 Qt WebEngine。请核对构建所安装的精确版本随附的许可证元数据和文件。完整的二进制构建清单还必须包括传递依赖和本机库，不能只列出直接 Python 依赖。

## English

The project's source ZIP does not bundle third-party runtime binaries. Source dependencies are installed separately as declared in `pyproject.toml` and `requirements.txt`; each dependency remains under its own license. This notice is also included in the Windows portable package, which bundles third-party runtime binaries such as CPython, Qt/PySide, and NumPy under their respective licenses.

The 2026-10-14, 2026-10-15, and 2026-10-20 records document engineering reviews performed on those dates; they do not constitute a package-specific review of every later candidate. The 2026-10-20 record lists release gates that still need to be closed and does not approve a package built on or after 2026-10-21. Review background is documented in the [2026-10-20 portable-package review](docs/licenses/windows-portable-review-2026-10-20.md) and [Qt and third-party licensing notes](docs/qt-licensing-and-third-party-notices.md). Each Windows build generates an exact runtime manifest and Qt/PySide inventory inside the candidate package; use that package's `BUILD-IDENTITY.json` to identify and review its `windows-runtime-license-manifest.json` and `windows-qt-runtime-inventory.csv`. A manifest marked `partial` or a review with open items does not clear binary redistribution.

## Qt for Python (PySide6) and Qt

The application uses PySide6 (Qt for Python) for its graphical interface. The project intends to use the community LGPLv3 licensing path for the LGPL-eligible Qt/PySide6 components. PySide6/Qt also offers GPL and commercial licensing options, and individual modules or bundled third-party components can have different terms. The exact license must be checked against the components in each binary build.

When a Windows binary is published, its release package must include the applicable license texts and notices, an exact component inventory, corresponding Qt/PySide source, and instructions that let recipients replace/relink those libraries and run the application. The 2026-10-20 engineering review records open release gates; the [source offer register](docs/licenses/windows-source-offer.md) and [replacement guide](docs/licenses/windows-replace-qt.md) remain drafts and do not establish compliance by themselves. Each later candidate requires its own provenance-bound review. No Windows portable candidate is currently claimed to be LGPL-compliant.

- [Qt for Python licensing](https://doc.qt.io/qtforpython-6/)
- [Qt open-source LGPL/GPL obligations](https://www.qt.io/development/open-source-lgpl-obligations)
- [GNU LGPLv3 text](https://www.gnu.org/licenses/lgpl-3.0.html)
- [GNU GPLv3 text referenced by LGPLv3](docs/licenses/GPL-3.0.txt)

## Other dependencies

The project directly depends on NumPy, PySide6-Essentials, pyserial, and pyqtgraph. `PySide6-Essentials` supplies the `PySide6` import namespace; the source release does not install the full PySide6 meta-package, so Qt PDF and Qt WebEngine are not pulled in by the project dependency. Refer to the license metadata and license files distributed by the exact versions installed for a build. A complete binary-build inventory must include transitive dependencies and native libraries, not only these direct Python packages.
