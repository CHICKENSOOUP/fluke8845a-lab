# Qt/PySide source delivery register

[简体中文](#简体中文) | [English](#english)

## English

This register covers the local `Fluke8845A_Essentials_6_12_7_Windows_x64.zip` candidate audited on 2026-10-15. The bundle's Qt/PySide inventory is [here](windows-qt-runtime-inventory-2026-10-15.csv). The binary package does not include Qt PDF or Qt WebEngine.

## Version-matched official source archives

| Distributed files | Version | Official archive | Size | Official SHA-256 | Audit status |
|---|---:|---|---:|---|---|
| Qt libraries and plugins | 6.12.0 | [qt-everywhere-src-6.12.0.tar.xz](https://download.qt.io/official_releases/qt/6.12/6.12.0/single/qt-everywhere-src-6.12.0.tar.xz) | 443,394,020 bytes | `98ff4f44bac6ec3e1e62ee2a4316ae0e3d15badb015d753cc0268a20db52f165` | Official URL and sidecar checksum returned successfully on 2026-10-15; patches/provenance used to build the wheel still need confirmation. |
| PySide6 and Shiboken bindings | 6.12.0 | [pyside-setup-everywhere-src-6.12.0.tar.xz](https://download.qt.io/official_releases/QtForPython/pyside6/PySide6-6.12.0-src/pyside-setup-everywhere-src-6.12.0.tar.xz) | 18,211,772 bytes | `099c1a597cf33c1b000e11cf68a0b82d216fb802bf81726efc720394f10c7751` | Official URL and sidecar checksum returned successfully on 2026-10-15; wheel build patches/provenance still need confirmation. |

Checksum sidecars: [Qt](https://download.qt.io/official_releases/qt/6.12/6.12.0/single/qt-everywhere-src-6.12.0.tar.xz.sha256) and [Qt for Python](https://download.qt.io/official_releases/QtForPython/pyside6/PySide6-6.12.0-src/pyside-setup-everywhere-src-6.12.0.tar.xz.sha256). The actual source code has not been copied into this repository or attached to a GitHub Release.

## Status and draft offer

The exact Qt/PySide versions and official source archives are identified, replacing the unresolved Qt PDF source match in the historical [2026-10-14 review](windows-portable-review-2026-10-14.md). Before using a written source offer for binary distribution, confirm that the archives contain the source and any patches corresponding to the distributed wheels, publish stable version-specific links and hashes, and preserve the source offer for the required period. The complete third-party notice set and the end-user library replacement/relaunch test are also open; see the [2026-10-15 package review](windows-portable-review-2026-10-15.md).

Draft wording, to use only after the open items above are closed:

> Qt and PySide6 libraries distributed with this application are offered under the GNU LGPL version 3. For at least three years from this release, corresponding source for the exact distributed versions, including applicable modifications, is available from the version-specific archives and checksums listed in this register. No fee is charged beyond the cost of providing the source.

This is a draft, not a completed source offer or legal opinion. Qt's [official open-source obligations](https://www.qt.io/development/open-source-lgpl-obligations) and the complete [LGPLv3 text](LGPL-3.0.txt) remain the governing references.

## 简体中文

本登记表对应 2026-10-15 审查的本地候选包 `Fluke8845A_Essentials_6_12_7_Windows_x64.zip`。组件清单见[这里](windows-qt-runtime-inventory-2026-10-15.csv)。此包不含 Qt PDF 或 Qt WebEngine。

| 随包文件 | 版本 | 官方源码包 | 大小 | 官方 SHA-256 | 核对状态 |
|---|---:|---|---:|---|---|
| Qt 库与插件 | 6.12.0 | [qt-everywhere-src-6.12.0.tar.xz](https://download.qt.io/official_releases/qt/6.12/6.12.0/single/qt-everywhere-src-6.12.0.tar.xz) | 443,394,020 字节 | `98ff4f44bac6ec3e1e62ee2a4316ae0e3d15badb015d753cc0268a20db52f165` | 2026-10-15 官方 URL 和校验和 sidecar 可访问；仍需确认构建 wheel 使用的补丁与来源。 |
| PySide6 与 Shiboken 绑定 | 6.12.0 | [pyside-setup-everywhere-src-6.12.0.tar.xz](https://download.qt.io/official_releases/QtForPython/pyside6/PySide6-6.12.0-src/pyside-setup-everywhere-src-6.12.0.tar.xz) | 18,211,772 字节 | `099c1a597cf33c1b000e11cf68a0b82d216fb802bf81726efc720394f10c7751` | 2026-10-15 官方 URL 和校验和 sidecar 可访问；仍需确认 wheel 构建补丁与来源。 |

校验和 sidecar： [Qt](https://download.qt.io/official_releases/qt/6.12/6.12.0/single/qt-everywhere-src-6.12.0.tar.xz.sha256) 和 [Qt for Python](https://download.qt.io/official_releases/QtForPython/pyside6/PySide6-6.12.0-src/pyside-setup-everywhere-src-6.12.0.tar.xz.sha256)。源码尚未复制到本仓库或上传到 GitHub Release。

对应源码版本和官方归档已确认，替代了[2026-10-14 历史审查](windows-portable-review-2026-10-14.md)中未解决的 Qt PDF 源码匹配项。二进制发布前，还需确认归档确实包含与随包 wheel 对应的源码及补丁、发布版本固定的下载链接和校验值，并在要求期限内持续提供源码。完整第三方声明和用户替换/重启 Qt 库测试也未完成，详见[2026-10-15 候选包审查](windows-portable-review-2026-10-15.md)。

上方英文书面源码提供声明只是草案，只有关闭上述事项后才能用于二进制发布；它不构成完整法律意见。请同时参阅 Qt 的[官方开源发行义务](https://www.qt.io/development/open-source-lgpl-obligations)和仓库中的[LGPLv3 完整文本](LGPL-3.0.txt)。
