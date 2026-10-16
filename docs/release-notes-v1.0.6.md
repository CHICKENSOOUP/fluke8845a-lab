# v1.0.6 — 模拟场景连接前选择

[简体中文](#简体中文) | [English](#english)

## 简体中文

- Web 界面现在可在连接模拟仪器前选择模拟场景；连接期间及连接后会锁定场景选择，需断开后再重新连接才能更换场景。
- 修复模拟场景选择控件此前仅在模拟器已连接后显示、无法预先选择的问题。
- 发布内容为源码 ZIP 和 SHA-256 校验文件；不包含预编译 Windows EXE。
- 自动化测试和 Mock 启动不代表真实 Fluke 8845A 硬件验证。

## English

- The Web interface now lets users select a Mock scenario before connecting the simulated instrument. Scenario selection is locked while connected and can be changed after disconnecting.
- Fixes the scenario selector previously appearing only after the Mock instrument was already connected.
- Assets are the source ZIP and SHA-256 checksum; no prebuilt Windows executable is included.
- Automated tests and Mock startup do not constitute validation on a real Fluke 8845A.
