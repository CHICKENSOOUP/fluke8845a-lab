# v1.0.7 — 测量 CSV 安全副本与空间预检

[简体中文](#简体中文) | [English](#english)

## 简体中文

- 桌面和 localhost 界面可导出 `measurements.csv` 的表格软件安全副本。只处理 `raw_reading` 和 `error` 文本列；SI 数值列不变，原始文件不会修改。采集期间不可导出。
- 采集预检显示保存盘当前可用空间和 10 MiB 启动预留值；采集期间定期复查空间，低于预留值时停止采集并尝试收尾保存。
- Web 统计样本数的字号从 11 px 调整为 12 px。
- 发布内容为源码 ZIP 和 SHA-256 校验文件；不包含预编译 Windows EXE。
- 自动化测试和 Mock 验证不代表真实 Fluke 8845A 硬件验证。

## English

- Desktop and localhost interfaces can export a spreadsheet-safe copy of `measurements.csv`. Only the `raw_reading` and `error` text columns are escaped; SI numeric fields remain unchanged and the original file is preserved. Export is unavailable during acquisition.
- Acquisition preflight shows currently available disk space and the 10 MiB startup reserve. Space is rechecked during acquisition; collection stops and attempts to finish saving if free space drops below the reserve.
- Web statistic sample counts use 12 px text instead of 11 px.
- Assets are the source ZIP and SHA-256 checksum; no prebuilt Windows executable is included.
- Automated tests and Mock checks do not constitute validation on a real Fluke 8845A.
