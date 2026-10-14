# v1.0.0 — source release

[简体中文](#简体中文) | [English](#english)

## 简体中文

首个公开版本提供 Python 源码、测试、文档、应用图稿和模拟测量示例。

### 包含内容

- 六种测量模式：DCV、ACV、DCI、ACI、二线电阻和四线电阻
- 使用项目自有串口驱动的 RS-232/SCPI 控制
- 连续 CSV 记录、事件标记、实时曲线和统计
- Mock 模式及明确标记为模拟数据的示例

### 验证范围

项目记录中包含开发版本通过 198 项测试及六种模式的冻结程序 Mock 检查结果。本次发布准备没有重新运行这些检查。当前源码快照尚未完成全部测量模式的真机验证或准确度测试。

### 打包

本次仅发布源码，没有附带 Windows 可执行程序，因为 Qt/PySide 二进制组件清单、对应源码交付和替换/重新链接说明尚未完备。详见 [Qt 许可说明](qt-licensing-and-third-party-notices.md)。

### 隐私与参考资料

归档中仅含模拟示例数据，不包含仪器日志、真实测量数据、工作站标识或 Fluke 手册 PDF。

## English

This first public release contains the Python source, tests, documentation, application artwork, and simulated measurement examples.

## Included

- Six measurement modes: DCV, ACV, DCI, ACI, 2-wire resistance, and 4-wire resistance
- RS-232/SCPI control with the project's own serial driver
- Continuous CSV recording, event markers, live plotting, and statistics
- Mock mode and example data explicitly marked as simulated

## Verification scope

The project notes record 198 passing tests and six-mode frozen-app Mock checks from the development build. These checks were not rerun for this release preparation. The exact current source snapshot has not completed hardware validation across all measurement modes or accuracy testing.

## Packaging

This is a source-only release. It does not attach a Windows executable because the exact Qt/PySide binary component inventory, corresponding source delivery, and replace/relink instructions are not yet ready. See [Qt licensing notes](qt-licensing-and-third-party-notices.md).

## Privacy and reference materials

The archive contains simulated examples only. Instrument logs, raw hardware measurements, workstation identifiers, and Fluke manual PDFs are not part of this release.
