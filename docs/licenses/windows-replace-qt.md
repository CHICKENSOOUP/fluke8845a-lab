# Replacing Qt libraries in the Windows portable folder (draft)

[简体中文](#简体中文) | [English](#english)

## English

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

The instructions above do not prove that this specific Nuitka bundle meets LGPLv3 replacement/relink requirements. The 2026-10-15 candidate no longer contains the Qt PDF plugin, but it has not been subjected to a replacement run. Do not publish this guide as completed compliance evidence until the maintainer verification succeeds on the final release artifact.

## References

- [Qt's LGPL obligations](https://www.qt.io/development/open-source-lgpl-obligations)
- [Qt for Python standalone deployment](https://doc.qt.io/qtforpython-6/deployment/deployment-pyside6-deploy.html)

## 简体中文

### Windows 便携目录中替换 Qt 库（草稿）

审查构建为 Nuitka standalone 目录。需保持整个解压目录完整；不要单独移动或分发 `Fluke8845A.exe`。本指南尚未在最终便携 ZIP 上验证。

#### 接收者操作步骤

1. 将完整 Windows ZIP 解压到可写目录，例如 `C:\Apps\Fluke8845A`。
2. 备份解压后的目录。
3. 获取兼容的 x64 Qt/PySide6 库构建及其对应源码，并使用该版本清单中列明的精确版本。不同主版本、架构、编译器运行库或不兼容插件可能无法加载。
4. 替换 `Fluke8845A.exe` 旁边选定的 Qt 共享库（例如 `qt6core.dll`、`qt6gui.dll` 及其他 `qt6*.dll`）及 `PySide6\qt-plugins\` 下对应插件。绑定运行库和 Qt 库须来自兼容构建，不要混搭随意版本。
5. 从解压目录启动程序。若启动失败，恢复备份并参考该构建的依赖清单和源码/构建说明。

#### 发布前维护者验证（必须完成）

对最终源码快照重新打包并记录 ZIP 的 SHA-256；复制 ZIP 后替换一个 LGPL Qt 库，再从副本启动应用；确认应用加载的是替换库，而不是 PATH 中的其他 Qt 副本；记录 DLL、替换构建信息和结果。若修改插件，也应验证插件加载。确认 EXE 没有阻止替换库运行的签名/哈希检查，并记录所需安装信息或重新链接步骤。替换后运行常规冒烟检查，同时确认原始 ZIP 仍可工作。

以上步骤并不能证明当前 Nuitka 包满足 LGPLv3 的替换/重新链接要求。2026-10-15 候选包已移除 Qt PDF 插件，但尚未做替换运行验证。只有在最终产物上完成验证后，才能将本指南作为合规证据发布。
