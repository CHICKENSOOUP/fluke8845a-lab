# Third-party notices

[简体中文](#简体中文) | [English](#english)

## 简体中文

本源码仓库不包含第三方运行时二进制文件。依赖按 `pyproject.toml` 和 `requirements.txt` 单独安装，各依赖继续适用其自身许可证。

### Qt for Python（PySide6）与 Qt

应用使用 PySide6（Qt for Python）构建图形界面。项目计划对具备该选项的 Qt/PySide6 组件采用社区版 LGPLv3 路径。PySide6/Qt 也提供 GPL 和商业许可选项，个别模块或捆绑的第三方组件可能适用不同条款。每次二进制构建都必须按实际组件核实许可证。

发布 Windows 二进制程序时，安装包需包含适用的许可证文本和声明、精确组件清单、对应 Qt/PySide 源码，以及允许接收者替换/重新链接这些库并运行程序的说明。当前本地候选包的审查材料见[Qt 许可审查](docs/licenses/windows-portable-review-2026-10-15.md)、[组件清单](docs/licenses/windows-qt-runtime-inventory-2026-10-15.csv)、[源码登记表](docs/licenses/windows-source-offer.md)和[替换指南](docs/licenses/windows-replace-qt.md)。本源码版本不声称二进制分发已符合 LGPL 要求。

- [Qt for Python 许可](https://doc.qt.io/qtforpython-6/)
- [Qt 开源 LGPL/GPL 义务](https://www.qt.io/development/open-source-lgpl-obligations)
- [GNU LGPLv3 文本](https://www.gnu.org/licenses/lgpl-3.0.html)

### 其他依赖

项目直接依赖 NumPy、PySide6-Essentials、pyserial 和 pyqtgraph。`PySide6-Essentials` 提供 `PySide6` 导入命名空间；源码版不安装完整 PySide6 元包，因此不会由项目依赖额外拉入 Qt PDF 或 Qt WebEngine。请核对构建所安装的精确版本随附的许可证元数据和文件。完整的二进制构建清单还必须包括传递依赖和本机库，不能只列出直接 Python 依赖。

## English

This source repository does not bundle third-party runtime binaries. Dependencies are installed separately as declared in `pyproject.toml` and `requirements.txt`; each dependency remains under its own license.

## Qt for Python (PySide6) and Qt

The application uses PySide6 (Qt for Python) for its graphical interface. The project intends to use the community LGPLv3 licensing path for the LGPL-eligible Qt/PySide6 components. PySide6/Qt also offers GPL and commercial licensing options, and individual modules or bundled third-party components can have different terms. The exact license must be checked against the components in each binary build.

When a Windows binary is published, its release package must include the applicable license texts and notices, an exact component inventory, corresponding Qt/PySide source, and instructions that let recipients replace/relink those libraries and run the application. The current local candidate's review materials are linked in [Qt licensing review](docs/licenses/windows-portable-review-2026-10-15.md), [component inventory](docs/licenses/windows-qt-runtime-inventory-2026-10-15.csv), [source offer register](docs/licenses/windows-source-offer.md), and [replacement guide](docs/licenses/windows-replace-qt.md). The source-only release does not claim that a binary distribution is LGPL-compliant.

- [Qt for Python licensing](https://doc.qt.io/qtforpython-6/)
- [Qt open-source LGPL/GPL obligations](https://www.qt.io/development/open-source-lgpl-obligations)
- [GNU LGPLv3 text](https://www.gnu.org/licenses/lgpl-3.0.html)

## Other dependencies

The project directly depends on NumPy, PySide6-Essentials, pyserial, and pyqtgraph. `PySide6-Essentials` supplies the `PySide6` import namespace; the source release does not install the full PySide6 meta-package, so Qt PDF and Qt WebEngine are not pulled in by the project dependency. Refer to the license metadata and license files distributed by the exact versions installed for a build. A complete binary-build inventory must include transitive dependencies and native libraries, not only these direct Python packages.
