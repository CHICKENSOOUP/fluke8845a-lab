# Third-party notices for the Windows portable candidate

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
