# Third-party notices for the Windows portable candidate

[简体中文](#简体中文) | [English](#english)

## English

Build snapshot: `Fluke8845A_DCVGui_20261014/Fluke8845A.dist`, reviewed 2026-10-14. This notice draft must be included in the portable ZIP together with the project [MIT license](../../LICENSE), the [GNU LGPL v3 text](LGPL-3.0.txt), and the component [inventory](windows-qt-runtime-inventory-2026-10-14.csv). The final ZIP must be rescanned because this list applies only to the reviewed snapshot.

## Application

Fluke 8845A Lab project-authored code and artwork are licensed under MIT. The MIT license applies to those project materials; it does not replace the licenses of bundled dependencies.

## Qt and Qt for Python

The application uses PySide6 (Qt for Python) and dynamically loads Qt libraries and plugins. This project intends to use the LGPLv3 option for components that offer it. The Qt for Python distributions also identify GPLv2/GPLv3 alternatives; no commercial Qt license is asserted by this notice. Retain the LGPLv3 text with the package and display a clear Qt/PySide usage notice.

This candidate contains Qt 6.12.0 modules and plugins listed in the inventory, plus Qt PDF file version 6.140.0.0. The exact module/source relationship and all licenses must be matched to the final build before distribution.

Qt provides version-specific module and embedded third-party license information here:

- [Qt 6.12 licensing](https://doc.qt.io/qt-6/licensing.html)
- [Third-party code in Qt 6.12](https://doc.qt.io/qt-6/licenses-used-in-qt.html)
- [Qt for Python third-party licenses](https://doc.qt.io/qtforpython-6/licenses.html)
- [Qt PDF licensing and notices](https://doc.qt.io/qt-6/qtpdf-licensing.html)
- [Qt open-source LGPL/GPL obligations](https://www.qt.io/development/open-source-lgpl-obligations)

Qt PDF includes PDFium and third-party components with separate terms. The Qt PDF notice identifies Abseil (Apache-2.0), FreeType (FTL), PDFium and Chromium components (BSD-family), fast_float (MIT), ICU (MIT), libjpeg-turbo (IJG/BSD-3-Clause/Zlib), libpng, and zlib. Confirm the precise notices in the source matching the shipped Qt PDF build; do not treat this summary as a substitute for those texts.

## Other bundled runtimes

The frozen folder also contains Python 3.12 runtime files, NumPy/OpenBLAS native libraries, OpenSSL DLLs, and Microsoft Visual C++ runtime DLLs. The candidate's lock file lists NumPy 2.5.4, pyqtgraph 0.14.0, and pyserial 3.5, but a lock file alone does not identify every copied file or establish the applicable notice set. Before release, extract the licenses/NOTICE files from the exact wheels and runtime installers used for this build, and add their texts/attributions here or in a `licenses/` subfolder. Preserve the pyqtgraph color-map attribution files already copied into the bundle.

## Release checklist

- [ ] Include this notice, `LICENSE`, and `docs/licenses/LGPL-3.0.txt` in the actual ZIP.
- [ ] Attach the exact Qt/PySide source archives and their third-party license files, or provide the approved durable source offer.
- [ ] Add notices for the exact CPython, NumPy/OpenBLAS, OpenSSL, Microsoft runtime, and any other native dependencies present in the ZIP.
- [ ] Reconcile this notice against the final ZIP contents and its generated SBOM; remove statements for components no longer shipped.

## 简体中文

### Windows 便携候选包第三方声明（草稿）

审查构建快照为 `Fluke8845A_DCVGui_20261014/Fluke8845A.dist`，日期为 2026-10-14。本声明草稿应与项目 [MIT 许可证](../../LICENSE)、[GNU LGPL v3 文本](LGPL-3.0.txt)及[组件清单](windows-qt-runtime-inventory-2026-10-14.csv)一并放入便携 ZIP。最终 ZIP 需重新扫描；本文只适用于所审查的快照。

项目自有代码和图稿采用 MIT 许可证，该许可不替代捆绑依赖的许可证。应用使用 PySide6 和 Qt 动态库/插件；项目计划对可选用 LGPLv3 的组件采用该路径。随包保留 LGPLv3 文本并提供清晰的 Qt/PySide 使用声明。候选包含清单所列 Qt 6.12.0 组件，以及文件版本为 6.140.0.0 的 Qt PDF / PDFium 组件；最终发版前必须核对确切模块、源码对应关系和许可证。Qt PDF 含有具备独立条款的 PDFium 及第三方组件，摘要不能替代实际源码中的声明文本。

候选冻结目录还包含 Python 3.12、NumPy/OpenBLAS、OpenSSL 和 Microsoft Visual C++ 运行库。锁文件不能完整识别实际复制的文件，也不足以证明声明齐全。发布前应从此次构建使用的精确 wheel 和运行时安装包中提取许可证/NOTICE，并核对图谱色表归属文件。

发布前检查：将声明、MIT 与 LGPL 文本纳入真实 ZIP；提供精确 Qt/PySide 对应源码或合规源码交付方式；补全 CPython、NumPy/OpenBLAS、OpenSSL、Microsoft 运行库等实际组件的声明；按最终 ZIP 内容和 SBOM 重新核验并移除未随包组件的陈述。
