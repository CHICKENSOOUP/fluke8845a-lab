# v1.0.2 — Versioned source release and cleaner test runs

[简体中文](#简体中文) | [English](#english)

## 简体中文

这是一个源码维护版本，统一了应用版本、Python 项目元数据、使用指南和发布包中的版本说明。正式发布标签与源码包、GHCR Packages 使用相同的 `v1.0.2` 版本号。

本机的有界 pytest 运行器现在默认把临时目录和日志写入系统临时目录下的 `Fluke8845A_Test_Artifacts`，不再向项目检出目录堆积测试输出。旧测试产物已移出仓库目录并保存在单独归档中。

本版本发布源码 ZIP 和 SHA-256 校验文件，不包含预编译 Windows EXE。软件测试和 Mock 启动不代表真实 Fluke 8845A 硬件验证。

## English

This source maintenance release aligns the application version, Python project metadata, user guide, and packaged release notes. The stable release, source archive, and GHCR Packages use the same `v1.0.2` version.

The bounded local pytest runner now writes temporary files and logs under `Fluke8845A_Test_Artifacts` in the operating system's temporary directory instead of accumulating test output in the source checkout. Existing test artifacts were moved to a separate archive outside the repository.

This release contains the source ZIP and its SHA-256 checksum, not a prebuilt Windows executable. Software tests and Mock startup do not constitute validation on a real Fluke 8845A.
