# Fluke 8845A Lab 使用指南 | User Guide

[简体中文](#简体中文) | [English](#english)

## 简体中文

版本 1.0.0 · 中文 Windows 桌面应用

## 1. 启动与工作模式

v1.0.0 是源码发布，不包含预编译 Windows 程序。请按根目录 [README](../README.md) 的步骤安装并启动软件。本版本的验证范围见[硬件验证说明](hardware-validation.md)；不宣称测量准确度或当前源码快照已通过真机验证。

- **真实仪器**：将仪器后面板设为 RS-232，并选择 8845A/8846A 指令集的 COMPUTER 协议模式。连接后本程序默认以 COMPUTER/REMOTE 控制仪表；仪器设置区可切换为 LOCAL 面板控制。连接时本程序查询 `*IDN?`，只接受设备型号字段为 `8845A` 的 Fluke 仪器；不会重置仪器。
- **MOCK**：勾选模拟模式后点击“启动MOCK模拟仪器”。状态栏和数据目录都会写明 MOCK。模拟结果不是仪器测量，不能用作校准或实验结论。

程序启动时扫描串口，也可在端口框直接输入 COM 号。默认值为 9600 baud、8 data bits、无校验、1 stop bit、无流控、CRLF 结束符、2秒普通命令响应超时。`READ?`会根据当前积分时间或交流滤波设置自动使用更长的主机等待上限；这只是避免过早超时的通信保护，不代表仪器的采样时间保证。手册中 RS-232 出厂参数包含 9600、8位、无校验、1停止位和无流控；EOL需按仪器面板设置。可配置选项包括手册列出的全部波特率、7/8数据位、无/奇/偶校验、1/2停止位、无/XON-XOFF/RTS-CTS流控以及CR/LF/CRLF结束符。若`READ?`仍超时，程序保存一条无数值的超时记录后停止采集并关闭串口；必须重新连接后才能开始下一次采集，避免迟到响应被误认为后续样本。

首次连接失败时，确认物理RS-232/USB转串口线、COM号、线缆类型、波特率和EOL。仪表菜单中的 RS-232 `COMPUTER` 协议模式与软件里的 `COMPUTER/LOCAL` 控制权是两项设置：协议模式须在仪表菜单选择；控制权可在仪器设置区切换。`COMPUTER` 使用 `SYST:REM`，除仪表的 `LOCAL` 键外，其他面板按键会被禁用。选择 `LOCAL` 会发送 `SYST:LOC`，将控制权交给面板；软件停用测量、设置、诊断和ZERO，串口保持连接。面板操作完成后，在软件选择 `COMPUTER` 即可切回远程控制；软件会重新读取当前测量模式和参数。采集中不能切换控制权。若直接按仪表 `LOCAL` 导致 `+550,"Command not allowed in local"`，软件会保存异常行且不伪造读数，停止采集并关闭串口；重新连接可恢复远程控制。连接后可在“仪器 → SCPI诊断”检查型号、模式、输入端子及仪器错误队列。正常断开或关闭程序时会尝试发送 `SYST:LOC` 恢复本地控制。

## 2. 测量模式与安全接线

支持 **DCV、ACV、DCI、ACI、2W电阻、4W电阻**。每个模式单独记住量程、NPLC/交流带宽、模拟滤波和自动零点设置；设置存于当前 Windows 用户配置目录。无关参数显示“不适用”且不能修改。ACV/ACI显示真 RMS 有效值，不是峰值或波形采样数据。

切换电压、电流或两线/四线电阻接线类别时会显示正确/错误接线示意和确认提示；开始电流或电阻实验前还会再次要求确认。示意图依据官方中文《用户手册》第 4-4、4-9 至 4-11 页的前面板端子图和接线步骤整理。软件不能观察被测电路、测试线、保险丝或插孔，这些检查必须由操作者完成：

- **电压**：电压 HI/LO 端子并联接入被测节点；核对电压等级和测量类别。
- **两线电阻**：被测电路断电，使用相应 HI/LO 端子。引线电阻会影响结果。
- **四线电阻**：被测电路断电；SOURCE/FORCE 线和 SENSE HI/LO 线接到正确端子。
- **电流**：必须先关闭被测电路电源，再将仪器串联接入回路。前面板的 mA/400 mA 与 10 A 是两个独立红色插孔：预期电流不超过 400 mA 时红表笔接 mA/400 mA、黑表笔接 LO；预期电流大于 400 mA 且不超过 10 A 时，红表笔接 10 A、黑表笔接 LO；两个红色电流孔不能同时连接。应用会按当前固定量程显示应选的插孔；自动量程时必须按最大预期电流选孔，并核对仪表显示的 mA/10 A 输入选择。绝不可将电流输入并联到电压源。

8845A 的 mA 输入有 440 mA 保险丝，10 A 输入有 11 A 保险丝；资料列出的允许值为 400 mA 连续、550 mA 最多2分钟后停1分钟。保险丝额定值不代表可连续测量值。1 A、3 A、10 A测量量程只适用于前面板端子；当前端子不支持时，软件会禁用这些高电流量程及可能跨入这些档位的电流自动量程。400 mA 功能有固件版本条件：DCI要求软件至少2.0，ACI要求至少1.0.700.18。8845A/8846A模式的`*IDN?`第四字段通常为日期/时间修订标记，不是可比较的软件版本；`SYST:VERS?`返回SCPI命令版本，也不是仪器软件版本。程序优先使用`*IDN?`中的可解析软件版本；若该字段只有日期/时间标记，可在连接页按序列号记录人工核对的 OutG SW 版本。仅当所选证据可解析且明确低于门槛时，才禁用固定400 mA档；InG SW只记录，不参与判断。固件未知或版本无法解析时不会触发拒绝。已知固件低于门槛时，电流自动量程也会被阻止，因为仪器可能进入400 mA档。面板版本记录是人工提供的设备元数据，不是SCPI回读。

改变模式或固定量程不会移动测试线或切换物理插孔。手册列出的 mA/10 A 前面板输入选择应与实际表笔插孔一致。`ROUT:TERM?`只报告FRONT/REAR，量程回读也不能识别红表笔接在mA/400 mA孔还是10 A孔；软件无法替操作者验证。量程切换跨越400 mA分界时，请再次断电、检查端子和保险丝。

## 3. 测量配置

量程列表来源于8845A扩展规格，并以SI单位传给仪器：

| 模式 | 软件提供的范围 | 可用参数 |
|---|---|---|
| DCV | 100 mV、1 V、10 V、100 V、1000 V | NPLC 0.02、0.2、1、10、100；模拟滤波；自动零点 |
| ACV | 100 mV、1 V、10 V、100 V、750 V | 交流滤波 3、20、200 Hz；自动零点 |
| DCI | 100 µA、1 mA、10 mA、100 mA、400 mA、1 A、3 A、10 A | NPLC 0.02、0.2、1、10、100；模拟滤波；自动零点 |
| ACI | 10 mA、100 mA、400 mA、1 A、3 A、10 A | 交流滤波 3、20、200 Hz；自动零点 |
| 2W | 100 Ω、1 kΩ、10 kΩ、100 kΩ、1 MΩ、10 MΩ、100 MΩ | NPLC 0.02、0.2、1、10、100；模拟滤波；自动零点 |
| 4W | 100 Ω、1 kΩ、10 kΩ、100 kΩ、1 MΩ、10 MΩ、100 MΩ | NPLC 0.02、0.2、1、10、100；模拟滤波；自动零点 |

可打开自动量程，或取消勾选后选择固定量程。电流模式在当前端子确认为前面板时可开放自动量程；已知固件低于400 mA要求时会拒绝自动量程。后端子下1 A、3 A和10 A固定量程仍不可用。按“应用仪器参数”后，软件写入SCPI命令并读取量程、功能、带宽、端子等可用状态；仪器错误队列非零或功能回读不匹配时不会启动实验。模拟滤波器只出现在DCV/DCI/电阻模式；NPLC只出现在这些直流积分模式。AC滤波档的3/20/200 Hz分别是慢/中/快三档截止频率选择，不是采样频率。

**零点校正与校准、去皮不同。**将鼠标悬停在“自动零点校正”或“执行 ZERO 校正”上，可查看各自说明。“自动零点校正”让仪表按自动零点流程测量内部零点并补偿偏移，有助于稳定读数，但额外测量会降低读数速度；它不会把被测输入强制显示为0。“执行 ZERO 校正”发送 `ZERO:AUTO ONCE`，按 Fluke 编程手册的定义立即启动一次零点测量。此操作不是校准调整，也不是对当前被测物去皮。只能在停止采集后执行；若启用模拟滤波，量程、NPLC和触发条件须与 ZERO 测量要求匹配。

**仪器测量速度和软件采样间隔是两项独立设置。** 直流电压/电流及电阻模式通过NPLC控制积分时间：0.02 NPLC通常转换较快，100 NPLC积分时间较长、通常更有利于抑制工频噪声；交流电压/电流模式使用3/20/200 Hz带宽档，200 Hz档响应较快。速度参数会按模式保存。停止采集时点击“应用仪器参数”写入仪器，开始实验时也会自动写入；连续采集或暂停时可修改NPLC/交流带宽，再点“应用采集速度”。仪表回读确认后从下一读数起生效，修改时间及生效样本写入配置和事件文件，CSV每行记录当时生效的NPLC/带宽。采集中的实时调速仅会改NPLC或交流带宽，不改变测量模式、量程、滤波或零点校正。软件目标采样间隔可单独设定，并能在连续采集中即时修改；实际Hz由有效读数时间戳计算。最低NPLC或最快带宽不保证某个固定Hz，实际速度还受量程、自动零点、滤波、仪器转换及串口通信影响。

“初始化参数”会将六种测量模式恢复为软件默认设置，并把目标采样间隔设为1秒；会清除各模式保存的基准值，但保留串口通信设置和当前保存目录。它只更改软件配置，不会向仪器发送复位或配置命令；若需把当前模式参数写入已连接的仪器，请随后单独点击“应用仪器参数”。采集时须先停止才能初始化。

采集中调整速度下拉框只修改“所选值”，不会立即改变仪表。读数卡会分别显示仪表已回读生效的参数和“待应用”值；点击“应用采集速度”且仪表回读成功后，新速度才从下一读数起生效。若应用失败，采集会安全停止并保留失败记录，不会把所选值误记为仪表当前值。

桌面主窗口以单张曲线和当前读数/采集状态为主。点击右下角状态栏的“设置与分析…”打开独立窗口：“连接与采集”页含串口、测量模式、仪器速度、实验目录和目标间隔；“分析与事件”页含统计、基准、局部区间和事件。完整SCPI通信日志可从“仪器”菜单打开。日志暂存最多5000条SCPI行；如果界面处理落后，会提示丢弃较早日志，测量CSV和全程统计不受影响。NPLC/交流带宽只在适用模式中启用，灰色控件表示当前模式不可用。目标间隔设为0表示每次读数后不额外等待，不代表仪器转换或串口延迟为零。

采集速率会受测量模式、量程、NPLC/交流带宽、自动零点、串口波特率和被测条件共同影响。请以当前实验中实际记录的时间戳和有效读数为准，不要把某一台设备或某次接线的速率当作准确度或通用性能保证。

## 4. 单次测量与连续采集

填写实验名称和保存根目录；目标间隔位于“设置与分析…”窗口的“连接与采集”页。目标间隔可设0至86400秒，输入精度为0.001秒；填0表示每次读数后不额外等待。默认目标间隔保存在当前Windows用户设置中，重启软件后仍保留。采集过程中也可修改目标间隔，新值立即调整后续调度，并记入`config.json`的变更历史及`events.csv`。软件不承诺超出仪器转换、串口波特率或积分时间能力的速率；请以实际频率显示和CSV时间戳为准。按“单次测量”会创建一份仅含一次尝试的实验；连续模式支持开始、暂停、继续和停止。必须停止当前实验后切换模式，切换后下一次采集创建独立数据目录。

真实后端使用 `CONF:<function>` 配置，之后逐次 `READ?` 触发/读取，并在每点读取实际量程。它不会调用 `INIT/FETCh` 批次模式，因此不占用或压缩仪器内部缓存，也不会把缓存批次的主机读取时刻误当作每个样本的采样时刻。

CSV的PC本地/UTC时间和`elapsed_s`记录主机发送单次`READ?`前的时间；`read_duration_s`包括读数及量程回读的主机通信周期。仪器没有给每个读数提供本机硬件时间戳，因此转换器真实采样时刻只在一次读取周期内，不能宣称比这个窗口更精确。GUI实际频率由最近最多21个有效读数的PC单调接收时刻差计算，短实验使用现有点数；它不是目标间隔的倒数。可用每点`elapsed_s + read_duration_s`重建接收时刻核对该滚动值。每次超时/异常尝试仍占一个序号，`value_si`留空并写明状态，不伪造零值。

串口、触发/读数解析、CSV写入、统计累积均在专用采集线程完成。测量数据逐点追加并刷新文件；GUI绘图按小批次刷新，最多保留最近100,000个点在内存中。若GUI线程暂时卡顿，界面通知会合并，待处理显示数据最多保留最近10,000点；曲线可能跳过中间显示点，但CSV和全程统计不会因此丢失。长实验全程min/max/平均值/样本标准差采用流式统计，不受绘图缓存长度影响。程序退出会请求停止当前采集、关闭文件和串口并尝试恢复仪器本地控制。

## 5. 分析、事件与历史数据

- 滚轮默认缩放读数轴；Shift+滚轮缩放时间轴，Ctrl+滚轮同时缩放两轴，Alt+滚轮缩放读数轴并以零点为锚。拖动曲线可平移视图，“自动缩放”重新适配当前数据；另可启用自动跟随、读取最近点、选择局部统计区间和导出PNG。统计区间可直接输入相对实验开始的起止秒数，也可拖动图中区间手柄；两种操作会互相同步。自动跟随先逐步扩展视窗至120秒，再滚动显示最近120秒。
- 基准可以手动输入SI值、取最新读数、取图上锁定点或取当前图中缓存均值。鼠标悬停只预览读数；在曲线上单击会锁定黄色圆圈标出的数据点，移动鼠标不会改变它。点击“锁定点设为基准”后，软件使用该固定点的读数。电压显示ΔV/V₀，电流显示ΔI/I₀，电阻显示ΔR/R₀。阈值栏使用SI值；基准绝对值小于或等于阈值、阈值无效或基准未设置时不计算相对变化率。
- “记录实验事件”写入事件CSV并在曲线上画标记。基准设置、更改和清除在活动实验中也会写入事件并画出曲线标记。
- “加载历史CSV”只读现存测量文件；若同一实验目录中有配套`events.csv`，会一并恢复落在当前时间范围内的事件标记。超过100,000点时，图形显示均匀抽稀视图，原文件不变；拖选区间后，软件在后台扫描完整原始CSV计算统计，图表抽稀不影响统计。实时采集期间，统计完成后会把区间内新到的有效读数增量纳入，后台读取不阻塞采集线程。

## 6. 实验目录和CSV字段

每次实验在根目录下新建唯一时间戳目录，包含：

- `measurements.csv`：每次测量/失败尝试一行。`raw_reading`保留仪器响应文本，`value_si`保存未经滤波/舍入的SI值；`voltage_v`、`current_a`、`resistance_ohm`三列供常见工具直接筛选。无效值为空字符串，状态列写`timeout`、`overflow`、`invalid`、`instrument_local`或其他通信错误。
- `config.json`：原始仪器身份、IDN修订字段、仪器通过IDN提供的软件版本、按序列号手动核对的 OutG/InG SW（若填写）、400 mA 判断采用的版本来源、串口、请求的模式设置、仪器回读、数据是真机还是模拟、采样时间语义及最终状态。手动版本会与SCPI读回分开保存。
- `events.csv`：人工事件、暂停/继续和相对基准变更。
- `run.log`：实验启动/停止、SCPI配置命令和异常。逐点原始响应在CSV；为避免长期高速采集产生巨型日志，测量响应不重复写入run.log。实时SCPI发送/接收在GUI日志面板可见；显示队列有界，界面跟不上时会提示丢弃的旧日志行。

CSV以UTF-8 BOM编码，含样本序号、主机本地和UTC ISO时间、单调经过时间、读数通信耗时、实际间隔、模式、SI单位、原始响应、仪器量程、NPLC、交流带宽、滤波/零点状态和异常信息。Python `repr(float)`不做小数位截断；`raw_reading`保留SCPI返回的十进制文本。

## 7. MOCK测试

MOCK可选阶跃响应/恢复、长期漂移、指定样本通信超时和指定样本断线场景；六种模式都有噪声和漂移读数。超时/断线测试均为模拟故障。看右上角MOCK标识及配置文件`simulation: true`识别模拟数据。

## 8. 依据、限制和状态

此前记录的冻结程序构建流程曾逐模式执行六种测量的 Mock GUI 采集和文件保存检查；这些记录不是当前源码快照的硬件验收结果。

可追溯 SCPI 与规格请看 [SCPI 参考](scpi-reference.md)。硬件检查范围与当前版本的验证限制见[硬件验证说明](hardware-validation.md)。当前源码包含 Mock 工作流；模拟结果不是真机验证。


本源码发布未附带预编译 Windows 二进制。测试与硬件验证的当前范围见[硬件验证说明](hardware-validation.md)。

## 9. localhost 浏览器界面

运行 `python -m fluke8845a.webapp` 或双击 `run_web.bat` 可启动本机网页界面。服务只监听 `127.0.0.1:8765`，不加载远程页面或 CDN；网页与桌面版共用仪器驱动、采集线程和逐点写盘器。主页面只显示当前读数、采集状态、控制按钮和一张大曲线；顶部“设置与分析”打开单独的设置对话框。图表的滚轮和 Shift/Ctrl/Alt 用法与桌面端相同，并提供自动缩放、自动跟随、历史 CSV 和 PNG 导出。

启动服务不会自动打开串口。连接前先断开桌面版占用的 COM 端口，因为同一端口不能同时由两个程序打开。电流和电阻测量仍须由操作者确认接线、输入端子、保险丝和被测电路状态。页面的“退出服务”会停止采集、完成待写数据并安全关闭串口。Mock 读数始终是模拟数据。

## English

Version 1.0.0 is a source release and does not include a prebuilt Windows executable. See the root [README](../README.md) for installation and startup instructions.

### 1. Connection and modes

For a real meter, set the rear interface to RS-232 and select the COMPUTER protocol with the 8845A/8846A command set. The application connects using COMPUTER/REMOTE control by default; its instrument settings panel can switch between this state and LOCAL front-panel control. It queries `*IDN?`, accepts only a Fluke model identified as `8845A`, and does not reset the instrument. Mock mode exercises simulated workflows; its readings are not instrument measurements and must not be used as calibration or experimental evidence.

The application scans serial ports at startup and also accepts a manually entered COM port. Defaults are 9600 baud, 8 data bits, no parity, one stop bit, no flow control, CRLF termination, and a two-second response timeout. Confirm the instrument's settings. The instrument menu's RS-232 COMPUTER protocol and the application's COMPUTER/LOCAL control state are separate settings. COMPUTER control disables front-panel keys except LOCAL. The application can send `SYST:LOC` to hand control to the panel; measurement and settings commands are paused while LOCAL. Switching back to COMPUTER sends `SYST:REM` and refreshes the active function and settings. A normal disconnect attempts `SYST:LOC`.

### 2. Measurement modes and safe wiring

Supported modes are DCV, ACV, DCI, ACI, 2-wire resistance, and 4-wire resistance. AC readings are RMS values, not peak values or waveform samples. The application cannot inspect the circuit, test leads, terminals, or fuses. Connect voltage inputs in parallel. De-energize the circuit before resistance measurements. For current, de-energize first and connect the meter in series. Select the appropriate mA/400 mA or 10 A input, never connect both red current inputs simultaneously, and never place a current input in parallel with a voltage source. Check wiring, range, front-panel input selection, and fuses before each measurement. Software cannot identify which physical red jack contains a lead. Firmware-sensitive limits apply only to 400 mA: a parseable `*IDN?` software version takes priority; if that field is a date/time marker, the operator may record OutG SW from the front-panel versions page, saved by instrument serial number. InG SW is recorded as metadata only. The fixed 400 mA range is rejected only when the selected version evidence is parseable and explicitly below the documented threshold; unknown or unparseable values do not cause rejection. Autorange is blocked only when an explicitly old firmware version could let the meter enter 400 mA.

The mA input has a 440 mA fuse and the 10 A input an 11 A fuse. The documented limit is 400 mA continuous; 550 mA is limited to two minutes followed by one minute off. Fuse ratings are not continuous operating limits. Ranges documented as unsupported for the model or active terminals are disabled. The firmware-sensitive fixed 400 mA range is blocked only when parseable version evidence is explicitly below that mode's threshold; a semantic `*IDN?` version has priority, with manually verified OutG SW as fallback when IDN contains only a date/time marker. InG SW is recorded but does not affect the gate. Other fixed ranges are unaffected by firmware; terminal-specific 1 A, 3 A and 10 A restrictions still apply. `SYST:VERS?` returns the SCPI command version, not the software version. Manual panel entries are labelled separately from SCPI data in experiment metadata.

### 3. Measurement settings and speed

DCV ranges are 100 mV–1000 V; ACV ranges are 100 mV–750 V. DCI ranges are 100 µA–10 A and ACI ranges 10 mA–10 A, subject to firmware and terminal conditions. Resistance ranges are 100 Ω–100 MΩ. DC voltage/current and resistance provide NPLC values 0.02, 0.2, 1, 10, and 100. AC voltage/current provide 3, 20, and 200 Hz bandwidth settings; these are filter bandwidths, not sample rates.

Instrument conversion speed and the application's target interval are independent. Actual rate depends on range, auto-zero, filtering, serial settings, instrument conversion, and communication. During acquisition, a speed change applies only after instrument readback confirms it. A target interval of zero means no additional host delay, not zero instrument or serial latency.

**Initialize parameters** restores the software defaults for all six measurement modes, clears their saved baselines, and sets the target interval to one second. It preserves serial communication settings and the current output directory. The button changes software preferences only; it does not reset or configure the instrument. Stop acquisition before initializing. To apply the selected mode to a connected meter, click **Apply instrument parameters** separately.

### 4. Acquisition and data files

Enter an experiment name and output directory, then start a single reading or continuous acquisition. Continuous acquisition can be paused, resumed, or stopped. Stop a run before changing modes; the next run uses a separate timestamped directory.

The real backend configures the measurement and performs one `READ?` per point, followed by a range query. It does not use `INIT/FETCh` batches. Timestamps are host-side: the CSV records the time before `READ?`, and `read_duration_s` records the read and range-query communication period. The instrument provides no per-reading hardware timestamp, so the exact sampling instant is unknown. Failed attempts retain a sequence and status with an empty value, never a fabricated zero.

Each experiment folder contains `measurements.csv`, `config.json`, `events.csv`, and `run.log`. `config.json` preserves the raw instrument identity and IDN revision field, the semantic firmware version only when the instrument supplies one, separately recorded front-panel OutG/InG software versions when entered, and the source selected for the 400 mA check. Measurement rows preserve raw instrument text separately from the SI value and include host local/UTC timestamps, elapsed time, communication duration, mode, range, NPLC or AC bandwidth, filter/zero state, and error status. CSV is UTF-8 with BOM. Acquisition and file writing run in a worker thread; rows are flushed as they arrive. The graph retains at most 100,000 display points. If the GUI thread stalls, sample notifications are coalesced and at most the latest 10,000 pending display points are kept; the SCPI display-log mailbox also stays bounded at 5,000 lines and reports when earlier display entries are dropped. The curve may skip intermediate display points while the CSV and full-run statistics remain complete.

### 5. Analysis, events, and history

The primary desktop window shows the live reading, acquisition status, controls, and one plot. The status-bar **Settings & Analysis** button opens a separate window for serial, instrument, experiment, statistics, baseline, and event settings. Plots support Y-axis wheel zoom, Shift+wheel X-axis zoom, Ctrl+wheel zoom of both axes, and Alt+wheel Y zoom anchored at zero. Drag to pan and use **Auto scale** to fit the current data. A reference can be entered, taken from the current reading, or calculated from the cached plot mean. Relative change is displayed as ΔV/V₀, ΔI/I₀, or ΔR/R₀ when the reference exceeds the configured threshold. Event notes, pause/resume actions, and reference changes are saved to `events.csv` and marked on the plot.

Historical CSV loading is read-only. Matching event markers are restored where available. For files over 100,000 points, the graph uses a uniformly downsampled view while statistics scan the full source file in the background; the original CSV is unchanged.

### 6. Mock and verification limits

Mock mode supports six-mode simulated readings, step response/recovery, long-term drift, selected timeouts, and simulated disconnects. Check the visible MOCK indicator and `simulation: true` in `config.json`. Mock workflows are not hardware tests. See the [SCPI reference](scpi-reference.md), [hardware verification notes](hardware-validation.md), and [known limitations](known-limitations.md).

### 7. Local browser interface

Start the browser interface with `python -m fluke8845a.webapp` or `run_web.bat`. It listens only on `127.0.0.1:8765`, uses no remote pages or CDN, and shares the instrument driver, acquisition worker, and per-reading recorder with the desktop application. The main page keeps the reading, acquisition state, controls, and one large plot; **Settings & Analysis** opens a separate dialog. It supports all six modes, wheel/modifier zoom, auto scale, statistics, reference changes, event logging, CSV history, and PNG export.

Starting the browser server does not open a serial port. Disconnect the desktop app from the meter before connecting the browser interface because Windows cannot open the same COM port from two applications simultaneously. Operators must still verify wiring, terminals, fuses, and circuit state for current and resistance measurements. **Exit service** stops acquisition, finishes pending writes, and closes the serial connection. Mock readings remain simulated.
