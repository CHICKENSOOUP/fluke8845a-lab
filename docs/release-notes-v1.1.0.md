# v1.1.0 — 图表交互、历史数据与实验记录改进

[简体中文](#简体中文) | [English](#english)

## 简体中文

- 桌面版和 localhost 界面改进曲线缩放与平移：滚轮可按修饰键调整不同坐标轴，纵轴支持细跨度缩放、自动缩放和手动跨度输入。视图操作不改写原始测量值。
- 改进大体量历史 CSV 的异步导入、区间读取与绘图，避免读取文件时阻塞界面；全量统计与绘图数据保持分离。
- 完善设置提示、恢复默认设置、LOCAL / COMPUTER 控制状态、ZERO 校正说明和统计区间操作。
- 增强实验事件与 CSV 导出处理。表格软件安全导出生成副本，原始测量 CSV 保持不变。
- 仅当按序列号记录的 OutG SW 可解析且明确低于对应要求时，才限制固定 400 mA 档；固件版本未知不会按旧版本误判。量程可用性仍受仪器型号、端子和官方规格限制。
- Windows 构建显示版本和源码摘要标识，并在便携 ZIP 校验中核对身份文件、冻结 Mock 报告和 EXE 哈希。
- 正式 GitHub Release 与 GHCR Packages 包含源码 ZIP 和 SHA-256 校验文件。预编译 Windows 包暂不上传，Qt/PySide 许可交付审查仍待完成。
- 自动化测试与 Mock 六模式验证不代表真实 Fluke 8845A 硬件验证；详见[硬件验证记录](hardware-validation.md)。

## English

- Improved desktop and localhost chart navigation: wheel modifiers select chart axes, and the Y axis supports fine zoom, autoscale, and a manual span. View operations do not change raw measurements.
- Historical CSV import, range reads, and plotting run asynchronously for large files, keeping file I/O off the UI thread and separating full-run statistics from plot data.
- Improved setting guidance, reset-to-default controls, LOCAL / COMPUTER state handling, ZERO correction help, and statistics interval controls.
- Improved experiment-event and CSV export handling. Spreadsheet-safe exports are separate copies; original measurement CSV files remain unchanged.
- The fixed 400 mA range is restricted only when the serial-number-specific recorded OutG SW is parseable and explicitly below its mode requirement. Unknown firmware is not treated as an old version. Range availability remains subject to the instrument model, terminals, and official specifications.
- Windows builds display the software version and source digest. Portable ZIP validation checks the identity files, frozen Mock report, and executable hash.
- The stable GitHub Release and GHCR package contain the source ZIP and SHA-256 checksum. A prebuilt Windows package is not uploaded while Qt/PySide license delivery review remains open.
- Automated tests and six-mode Mock verification do not constitute hardware validation on a Fluke 8845A. See the [hardware validation record](hardware-validation.md).
