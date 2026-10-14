# Qt 与第三方许可说明

本仓库的首个 GitHub 源码版不包含 Qt/PySide6 二进制运行库。应用使用 PySide6 调用 Qt 构建 GUI，使用 pyserial 和项目自身的 SCPI 驱动进行串口通信；未使用 Qt SerialPort。

## 计划采用的 Qt 发行路径

项目计划对 LGPL 许可的 Qt/PySide6 组件采用社区版 LGPLv3 发行路径。PySide6 的发行元数据也列有 GPL 许可选项；Qt 个别模块和第三方组件可能有不同许可证。必须针对最终 `.dist` 目录里的每个 Qt 模块、DLL、插件和嵌入组件逐项核验，不能只根据 Python 源码中的 import 判断。

## Windows 二进制发布材料

已为 2026-10-14 的只读候选包准备以下材料：

- [Qt/PySide 组件、文件版本和 SHA-256 清单](licenses/windows-qt-runtime-inventory-2026-10-14.csv)
- [候选包许可证审查与未关闭事项](licenses/windows-portable-review-2026-10-14.md)
- [便携包第三方声明草稿](licenses/windows-third-party-notices.md)
- [Qt/PySide 对应源码登记表和源码提供声明草稿](licenses/windows-source-offer.md)
- [Qt 动态库替换和运行步骤草稿](licenses/windows-replace-qt.md)
- LGPLv3 完整文本：`docs/licenses/LGPL-3.0.txt`

目前这些是审查材料/草稿，不代表候选包已达到可发布状态。当前候选包包含版本为 6.140.0.0 的 Qt PDF 组件，但尚未找到与其匹配的稳定版源码；替换库流程尚未在最终 ZIP 上验证，全部传递依赖的许可证声明也未齐全。

## Windows 二进制发布要求

二进制版发布时应仿照可审计的第三方许可清单流程：

1. 从每次确切的构建产物生成依赖与许可证清单，包含 Qt 模块、插件、Shiboken 和其他本机库。当前候选的快照清单见上方链接。
2. 随包提供适用的完整许可证文本、Qt 使用声明及第三方归属声明。
3. 提供本次实际使用的 Qt/PySide 库的对应源码及适用修改，或建立符合许可证条款的源码交付方式。
4. 保持 Qt 库可替换，并写明替换/重新链接后如何运行程序；在发布前实际验证流程。
5. 排除未经确认或应用不需要的模块；若包含 GPL-only 组件，先确定兼容的整体授权路径。

本源码仓库不声称当前 Windows 便携包已满足上述条件。对应源码、准确的第三方声明和替换运行验证全部闭环前，不发布 Windows 二进制包。

## 官方资料

- [Qt for Python](https://doc.qt.io/qtforpython-6/)
- [Qt 官方 LGPL/GPL 发行义务](https://www.qt.io/development/open-source-lgpl-obligations)
- [Qt 6 许可信息](https://doc.qt.io/qt-6/licensing.html)
- [GNU LGPLv3](https://www.gnu.org/licenses/lgpl-3.0.html)
