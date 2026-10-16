# v1.0.4 — 界面反馈与窄屏适配

[简体中文](#简体中文) | [English](#english)

## 简体中文

本版本改进界面状态反馈、图表操作说明和窄屏布局：

- Web 平均值、样本标准差、最小值和最大值卡片分别显示参与统计的有效样本数。
- 收到新的真实仪器状态回读时，如果测量模式、固定量程或输入端子发生变化，Web 接线确认会清除，要求操作者重新确认。
- Web 和桌面界面区分连续采集与单次测量：单次测量仍可停止，但暂停控件会禁用并说明原因。
- 桌面自动跟随提示与图表实际行为一致；Web 将悬停读数与常驻手势说明分开显示。
- Web 顶栏在 351 px 等窄视口下收缩布局，避免横向溢出。

本次只调整界面状态与显示，不改变 SCPI 命令或测量参数。接线确认只会在软件收到更新状态回读后因状态变化而清除；软件不会在仪表空闲时持续轮询前面板变化。

本版本发布源码 ZIP 和 SHA-256 校验文件，不包含预编译 Windows EXE。自动化测试及 Mock 启动不代表真实 Fluke 8845A 硬件验证。

## English

This release improves interface feedback, chart guidance, and narrow-screen layout:

- The Web mean, sample standard deviation, minimum, and maximum cards each show the number of valid samples used in the statistic.
- When a refreshed real-instrument state readback reports a changed measurement mode, fixed range, or input terminal, the Web wiring confirmation is cleared so the operator must confirm again.
- Desktop and Web distinguish continuous acquisition from a single measurement. A single measurement remains stoppable, while Pause is disabled with an explanation.
- The desktop live-follow hint matches chart behavior; the Web keeps hover readout separate from persistent gesture guidance.
- The Web top bar contracts at narrow viewports such as 351 px to prevent horizontal overflow.

This release changes interface state and presentation only; it does not change SCPI commands or measurement parameters. Wiring confirmation is cleared only when the application receives a refreshed state readback showing a change; the application does not continuously poll front-panel changes while idle.

The release contains the source ZIP and SHA-256 checksum, not a prebuilt Windows executable. Automated tests and Mock startup do not constitute validation on a real Fluke 8845A.
