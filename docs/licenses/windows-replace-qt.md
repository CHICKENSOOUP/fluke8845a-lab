# Replacing Qt libraries in the Windows portable folder (draft)

The build under review is a Nuitka standalone directory. Keep the whole extracted folder together; do not move or redistribute only `Fluke8845A.exe`. This guide is a draft and has not yet been validated against a final portable ZIP.

## Recipient procedure

1. Extract the complete Windows ZIP to a writable folder, for example `C:\Apps\Fluke8845A`.
2. Keep a backup copy of the extracted folder.
3. Obtain a compatible x64 Qt/PySide6 library build and its corresponding source. Use the exact Qt/PySide versions listed in that release's inventory. A different major version, architecture, compiler runtime, or incompatible plugin set may not load.
4. Replace the selected Qt shared libraries beside `Fluke8845A.exe` (for example `qt6core.dll`, `qt6gui.dll`, and other `qt6*.dll` files) and the corresponding Qt platform/plugin DLLs under `PySide6\qt-plugins\`. Keep PySide binding/runtime DLLs and Qt libraries from a compatible build together; do not mix arbitrary versions.
5. Start `Fluke8845A.exe` from the extracted folder. If it fails to start, restore the backup and consult the build-specific dependency inventory and source/build instructions.

## Release maintainer verification (required before publication)

- Produce a clean portable ZIP from the final source snapshot and record its SHA-256.
- Make a copy of that ZIP, replace one LGPL Qt library with a modified/rebuilt compatible library, then launch the application from the copied folder.
- Confirm the program loads the replacement rather than another Qt copy on `PATH`; record the exact changed DLL, replacement build details, and observed result.
- Repeat for plugin loading if the plugin architecture or plugin binary was modified.
- Confirm the EXE does not enforce a signature/hash check that prevents running with replaced libraries.
- If users must relink an application object or provide installation information for their device, document the build inputs and commands needed to do so. The release has not yet demonstrated that this step is unnecessary for the chosen distribution format.
- Run the ordinary release smoke checks after replacement and confirm the untouched original ZIP still works. Record these results with the release.

## Current limitation

The instructions above do not prove that this specific Nuitka bundle meets LGPLv3 replacement/relink requirements. The `Fluke8845A_DCVGui_20261014` candidate has not been subjected to a replacement run, and its Qt PDF source match is unresolved. Do not publish this guide as completed compliance evidence until the maintainer verification succeeds on the final release artifact.

## References

- [Qt's LGPL obligations](https://www.qt.io/development/open-source-lgpl-obligations)
- [Qt for Python standalone deployment](https://doc.qt.io/qtforpython-6/deployment/deployment-pyside6-deploy.html)
