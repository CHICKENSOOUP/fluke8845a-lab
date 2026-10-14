# Qt/PySide source delivery register (draft)

[简体中文](#简体中文) | [English](#english)

## English

This register is for the 2026-10-14 Windows portable build review. It is not yet a complete source offer for distribution because the Qt PDF binary in the candidate has not been matched to an exact stable source revision.

## Source packages identified

| Shipped component | Version indicated by candidate | Official source location | Status |
|---|---:|---|---|
| Qt framework modules in the candidate | 6.12.0 | [Qt 6.12.0 source packages](https://download.qt.io/official_releases/qt/6.12/6.12.0/single/) (`qt-everywhere-src-6.12.0.zip` for Windows or `qt-everywhere-src-6.12.0.tar.xz`) | Version match identified; archive checksum and any build patches still need to be recorded for the actual release build. |
| PySide6/Shiboken bindings | 6.12.0 | [PySide6 6.12.0 source package](https://download.qt.io/official_releases/QtForPython/pyside6/PySide6-6.12.0-src/) (`pyside-setup-everywhere-src-6.12.0.zip` or `.tar.xz`) | Version match identified; confirm the build's source patches and exact wheel provenance. |
| Qt PDF / PDFium component | 6.140.0.0 file version | [QtWebEngine source repository](https://code.qt.io/cgit/qt/qtwebengine.git/) | **Open:** the public repository ref found was `v6.140.0-rc`, not a stable source matching the candidate. Do not represent the RC source as corresponding source for the stable binary. |

The PDF candidate includes code from PDFium and other third-party projects. Its corresponding source set and notices must include the matching QtWebEngine/PDFium source and the license/attribution material for the components actually shipped. The general Qt 6.12 source archive alone does not establish this coverage.

## Draft written-offer wording

Use this wording only after the source set is complete, archived, and the release ZIP has passed the replacement procedure:

> The Qt and PySide6 libraries distributed with this application are available under the GNU LGPL version 3. For at least three years from this release, the corresponding source code for the exact distributed versions, including any modifications made for the distributed libraries, can be obtained at the source links listed in `docs/licenses/windows-source-offer.md`, or by contacting the project maintainer through the GitHub repository. No fee is charged beyond the cost of providing the source. The source archive checksums and build patches are listed with the release.

Before using this wording, replace the links with stable downloadable release assets, record SHA-256 hashes, record all patches, and ensure the source package remains available for the full stated period. The exact Qt PDF source is currently missing, so this offer must not yet be presented as complete.

## Source package integrity record

No large upstream source archive has been copied into this repository. For the final portable release, retain the exact Qt, QtWebEngine/PDF, and PySide source archives separately from Git source history and publish stable download links plus SHA-256 checksums. Do not use an unrelated later/earlier source version as a substitute.

## References

- [Qt source downloads](https://www.qt.io/development/offline-installers)
- [Qt 6.12.0 source archive index](https://download.qt.io/official_releases/qt/6.12/6.12.0/single/)
- [PySide6 6.12.0 source archive index](https://download.qt.io/official_releases/QtForPython/pyside6/PySide6-6.12.0-src/)
- [Qt PDF source/build notes](https://wiki.qt.io/QtPDF_Build_Instructions)
- [QtWebEngine repository](https://code.qt.io/cgit/qt/qtwebengine.git/)

## 简体中文

### Qt/PySide 源码交付登记表（草稿）

本登记表针对 2026-10-14 Windows 便携构建审查。候选包中的 Qt PDF 二进制尚未匹配到精确稳定版源码，因此本登记表还不是完整的源码交付方案。

| 随包组件 | 候选版本 | 官方源码位置 | 状态 |
|---|---:|---|---|
| Qt 框架模块 | 6.12.0 | [Qt 6.12.0 源码包](https://download.qt.io/official_releases/qt/6.12/6.12.0/single/) | 已识别版本；最终构建仍须记录校验和及构建补丁。 |
| PySide6/Shiboken 绑定 | 6.12.0 | [PySide6 6.12.0 源码包](https://download.qt.io/official_releases/QtForPython/pyside6/PySide6-6.12.0-src/) | 已识别版本；须确认补丁及 wheel 来源。 |
| Qt PDF / PDFium | 文件版本 6.140.0.0 | [QtWebEngine 源码仓库](https://code.qt.io/cgit/qt/qtwebengine.git/) | **未解决：**审查时找到的公开 ref 只有 `v6.140.0-rc`，不能把它宣称为稳定版二进制的对应源码。 |

Qt PDF 的对应源码集还须覆盖匹配的 QtWebEngine/PDFium 源码及随包第三方组件的许可证/归属材料。Qt 6.12 通用源码包本身不能证明该覆盖完整。

文档中的书面要约措辞只有在源码完整归档、发布 ZIP 完成替换验证后才能使用。届时需提供稳定下载地址、SHA-256、所有补丁，并确保源码在承诺期限内持续可取。当前 Qt PDF 源码缺失，故不得将要约表述为已完备。

本仓库没有复制大型上游源码包。最终便携版需将实际 Qt、QtWebEngine/PDF 和 PySide 源码包保存在 Git 之外，发布稳定链接及校验值；不能以不匹配的其他版本替代。
