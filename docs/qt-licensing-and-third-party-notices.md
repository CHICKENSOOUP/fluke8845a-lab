# Qt 与第三方许可说明

[简体中文](#简体中文) | [English](#english)

## 简体中文

本仓库的首个 GitHub 源码版不包含 Qt/PySide6 二进制运行库。应用使用 PySide6 调用 Qt 构建 GUI，使用 pyserial 和项目自身的 SCPI 驱动进行串口通信；未使用 Qt SerialPort。

## 计划采用的 Qt 发行路径

项目计划对 LGPL 许可的 Qt/PySide6 组件采用社区版 LGPLv3 发行路径。PySide6 的发行元数据也列有 GPL 许可选项；Qt 个别模块和第三方组件可能有不同许可证。必须针对最终 `.dist` 目录里的每个 Qt 模块、DLL、插件和嵌入组件逐项核验，不能只根据 Python 源码中的 import 判断。

## Windows 二进制发布材料

已为 2026-10-15 的只读便携包候选准备以下材料：

- [Qt/PySide 组件、文件版本和 SHA-256 清单](licenses/windows-qt-runtime-inventory-2026-10-15.csv)
- [当前候选包审查与未关闭事项](licenses/windows-portable-review-2026-10-15.md)
- [便携包第三方声明草稿](licenses/windows-third-party-notices.md)
- [Qt/PySide 对应源码登记表和源码提供声明草稿](licenses/windows-source-offer.md)
- [Qt 动态库替换和运行步骤草稿](licenses/windows-replace-qt.md)
- LGPLv3 完整文本：`docs/licenses/LGPL-3.0.txt`

当前 6.12.0 候选包不含 Qt PDF、Qt WebEngine 或 `qpdf.dll`，相应的历史源码匹配问题已排除。其余材料仍是候选审查记录/草稿：非 Qt 运行库的完整声明，以及修改或重新链接 Qt 后在该 ZIP 上运行的验证仍未完成。源码归档和校验值见源码登记表；在核对 wheel 补丁来源并建立稳定源码交付方式前，不把它视为已完成源码提供要约。

## Windows 二进制发布要求

二进制版发布时应仿照可审计的第三方许可清单流程：

1. 从每次确切的构建产物生成依赖与许可证清单，包含 Qt 模块、插件、Shiboken 和其他本机库。当前候选的快照清单见上方链接。
2. 随包提供适用的完整许可证文本、Qt 使用声明及第三方归属声明。
3. 提供本次实际使用的 Qt/PySide 库的对应源码及适用修改，或建立符合许可证条款的源码交付方式。
4. 保持 Qt 库可替换，并写明替换/重新链接后如何运行程序；在发布前实际验证流程。
5. 排除未经确认或应用不需要的模块；若包含 GPL-only 组件，先确定兼容的整体授权路径。

本源码仓库不声称当前 Windows 便携包已满足上述条件。准确的第三方声明、源码交付承诺和替换运行验证全部闭环前，不发布 Windows 二进制包。

## 官方资料

- [Qt for Python](https://doc.qt.io/qtforpython-6/)
- [Qt 官方 LGPL/GPL 发行义务](https://www.qt.io/development/open-source-lgpl-obligations)
- [Qt 6 许可信息](https://doc.qt.io/qt-6/licensing.html)
- [GNU LGPLv3](https://www.gnu.org/licenses/lgpl-3.0.html)

## English

# Qt and third-party licensing notes

The first GitHub source release does not include Qt/PySide6 runtime binaries. The application uses PySide6 to build its GUI and pyserial plus the project's own SCPI driver for serial communication; it does not use Qt SerialPort.

## Intended Qt distribution path

The project plans to use the community LGPLv3 path for Qt/PySide6 components that are available under LGPL. PySide6 metadata also lists GPL options, and individual Qt modules or third-party components may use different licenses. Each Qt module, DLL, plugin, and embedded component in the final `.dist` directory must be reviewed; Python imports alone do not establish what is shipped.

## Windows binary release materials

The following materials were prepared for the read-only portable candidate audited on 2026-10-15:

- [Qt/PySide component, file-version, and SHA-256 inventory](licenses/windows-qt-runtime-inventory-2026-10-15.csv)
- [Current candidate review and open items](licenses/windows-portable-review-2026-10-15.md)
- [Draft third-party notices for the portable package](licenses/windows-third-party-notices.md)
- [Qt/PySide corresponding-source register and draft source offer](licenses/windows-source-offer.md)
- [Draft Qt library replacement and launch procedure](licenses/windows-replace-qt.md)
- Full LGPLv3 text: `docs/licenses/LGPL-3.0.txt`

The current Qt 6.12.0 candidate contains no Qt PDF, Qt WebEngine, or `qpdf.dll`; the historical source-match issue is removed. The remaining documents are still review materials and drafts. Exact notices for non-Qt runtimes and a modified/relinked Qt library replacement-and-run check against this ZIP remain open. Source archive URLs and checksums are listed in the source register, but the wheel build patches/provenance and a durable recipient source offer have not been verified.

## Requirements for a Windows binary release

1. Generate a dependency and license inventory from the exact build, including Qt modules, plugins, Shiboken, and other native libraries. The candidate snapshot inventory is linked above.
2. Include applicable full license texts, a Qt usage notice, and third-party attributions.
3. Provide corresponding source for the Qt/PySide libraries actually used, including applicable modifications, or establish a source-delivery method that meets the license terms.
4. Keep Qt libraries replaceable, document how to replace/relink and run the application, and verify the procedure before release.
5. Exclude unverified or unnecessary modules. If a GPL-only component is included, determine a compatible licensing path for the distribution.

This source repository does not claim that the current Windows portable candidate meets these requirements. Do not publish a Windows binary until the source offer, accurate third-party notices, and replacement/run verification are complete.

## Official references

- [Qt for Python](https://doc.qt.io/qtforpython-6/)
- [Qt's official LGPL/GPL distribution obligations](https://www.qt.io/development/open-source-lgpl-obligations)
- [Qt 6 licensing information](https://doc.qt.io/qt-6/licensing.html)
- [GNU LGPLv3](https://www.gnu.org/licenses/lgpl-3.0.html)
