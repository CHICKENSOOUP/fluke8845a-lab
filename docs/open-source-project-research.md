# 测量软件开源项目源码与许可证调研 | Open-source Project Research

[简体中文](#简体中文) | [English](#english)

## 简体中文

调研日期：2026-10-14

本文件记录三个项目对 Fluke 8845A RS-232 测量软件的可借鉴内容、适配边界及许可证审查结果。审阅的是以下源码快照；仓库主分支后续可能变化：

| 项目 | 审阅快照 | 许可证 |
|---|---|---|
| [ETH Zurich HVL Common Code Base](https://gitlab.com/ethz_hvl/hvl_ccb) | `devel` @ `c99334f26e3ca88aa5277aa3c45037572f50004e` | GPL-3.0-or-later |
| [wwd_gui_api](https://github.com/andersbandt/wwd_gui_api) | `main`，提交 `66b0d8c` | MIT（主仓库） |
| [psc-datalogger](https://github.com/DiamondLightSource/psc-datalogger) | `1.1.0` | Apache-2.0 |

本审查未复制三个项目的源码，也未连接 COM 端口或执行这些项目的运行测试。SCPI 命令、量程、固件门槛、触发和测量限制仍以研发时参考的 Fluke 官方资料（手册未随此仓库发布）及[SCPI 技术依据](scpi-reference.md)为准。

## 横向比较

| 关注点 | `hvl_ccb` | `wwd_gui_api` | `psc-datalogger` |
|---|---|---|---|
| 与 8845A 的关系 | 有 8845A/8846A 驱动；实现选择 TCP 3490 | 没有 8845A 驱动；子模块里有 8842A 文件 | 支持 Agilent 3458A/34401A，不支持 Fluke |
| 采集与线程 | 库提供可停止/可限时的轮询器；不是桌面采集 GUI | Python 线程控制实验记录，单调时钟计算间隔；另启 Dash 实时绘图服务 | PyQt `QThread` + `QObject` Worker，信号通知 GUI |
| 异常处理 | 通信查询空响应报错；由调用方处理；无串口重连实现 | DMM 服务返回 `(None, error)`；底层串口 `reopen()` 只处理打开失败，运行中串口异常会终止该读循环 | 初始化找不到 Prologix 时 5 秒重试；单次读取失败保存 `#ERROR` 并发信号，之后循环继续；未见运行中断线后的重新建链 |
| 图形界面 | 不提供本任务所需桌面曲线界面 | Plotly/Dash 实时曲线及 HTML 导出；有界队列可能背压采集线程 | 简单 PyQt 日志界面，没有实时曲线 |
| CSV | 没有完整实验 CSV 记录器 | 每行重新打开、追加、关闭；同时在内存保留 `recorded_data` | 文件句柄常驻，每条写入后 `flush()`；错误记作 `#ERROR` |
| 最值得借鉴 | 配置类型、设置后回读、协议 Mock 测试思路 | 单调时钟节拍、线程安全图形缓冲与界面分层 | Qt Worker 信号、逐条落盘和明确错误行 |

这三者都不能直接充当本项目的 COM 端口/8845A 完整驱动。横向表描述的是代码审阅所得，不代表在真实 Fluke 8845A 上验证通过。

## 1. `hvl_ccb`：8845A 仪表驱动

项目 README 列出 Fluke 8845A 和 8846A，并将驱动称为 `Fluke8845a`。实现位于 [`src/hvl_ccb/dev/fluke884x/base.py`](https://gitlab.com/ethz_hvl/hvl_ccb/-/blob/c99334f26e3ca88aa5277aa3c45037572f50004e/src/hvl_ccb/dev/fluke884x/base.py)，量程和滤波枚举位于 [`ranges.py`](https://gitlab.com/ethz_hvl/hvl_ccb/-/blob/c99334f26e3ca88aa5277aa3c45037572f50004e/src/hvl_ccb/dev/fluke884x/ranges.py)。

- 驱动的通信类继承 TCP 通信实现，默认端口为 3490，命令结束符为 CR。此处是该库选择的通信链路，不代表仪表只能使用 TCP。
- 启动流程依次打开连接、发送 `SYST:REM`、`*RST`、`*CLS`。`*RST`会将仪表恢复到上电配置，移植时不能不加判断地复用。
- 单次 `measure()` 按 `INIT`、`*TRG`、`FETC?` 读取；模式设置后查询 `FUNC?`核对，量程/带宽描述符也会查询设置值。
- 功能枚举包含直流/交流电压、直流/交流电流、2W/4W电阻，以及频率、周期、二极管等项；枚举不能直接视为本应用可启用的 8845A 功能列表。
- 量程枚举覆盖多种电压、电流和电阻量程，交流电压/电流有 3、20、200 Hz 滤波枚举；频率/周期另有 aperture 枚举。该驱动没有本应用所需的 NPLC 配置 API。
- 通信层对空响应默认最多重试 10 次、重试间隔 0.5 秒；Fluke 包装类对最终没有非空响应的查询抛出设备异常。此路径不是自动断线重连机制。
- 项目测试树含 Fluke 本地 TCP 服务器 Mock，可作为无硬件协议测试的参考；Mock 服务器并不证明真实仪器行为。

**复用结论：**技术上适合参考“写入后回读验证”和设备配置抽象。许可证文件为 GPL-3.0-or-later；直接复用代码可能影响本软件整体分发许可，需先单独完成许可兼容性审查。当前软件不复制此代码，独立依照官方手册维护自己的驱动实现。

它还附带 `Poller`：用 `ThreadPoolExecutor(max_workers=1)` 在后台执行回调，支持延迟启动、停止事件和超时。它按固定 `sleep/wait` 后再运行回调，回调耗时会叠加到周期中；没有面向 GUI 的信号传递、队列或 CSV 存储。该工具可作为简单轮询生命周期的参考，当前项目的采集线程已有状态控制和记录链路，因此没有直接移植。

## 2. `wwd_gui_api`：实验记录、设备服务与实时曲线

主仓库 README 描述 Tkinter 多设备测试软件、记录线程、CSV和Plotly/Dash曲线；仓库明确采用 MIT 许可证。实际实现可见 [`common/serial_api.py`](https://github.com/andersbandt/wwd_gui_api/blob/66b0d8c/common/serial_api.py)、[`common/logger.py`](https://github.com/andersbandt/wwd_gui_api/blob/66b0d8c/common/logger.py)、[`common/csv_helper.py`](https://github.com/andersbandt/wwd_gui_api/blob/66b0d8c/common/csv_helper.py)、[`common/plotter.py`](https://github.com/andersbandt/wwd_gui_api/blob/66b0d8c/common/plotter.py) 和 [`services/equipment_service.py`](https://github.com/andersbandt/wwd_gui_api/blob/66b0d8c/services/equipment_service.py)。

- Windows 串口扫描使用 `serial.tools.list_ports.comports()`。通用 `SerialGeneral`只接收端口和波特率，没显式设置超时、校验、停止位和流控；PySerial 默认读超时为无限等待。`reopen()`处理打开失败，但不是持续通信异常后的自动恢复策略。
- DMM 服务把通信异常或解析异常转成错误返回；记录行可写入 `ERROR`，但并未把完整异常细节作为独立的状态与原始响应保存。
- 定时采集使用单独的 Python `StoppableThread`，以 `time.monotonic()`计算周期剩余等待。暂停/继续会复用同一 CSV。
- `CSVHelper`追加行时每次重新打开和关闭文件，结构简单；写入 `ERROR` 时不保留精确设备异常。实验函数会把行追加进 `recorded_data`，长实验若一直运行会持续占用内存。
- 实时图运行在本机 Dash/浏览器服务中，通过有界队列接收采集行，绘图使用有界 deque；采集线程写文件后向绘图队列 `put()`，慢消费者或队列填满可能增加采集线程等待。
- 子模块 `EEequipment` 是单独仓库，父仓库的 MIT 许可证不自动覆盖它。审阅的子模块提交为 `1559830`，检出内容没有 `LICENSE` 或 `COPYING` 文件；其中 Fluke 8842A 类的量程/模式方法是 `pass` 存根，不是可用驱动，更不是 8845A。当前审阅下不复用此子模块代码。

**复用结论：**MIT 主仓库代码可在保留版权和许可证声明的条件下使用；结构上可借鉴设备服务层返回连接结果、曲线缓存与完整原始记录分离的思路。其 Tkinter/Dash GUI、DMM 驱动和通用串口类不适合直接替换本项目的 PySide6/pyqtgraph 与 Fluke 8845A 驱动。

## 3. `psc-datalogger`：PyQt 后台采集与 CSV

项目 README 说明该工具记录一至三台 Agilent 3458A/34401A，支持采样间隔、CSV及 Windows EXE 打包；源码入口有 [`connection.py`](https://github.com/DiamondLightSource/psc-datalogger/blob/1.1.0/src/psc_datalogger/connection.py)、[`multimeter.py`](https://github.com/DiamondLightSource/psc-datalogger/blob/1.1.0/src/psc_datalogger/multimeter.py) 和 [`gui.py`](https://github.com/DiamondLightSource/psc-datalogger/blob/1.1.0/src/psc_datalogger/gui.py)。许可证为 [Apache-2.0](https://github.com/DiamondLightSource/psc-datalogger/blob/1.1.0/LICENSE)。

- `ConnectionManager`建立 `QThread` 和 Worker，将设备读取放在 GUI 主线程之外。
- Worker 用 PyVISA 枚举 VISA 资源，并通过 Prologix 的 `++help`识别 GPIB 转接器，再以 `++addr`选择设备；它不是直接连接 Fluke 8845A COM 端口 的 PySerial 驱动。
- 抽象 `Multimeter`及其实现仅覆盖两款 Agilent。NPLC命令、触发等待和读取命令是 Agilent 专属，不能复制给 Fluke。
- `DataWriter`使用 `csv.DictWriter`并在每行后 `flush()`，可借鉴逐点刷新的方法；文件以写模式打开，CSV只有时间戳和最多三个仪表读数列，通信错误以 `#ERROR`保存，缺少本项目所需的每次实验目录、完整仪器配置、原始响应和事件记录。
- Worker 捕获单次仪器读取异常、发出错误信号并在对应字段写 `#ERROR`；启动时找不到 Prologix 会每 5 秒重试。实际采集周期是在一轮读取后再 `sleep(interval)`，因此周期还会叠加设备读取耗时。GUI没有实时测量曲线。
- GUI依赖 PyQt5，与本项目的 PySide6 技术栈不同。

**复用结论：**Apache-2.0 允许复用，但分发时需保留许可证及版权/归属声明、标注修改，并包含适用的 NOTICE。可参考 Worker 与逐行刷新的做法；当前项目已有更完整的 `QThread`采集层和元数据 CSV 记录器，因此不直接移植这些模块。

## 4. 针对 Windows COM 端口 与 8845A 的落地边界

若将上述设计思想应用于此项目，需要保持以下具体条件：

1. 直接使用 PySerial 打开 Windows `COM 端口`；本次已验证的串口设置为 115200 baud、8 数据位、无校验、1 停止位、无流控、CRLF。其他电脑或仪表串口菜单变更后仍应从界面核对。不能发送 Prologix `++help`/`++addr`，也不能按 TCP 端口 3490建立 socket。
2. 用 `*IDN?`解析厂牌、型号、序列号，并保留第四字段原文；它在8845A/8846A模式下通常是日期/时间修订标记，不能直接称作固件。`SYST:VERS?`查询的是SCPI命令版本。仅接受 Fluke 8845A；不能因上游代码或说明把 8846A 的模式、量程、速度或固件能力带进来。
3. 六种模式、量程、NPLC、交流滤波、端子和固件条件必须逐项按本项目官方资料配置及核验；模式不适用的参数应隐藏或禁用。尤其电流模式仍需提醒用户断电、检查保险丝和物理串联接线，软件不能识别表笔插孔。
4. 串口收发只能由采集工作线程独占；通信超时、断线和仪器 LOCAL 状态应保存为空数值的异常行，不得填零或沿用上次读数。重连必须在错误状态被记录后进行。
5. 曲线绘制和统计不能阻塞原始数据写入。每次实验独立目录，记录逐点刷新；原始响应与数值分开保存，历史图可抽稀而不能改写原始 CSV。

## 5. 与当前工程的对应关系

当前代码已按直接 COM 串口而非以上游 TCP/Prologix 为传输方式实现：

- [`src/fluke8845a/scpi.py`](../src/fluke8845a/scpi.py)：PySerial 参数配置、结束符处理、8845A型号识别、SCPI读数解析和通信错误分类。
- [`src/fluke8845a/worker.py`](../src/fluke8845a/worker.py)：独立采集线程、状态机、暂停/继续/停止、时间戳、异常记录及停止条件。
- [`src/fluke8845a/storage.py`](../src/fluke8845a/storage.py)：逐点刷新原始 CSV、实验配置与事件/运行日志。
- [`src/fluke8845a/gui.py`](../src/fluke8845a/gui.py)：PySide6中文界面、pyqtgraph曲线、分析、历史记录与采集状态。

这些模块的实现仍要以官方 Fluke 手册为准。本次开源项目审查没有改变 SCPI 命令、量程或仪器能力声明；许可证允许复用与仪器行为正确性是两项独立检查。

## 6. 对本项目界面和采集架构的直接启示

- 采集线程只负责 SCPI、时间戳和落盘；图形界面通过有界、可丢弃旧显示点的队列更新曲线。图形刷新变慢时可以降低显示刷新率或抽稀显示缓存，但不能阻塞仪器读取和原始 CSV 写入。
- 保留一个原生桌面实时曲线作为主工作区。WWD 的 Plotly/Dash 浏览器窗口适合研究交互和 HTML 导出，但多个窗口/浏览器服务不适合替代 PySide6 主界面；PSC 的表单+状态栏适合参考线程信号与状态反馈，没有曲线布局可复用。
- 将连接、测量配置、实验控制、曲线和统计分层展示；常用状态（连接、模式、当前读数、实际频率、保存状态）应一直可见，把串口诊断、SCPI 日志、设备参数放到可展开区域。
- 对设备参数采用“选择模式后只显示有效参数”的方式。连接或模式切换时回读仪器配置；不确定、超时或断线状态要显式呈现，不能显示成正常数值。
- COM 端口 是本机串口号，不是仪器型号的一部分。启动时枚举可用端口并默认选中 COM 端口（若存在），同时允许手动修改；连接参数继续显式设置超时、8N1、流控和 CRLF。

## English

Research date: 2026-10-14. This review examined three public measurement/data-logging projects as architectural and licensing references. No source code was copied, and the projects were not run or connected to a COM device. Instrument commands, ranges, firmware constraints, triggering, and measurement limits remain governed by Fluke's official manuals referenced during development; those PDFs are not included here. See the [SCPI reference](scpi-reference.md).

### Projects reviewed

- **ETH Zurich HVL CCB**: the reviewed repository is MIT-licensed. Its separation of device service, plotting cache, and raw records offers architectural ideas. A separately maintained `EEequipment` submodule did not contain a license or a working Fluke 8845A driver; its Fluke 8842A range/mode methods were stubs. No submodule code is reused.
- **`wwd_gui_api`**: the reviewed project uses a different device/network and GUI architecture. Its Tkinter/Dash interface, generic serial classes, and DMM methods are not drop-in replacements for this application's PySide6/pyqtgraph interface and direct 8845A serial driver. The detailed Chinese review records the frozen revision and license evidence; do not infer a submodule's rights from its parent repository license.
- **`psc-datalogger`**: Apache-2.0 licensed, using PyQt5, worker threads, PyVISA, and a Prologix GPIB adapter for Agilent meters. Per-row CSV flushing and worker signaling are useful reference patterns, but its transport, commands, device support, and output format differ. Apache reuse requires retaining license/copyright notices, marking changes, and including applicable NOTICE material. No code was ported.

These findings apply to the frozen revisions examined on the research date and are not an exhaustive audit of the projects.

### Application-specific boundaries

This application uses direct Windows COM serial communication through PySerial; it must not send Prologix `++help`/`++addr` commands or use a TCP socket. `*IDN?` must identify a Fluke 8845A; its fourth field is preserved as an IDN revision and any dotted number is labelled only as an unverified candidate, never as OutG SW evidence. The 8845A manual defines `SYST:VERS?` as the SCPI command version query, not a firmware query. The DCI 400 mA firmware threshold explicitly maps to OutG SW; the ACI specification lists a software threshold but does not identify OutG versus InG. The application preserves the ACI 1.0.700.18 requirement as an operator-visible panel check and does not infer a firmware field for automatic rejection. 8846A behavior must not be assumed. Mode, range, NPLC, AC filter, terminal, and firmware support are validated against Fluke references, with unresolved fields identified in the UI. Serial I/O is owned by the acquisition worker; failed attempts are stored with an empty value and explicit status. Plot downsampling must not change raw CSV records or block acquisition.

The implementation maps those responsibilities to `scpi.py` (serial and SCPI), `worker.py` (acquisition thread and state), `storage.py` (CSV/config/events/logging), and `gui.py` (PySide6 UI and plotting). This research did not modify command behavior or instrument capability claims. Reuse permissions and technical correctness are separate checks.

### Design takeaways

Keep acquisition, timestamps, and durable writes independent of GUI refresh. Keep connection state, mode, current reading, actual rate, and save status visible; put diagnostics and advanced serial settings in an accessible secondary area. Show only settings applicable to the selected mode, and make uncertain or failed states explicit. COM is the host port name, not an instrument model. The port and serial settings must remain configurable and be verified by the operator.
