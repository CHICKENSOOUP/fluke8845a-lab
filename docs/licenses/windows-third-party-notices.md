# Windows portable package third-party notice review (draft)

[简体中文](#简体中文) | [English](#english)

## English

This draft describes the local `Fluke8845A_Essentials_6_12_7_Windows_x64.zip` candidate audited on 2026-10-15. It is not approval to publish the binary. See the [snapshot review](windows-portable-review-2026-10-15.md) and [Qt/PySide inventory](windows-qt-runtime-inventory-2026-10-15.csv).

### Project-authored material

Fluke 8845A Lab code and artwork are licensed under MIT; see the package's `LICENSE` file. This does not license third-party components.

### Qt and Qt for Python

The candidate dynamically loads Qt 6.12.0 and PySide6/Shiboken 6.12.0 libraries. The project intends to use the LGPLv3 option where offered. The package includes `docs/licenses/LGPL-3.0.txt` and a Qt use notice. It has no Qt PDF, Qt WebEngine, or `qpdf.dll` component. Qt's version-specific module and third-party license pages are linked below; the actual notices must be reconciled with the exact plugins and libraries in the inventory.

### Other bundled runtime families

The frozen package also contains CPython 3.12.10, NumPy 2.5.4 and its OpenBLAS DLL, OpenSSL 3.0.16 DLLs, Nuitka 4.1.1 runtime code, pyqtgraph 0.14.0, pySerial 3.5, Shiboken, and Microsoft Visual C++ runtime DLLs. The build copies the CPython license, NumPy's full wheel license tree (including OpenBLAS/LAPACK/GCC notices), pyqtgraph and CET color-map notices, the pinned pySerial license, OpenSSL's Apache 2.0 license, and Nuitka's license/exception/notice files. The generated `docs/licenses/windows-runtime-license-manifest.json` records all 25 copied files and their SHA-256 hashes. This is evidence for the copied texts, not yet a complete binary redistribution review.

### Open items

- Verify the complete mapping of every bundled native component; Microsoft Visual C++ DLL provenance and applicable redistribution terms remain open.
- Reconcile Qt/PySide modules and embedded third-party code against the exact bundle.
- Publish a version-specific Qt/PySide corresponding-source offer after confirming wheel patches and provenance.
- Test that recipients can replace/relink the Qt libraries and start this exact portable package.
- Regenerate and review the inventory for each candidate build.

Until these items are closed, this candidate is not attached to a public Release. The current GitHub workflow publishes the source ZIP only.

### Official references

- [Qt 6.12 licensing](https://doc.qt.io/qt-6/licensing.html)
- [Third-party code used in Qt 6.12](https://doc.qt.io/qt-6/licenses-used-in-qt.html)
- [Qt Test licensing](https://doc.qt.io/qt-6/qttest-index.html)
- [Qt for Python licensing](https://doc.qt.io/qtforpython-6/)
- [Qt open-source LGPL/GPL obligations](https://www.qt.io/development/open-source-lgpl-obligations)
- [Python 3.12 license](https://docs.python.org/3.12/license.html)
- [NumPy license](https://numpy.org/doc/stable/license.html)
- [OpenSSL license](https://www.openssl.org/source/license.html)
- [pySerial v3.5 license](https://github.com/pyserial/pyserial/blob/v3.5/LICENSE.txt)
- [Nuitka runtime exception](https://github.com/Nuitka/Nuitka/blob/develop/LICENSE-RUNTIME.txt)
- [Microsoft Visual C++ redistribution terms](https://learn.microsoft.com/en-us/cpp/windows/redistributing-visual-cpp-files)

## 简体中文

本草案描述 2026-10-15 审查的本地候选包 `Fluke8845A_Essentials_6_12_7_Windows_x64.zip`，不代表批准公开发布。详见[快照审查](windows-portable-review-2026-10-15.md)和[Qt/PySide 组件清单](windows-qt-runtime-inventory-2026-10-15.csv)。

### 项目自有材料

Fluke 8845A Lab 自有代码和图稿采用 MIT 许可证，见包内 `LICENSE`。MIT 不覆盖第三方组件。

### Qt 与 Qt for Python

候选包动态加载 Qt 6.12.0 和 PySide6/Shiboken 6.12.0 库。项目拟对可使用 LGPL 的组件采用 LGPLv3 路径。包内含有 `docs/licenses/LGPL-3.0.txt` 和 Qt 使用声明；不含 Qt PDF、Qt WebEngine 或 `qpdf.dll`。Qt 各版本模块与第三方许可证页面见下方链接；最终声明仍需按清单中的实际库与插件逐项核对。

### 其他随包运行时

冻结包还包含 CPython 3.12.10、NumPy 2.5.4 及其 OpenBLAS DLL、OpenSSL 3.0.16 DLL、Nuitka 4.1.1 运行时代码、pyqtgraph 0.14.0、pySerial 3.5、Shiboken 和 Microsoft Visual C++ 运行库。构建过程复制了 CPython 许可、NumPy wheel 完整许可树（含 OpenBLAS/LAPACK/GCC 声明）、pyqtgraph 与 CET 色表归属说明、固定版本的 pySerial 许可、OpenSSL Apache 2.0 许可，以及 Nuitka 运行库许可/例外/声明文件。生成的 `docs/licenses/windows-runtime-license-manifest.json` 记录这 25 份文件及其 SHA-256。这能证明包内许可正文与清单匹配，但还不能证明全部二进制再分发条件均已核验。

### 尚未完成

- 核对每个本机运行库与许可材料的完整映射；Microsoft Visual C++ DLL 的来源和适用再分发条款仍未关闭。
- 按确切包内容核对 Qt/PySide 模块和嵌入式第三方代码。
- 核实 wheel 的补丁和来源后，提供版本固定的 Qt/PySide 对应源码要约。
- 实测接收者可替换/重新链接 Qt 库并启动此便携包。
- 对每个候选构建重新生成并审查组件清单。

这些事项完成之前，此候选包不会附加到公开 Release。当前 GitHub 工作流只发布源码 ZIP。

### 官方参考

- [Qt 6.12 许可](https://doc.qt.io/qt-6/licensing.html)
- [Qt 6.12 第三方代码](https://doc.qt.io/qt-6/licenses-used-in-qt.html)
- [Qt Test 许可](https://doc.qt.io/qt-6/qttest-index.html)
- [Qt for Python 许可](https://doc.qt.io/qtforpython-6/)
- [Qt LGPL/GPL 开源发行义务](https://www.qt.io/development/open-source-lgpl-obligations)
- [Python 3.12 许可](https://docs.python.org/3.12/license.html)
- [NumPy 许可](https://numpy.org/doc/stable/license.html)
- [OpenSSL 许可](https://www.openssl.org/source/license.html)
- [pySerial v3.5 许可](https://github.com/pyserial/pyserial/blob/v3.5/LICENSE.txt)
- [Nuitka 运行库例外](https://github.com/Nuitka/Nuitka/blob/develop/LICENSE-RUNTIME.txt)
- [Microsoft Visual C++ 再分发条款](https://learn.microsoft.com/en-us/cpp/windows/redistributing-visual-cpp-files)
