# Windows runtime license source files

[简体中文](#简体中文) | [English](#english)

## English

These source files are copied into the Windows build by `scripts/collect_windows_runtime_licenses.py`:

- `pyserial-3.5-LICENSE.txt` is the exact BSD-3-Clause notice from the upstream [pySerial v3.5 tag](https://github.com/pyserial/pyserial/blob/v3.5/LICENSE.txt). The collector refuses to use it for a different installed pySerial version.
- `Apache-2.0.txt` is the Apache License 2.0 text from the Apache Software Foundation, used for the bundled OpenSSL 3.x runtime. OpenSSL's [official license page](https://www.openssl.org/source/license.html) states that OpenSSL 3.0 and later use Apache 2.0.
- CPython, NumPy/OpenBLAS, pyqtgraph, and Nuitka runtime license materials are copied from the exact build runtime or its locked package metadata. Their target paths and SHA-256 digests are recorded in `windows-runtime-license-manifest.json` inside a Windows build.

The manifest does not claim that all distribution conditions are satisfied. Qt/PySide component attribution and Microsoft Visual C++ redistribution eligibility remain separate review items.

## 简体中文

`scripts/collect_windows_runtime_licenses.py` 会将以下源码许可材料复制进 Windows 构建包：

- `pyserial-3.5-LICENSE.txt` 来自上游 [pySerial v3.5 标签](https://github.com/pyserial/pyserial/blob/v3.5/LICENSE.txt)，为 BSD-3-Clause 声明。若安装的 pySerial 不是 3.5，收集脚本会拒绝使用该声明。
- `Apache-2.0.txt` 是 Apache 软件基金会发布的 Apache License 2.0 正文，用于随包的 OpenSSL 3.x 运行库。OpenSSL[官方许可页面](https://www.openssl.org/source/license.html)说明 OpenSSL 3.0 及之后版本使用 Apache 2.0。
- CPython、NumPy/OpenBLAS、pyqtgraph 和 Nuitka 运行库的许可材料从确切的构建运行时或锁定包元数据复制；Windows 包中的路径和 SHA-256 会记录在 `windows-runtime-license-manifest.json`。

该清单不代表已满足全部发行条件。Qt/PySide 组件归属声明和 Microsoft Visual C++ 再分发资格仍需分别审查。
