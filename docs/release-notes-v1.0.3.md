# v1.0.3 — 采集界面与连续运行体验

[简体中文](#简体中文) | [English](#english)

## 简体中文

本版本改进了连续采集界面的稳定性和操作说明：

- Web 设置按测量模式显示可用控件，不再因当前模式不支持的可选控件缺失而导致界面初始化中断。
- Web 状态轮询采用单请求保护，避免串口/服务响应较慢时重叠请求和旧状态覆盖新状态。
- 桌面图表手动缩放任一坐标轴后都会退出自动跟随；提示文字与实际行为保持一致。
- 桌面和 Web 配置明确说明 NPLC、交流带宽、模拟滤波、自动零点及软件目标间隔对实测采样间隔的影响。
- Web“退出服务”按钮和确认框说明：关闭浏览器标签页不会停止采集或 localhost 服务；确认退出才会停止采集、保存实验并关闭服务。

本版本发布源码 ZIP 和 SHA-256 校验文件，不包含预编译 Windows EXE。自动化测试及 Mock 启动不代表真实 Fluke 8845A 硬件验证。

## English

This release improves acquisition UI reliability and operator guidance:

- Web settings render only controls supported by the selected measurement mode, avoiding initialization failures when optional controls are absent.
- Web state polling allows only one request at a time, preventing overlapping requests and stale state updates when the service responds slowly.
- Manually zooming either axis in the desktop chart disables live follow, matching the on-screen guidance.
- Desktop and Web settings explain how NPLC, AC bandwidth, analog filtering, autozero, and the software target interval affect the measured sample interval.
- The Web shutdown button and confirmation explain that closing a browser tab does not stop acquisition or the localhost service; confirming shutdown stops acquisition, saves the experiment, and closes the service.

This release contains the source ZIP and SHA-256 checksum, not a prebuilt Windows executable. Automated tests and Mock startup do not constitute validation on a real Fluke 8845A.
