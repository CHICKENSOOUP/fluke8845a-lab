# v1.0.1 — Windows source-package startup

[简体中文](#简体中文) | [English](#english)

## 简体中文

本补丁版本修复了 Windows 源码 ZIP 的首次启动流程。之前的 `run_windows.bat` 和 `run_web.bat` 要求用户先手动创建虚拟环境并安装依赖，双击源码包中的启动器会直接失败。

现在，解压源码 ZIP 后可以双击 `run_windows.bat` 启动桌面界面，或双击 `run_web.bat` 启动 localhost 网页界面。首次启动会在本地创建 `.venv` 并安装运行依赖，之后复用该环境。需要 Windows、64 位 Python 3.11 或更高版本，以及首次安装时的网络连接。

本版本仍是源码发布，不包含预编译 EXE；Mock 启动流程不代表真机验证。

## English

This patch fixes the first-run flow for the Windows source ZIP. Previously, `run_windows.bat` and `run_web.bat` required users to create a virtual environment and install dependencies manually, so double-clicking either launcher failed on a fresh extraction.

After extracting the source ZIP, double-click `run_windows.bat` for the desktop app or `run_web.bat` for the localhost browser interface. On first launch, the script creates a local `.venv` and installs runtime dependencies; later launches reuse it. Windows, 64-bit Python 3.11 or later, and an internet connection for the initial installation are required.

This remains a source release and does not include a prebuilt EXE. Mock startup does not constitute hardware validation.
