# Third-party notices

This source repository does not bundle third-party runtime binaries. Dependencies are installed separately as declared in `pyproject.toml` and `requirements.txt`; each dependency remains under its own license.

## Qt for Python (PySide6) and Qt

The application uses PySide6 (Qt for Python) for its graphical interface. The project intends to use the community LGPLv3 licensing path for the LGPL-eligible Qt/PySide6 components. PySide6/Qt also offers GPL and commercial licensing options, and individual modules or bundled third-party components can have different terms. The exact license must be checked against the components in each binary build.

When a Windows binary is published, its release package must include the applicable license texts and notices, exact component inventory, a source delivery method for the Qt/PySide libraries, and instructions that let recipients replace/relink those libraries and run the application. This source-only release does not claim that a binary distribution is LGPL-compliant.

- [Qt for Python licensing](https://doc.qt.io/qtforpython-6/)
- [Qt open-source LGPL/GPL obligations](https://www.qt.io/development/open-source-lgpl-obligations)
- [GNU LGPLv3 text](https://www.gnu.org/licenses/lgpl-3.0.html)

## Other dependencies

The project directly depends on NumPy, PySide6, pyserial, and pyqtgraph. Refer to the license metadata and license files distributed by the exact versions installed for a build. A complete binary-build inventory must include transitive dependencies and native libraries, not only these direct Python packages.
