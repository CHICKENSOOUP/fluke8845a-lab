# SCPI 命令与 8845A 限制依据 | SCPI Reference

[简体中文](#简体中文) | [English](#english)

## 简体中文

下表列明本程序采用的命令与显示条件及其依据。页码同时区分PDF页与印刷页。Fluke 手册在研发过程中作为参考；出于版权与发布范围考虑，本仓库不再分发手册 PDF。

| 软件行为或事实 | 依据位置 |
|---|---|
| RS-232出厂为9600、无校验、8位、1停止位、无流控；有效数据位/校验组合仅8N、7E、7O；支持波特率1200–230400；终止符可用CR/LF/CRLF | `Fluke 8845A/8846A Programmers Manual, Rev. 3`：PDF页16–17，印刷页6–7 |
| COMPUTER/TERMINAL方式、RS-232 `SYST:REM`/`SYST:LOC`、`*IDN?` 安装测试及SCPI终止符；8845A/8846A模式的`*IDN?`第四字段可能是日期/时间修订标记或用户自定义内容，不能作为OutG SW证据；`SYST:VERS?`只返回SCPI命令版本；RS-232收发前先发送 `SYST:REM` | 同上：PDF页18–19、28、44、46、70和76，印刷页8–9、18、34、36、60和66；第76页要求先发送 `SYS:REM`，以避免不可预测操作 |
| `MEASure?` 是配置预设后测量，等价于CONFigure + READ?；`CONFigure`本身不触发，须后跟READ?或INIT/FETCh；`READ?`在下一触发条件满足时返回，且读取每次响应后再继续 | 同上：PDF页48–52，印刷页38–42 |
| `CONF` 后、单点 `READ?` 前核对 `TRIG:SOUR?`、`TRIG:COUN?`、`SAMP:COUN?`：仅接受 `IMM`、1、1；非预期回读时拒绝继续配置/采集 | 同上：Table 17，PDF页47（印刷页37）；回读值PDF页67–68（印刷页57–58）；`CONF`预设Table 18，PDF页49–51（印刷页39–41）；BUS死锁错误-214，PDF页106（印刷页96） |
| 六种量测的CONF function；D/SENS:FILT/NPLC、AC带宽3/20/200、ZERO:AUTO、ROUT:TERM?、SYST:ERR? | 同上：PDF页41–47，印刷页31–37；具体量测配置为PDF页42–44、印刷页32–34 |
| DCV 输入阻抗自动选择：`INP:IMP:AUTO ON|OFF` 和 `INP:IMP:AUTO?`；OFF 为所有量程 10 MΩ，ON 时仅 100 mV、1 V、10 V 量程大于 10 GΩ；`CONF` 预设为 OFF | 编程手册PDF页60–61、印刷页50–51；默认OFF见Table 18，PDF页51、印刷页41；扩展规格PDF页3 |
| `CONF` 的 DC Digital Filter 预设为ON；DCV/DCI/2W/4W 的数字滤波命令见编程手册。用户手册明确 OutG SW 早于2.0时数字滤波功能不可用。对面板核实低于2.0的版本，软件跳过滤波命令和查询，并报告“不支持”；NPLC仍按所选值写入。对已核实>=2.0和未知版本，软件发送 `FILT:DIG OFF` 并查询确认后才写 NPLC；未知版本若命令报错或回读异常则拒绝写入NPLC。实时调速采用相同规则。滤波ON不开放，因为手册说明平均次数随功能/量程变化，却未给出最大 `READ?` 响应时长 | 编程手册Table 13：PDF页42–43、印刷页32–33；数字滤波命令：PDF页58–60、印刷页48–50；默认ON：Table 18，PDF页51、印刷页41；OutG SW <2.0：用户手册Rev.3 PDF页47、印刷页3-3；平均次数说明：用户手册PDF页75、印刷页4-5（中文PDF页43/印刷页3-3、PDF页68/印刷页4-4） |
| 8845A与8846A区分；DCV范围100 mV至1000 V，ACV最高750 V；电阻8845A从100 Ω档起（10 Ω为8846A专属） | `Fluke 8845A/8846A Extended Specifications`：PDF页3–6，印刷页2–5；型号专属能力摘要PDF页1 |
| 电流范围、400 mA固件条件、前面板1/3/10 A条件、负担电压、保险丝、400 mA连续/短时上限和交流400 mA峰值因数限制；8845A直流电流从100 µA档起、交流电流从10 mA档起，交流100 µA/1 mA档仅8846A支持 | 同上：PDF页7–9，印刷页6–8；前后端子及“后面板不能测10 A电流”见中文用户手册PDF页50（印刷页3-7），电流端子和保险丝说明亦见PDF页73–74、印刷页4-9至4-10 |
| NPLC可选0.02、0.2、1、10、100（用于DCV/DCI/电阻）；交流滤波带宽档；速率数字仅在典型的Auto Zero off、0延迟、显示关闭、固定量程等条件下成立，RS-232速率另受波特率限制 | 同上：PDF页12，印刷页11；编程手册PDF页42–43，印刷页32–33 |
| 电流必须串联；≤400 mA使用独立mA/400 mA与LO插孔，>400 mA至10 A使用独立10 A与LO插孔；mA/10 A输入按仪器面板选择，过流会熔断保险丝；电阻2线/4线分别使用HI/LO和SENSE | `Fluke 8845A/8846A Users Manual (Chinese)`：PDF页73–76，印刷页4-9至4-12（图4-4/4-5及mA/10A软键选择步骤）；保险丝说明及增补 `Fluke 8845A/8846A Users Manual Supplement`：PDF页2，印刷页1 |
| 交流读数为RMS；真有效值AC耦合；ACV范围3Hz–300kHz、ACI范围3Hz–10kHz，各有慢/中/快三档滤波 | `Fluke 8845A/8846A Extended Specifications`：PDF页4–5和8–9，印刷页3–4和7–8；交流滤波误差修订见用户手册补遗PDF页3–4 |
| 8845A具有10,000点内部存储；TrendPlot会自动合并时间段并保留min/max，不是逐点原始下载。应用按逐点CSV独立保存，不使用TrendPlot压缩 | 扩展规格PDF页2；`Fluke Data Logging and TrendPlot Application Note`：PDF页2–3，特别是第3页“How to Trendplot, and when to collect raw data” |

## 实际写入命令

连接：串口打开后先写 `SYST:REM`，再查询 `*IDN?` 并核验 8845A 身份，随后读取仪器错误队列；若身份识别失败，软件会尽力写 `SYST:LOC` 后关闭串口。该先 REM 后收发的顺序遵循编程手册 PDF 第76页（印刷第66页）。仪器设置区允许在停止采集时切换 `SYST:LOC`（Local面板操作）与 `SYST:REM`（Computer远程操作）；切回Computer后重新回读当前模式及参数。即使软件缓存已显示Computer，用户再次请求Computer也会重发 `SYST:REM` 并重新核对仪表状态。真实连接成功后，桌面 Worker 每10秒在停止或暂停状态下查询 `*IDN?` 检查连接和8845A身份；连续采集运行时不插入探测查询，LOCAL 与 MOCK 状态不探测。探测超时、身份变化或串口错误会关闭连接并更新状态；暂停中的实验会以失败状态结束并保存。正常断开写 `SYST:LOC`。仪表RS-232菜单的COMPUTER协议设置与软件的Local/Computer控制权分开：前者须在仪表面板设置，后者使用这两个SCPI命令。

功能配置用 `CONF:VOLT:DC`、`CONF:VOLT:AC`、`CONF:CURR:DC`、`CONF:CURR:AC`、`CONF:RES`、`CONF:FRES`。设置自动量程用 `SENS:<function>:RANG:AUTO ON`；固定量程用 `...:RANG:AUTO OFF` 后写 `...:RANG <SI>`。仅DCV/DCI/RES/FRES写 `...:NPLC`；ACV/ACI写 `SENS:DET:BAND <3|20|200>`。DCV 显式写 `INP:IMP:AUTO ON|OFF` 并查询回读；该模式依据手册在所有量程默认关闭为10 MΩ，启用后仅100 mV、1 V、10 V量程高于10 GΩ。对面板核实的 OutG SW <2.0，用户手册称数字滤波功能不可用，故四种直流模式不发送滤波命令或查询，并在状态中标记“不支持”，仍可设置NPLC。对已核实>=2.0及未知版本，`CONF` 后发送 `SENS:<function>:FILT:DIG OFF`、检查错误队列并查询状态；只有回读确认为OFF后才写NPLC。未知版本的命令错误、查询超时或回读异常会阻止NPLC写入。实时调速采用相同规则。数字滤波开启在软件中禁用，因为官方手册未给出最大 `READ?` 响应时长。400 mA电流量程有独立固件门槛。自动零点写 `ZERO:AUTO ON|OFF`。每次数据以单点 `READ?` 触发，之后读取实际 `...:RANG?` 并立即完整解析响应。

程序没有配置电容、RTD温度、外部电源输出或电压/电流同步双通道功能。若 `*IDN?` 返回8846A、其它型号或格式异常，连接会被拒绝。测试/Mock不提供实际电表精度保证。

**电流插孔边界：**官方用户手册将mA/400 mA和10 A画为两个独立前面板插孔，并要求按预期电流选择相应输入。编程手册的`ROUT:TERM?`只返回`FRON`或`REAR`；本程序另回读测量量程，但没有可据以确认红表笔实际插孔的状态。因此界面依选定量程提示mA/10 A接线，最终插孔和仪器面板输入选择仍须由操作者核对。这是对两个官方资料中端子图和SCPI查询范围的保守处理。

### ZERO与量程回读

`ZERO:AUTO ONCE`启动一次内部零点测量，`ZERO:AUTO?`只回读自动零点模式开关，并不返回零点测量值。软件执行ZERO后显示命令反馈和参数配置回读；配置回读不能证明零点测量值。自动量程下的`SENS:<function>:RANG?`作为独立档位快照显示，不覆盖“自动量程”配置，且可能随输入变化。依据《Programmers Manual Rev. 3》PDF第60页（印刷第50页）；使用模拟滤波时 ZERO 需匹配量程、NPLC和触发条件，见《Users Manual Rev. 3》附录D-1（PDF第99页）。

## 重要保守处理

- 模式参数先按手册的离散枚举验证。仪器错误队列非零时配置失败并中止，不继续保存看似有效的读数。
- `CONF` 后、单点 `READ?` 前只读验证触发源为 `IMM`、触发数和样本数均为1。手册将这些值列为 `CONF` 预设；若仪表回读不符，软件清除缓存测量模式并拒绝继续配置/采集，不另发触发写命令。`READ?` 会等待触发条件，BUS与EXT可能等待外部事件；本软件只支持单点立即触发采集。
- `LOCAL`面板控制时软件不发送配置、诊断、ZERO或采集命令；采集停止后可切回`COMPUTER`，并从仪表重新读取面板操作后当前的模式和参数。
- 400 mA的DCI/ACI分别检查资料所列固件门槛；`*IDN?`第四字段可能是用户自定义内容，即使可解析出点分数字也不用于量程判断。只使用按序列号保存的人工核对 OutG SW。InG SW和IDN数字候选只作元数据记录。只有 OutG SW 可解析且明确低于门槛时，才禁用固定400 mA档；未知或无法解析不触发拒绝。其它固定档不受此固件门槛影响；若人工核对的 OutG SW 明确过旧，自动量程会被阻止，因为仪器可能进入400 mA档。`SYST:VERS?`返回SCPI命令版本，不是仪器软件版本。
- 固定1/3/10 A档仅在前面板端子显示可用：DCI的1 A标注为前面板专用，3 A属于10 A量程，且用户手册说明后面板不能测量10 A；ACI则直接将1/3/10 A标注为前面板专用。后端子下这些固定档被禁用，可能进入这些档位的电流自动量程也被阻止。400 mA的固件门槛与此端子限制分开判断。
- 原有`timestamp_local`/`timestamp_utc`保留主机`READ?`请求开始时间；`host_read_request_start_*`与`host_read_response_end_*`另存请求开始和收到响应/抛错边界的主机墙钟、单调时钟。响应结束在`READ?`查询获得结果或抛出错误时记录，不包括后续量程回读；`read_duration_s`也只覆盖该请求/响应窗口。`actual_interval_s`与桌面/Web遥测均按相邻有效响应结束单调时钟计算，异常行间隔留空。仪器没有逐样本硬件时间戳。并未使用资料中有额外条件的典型高速度作为保证。
- 用户设置的串口响应超时是普通SCPI命令的下限。程序按NPLC/自动零点或交流带宽为`READ?`增加主机等待时间；这是保守通信保护，不是Fluke转换时间保证。若响应不完整，软件记录一条空值超时行后停止采集并关闭串口，避免后续请求误读迟到数据。

## English

This reference maps application behavior to the Fluke 8845A documentation consulted during development. Page numbers distinguish PDF pages from printed pages. The manuals are not redistributed with this repository.

### Documentation basis

| Topic | Fluke reference |
|---|---|
| RS-232 defaults and configurable serial parameters | *8845A/8846A Programmers Manual*, Rev. 3, PDF pp. 16–17 (printed pp. 6–7) |
| COMPUTER/TERMINAL modes, `SYST:REM`, `SYST:LOC`, `*IDN?`, terminators, and the requirement to enter REMOTE before RS-232 traffic; the fourth `*IDN?` field may be a date/time revision or user-defined content and is not accepted as OutG SW evidence, while `SYST:VERS?` returns the SCPI command version | Same manual, PDF pp. 18–19, 28, 44, 46, 70, 76 (printed pp. 8–9, 18, 34, 36, 60, 66); p. 76 instructs sending `SYS:REM` before RS-232 send/receive |
| `MEASure?`, `CONFigure`, `READ?`, `INIT/FETCh`, and trigger behavior | Same manual, PDF pp. 48–52 (printed pp. 38–42) |
| Six measurement functions, range, NPLC, filter, bandwidth, auto-zero, terminal and error queries | Same manual, PDF pp. 41–47 (printed pp. 31–37) |
| 8845A/8846A differences; voltage and resistance ranges | *Extended Specifications*, PDF pp. 1–6 (printed pp. 2–5) |
| Current ranges, 400 mA firmware conditions, front-terminal 1/3/10 A ranges, burden voltage and fuses | Same specifications, PDF pp. 7–9; Chinese *Users Manual*, PDF p. 50 (printed p. 3-7) and PDF pp. 73–74 |
| Typical rate conditions, NPLC and AC bandwidth | Same specifications, PDF p. 12; programmers manual, PDF pp. 42–43 |
| DCV input impedance `INP:IMP:AUTO` behavior and preset | *Programmers Manual*, Rev. 3, PDF pp. 51, 60–61; *Extended Specifications*, PDF p. 3 |
| Digital-filter commands and preset; the Users Manual explicitly states the feature is unavailable below OutG SW 2.0 | *Programmers Manual*, Rev. 3, Table 13, PDF pp. 42–43; filter commands, PDF pp. 58–60; preset, Table 18, PDF p. 51; *Users Manual*, Rev. 3, English PDF pp. 47, 75 and Chinese PDF pp. 43, 68 |
| Current-series wiring, jacks, fuses, and 2-/4-wire resistance wiring | Chinese *Users Manual*, PDF pp. 73–76 (printed pp. 4-9–4-12), and its supplement, PDF p. 2 |
| RMS readings, AC coupling, AC ranges and filter bands | *Extended Specifications*, PDF pp. 4–5, 8–9, and user-manual supplement, PDF pp. 3–4 |
| Internal memory and TrendPlot behavior | *Extended Specifications*, PDF p. 2; *Data Logging and TrendPlot Application Note*, pp. 2–3 |

### Commands sent by the application

After opening the serial port, connection sends `SYST:REM` before querying `*IDN?`, as required by the manual, then checks the instrument error queue after validating 8845A identity. If identification fails after REMOTE was attempted, the application makes a best-effort `SYST:LOC` before closing the port. When acquisition is stopped, the instrument settings panel can switch `SYST:LOC` (Local/front-panel control) and `SYST:REM` (Computer/remote control); switching back to Computer re-reads the active function and settings. A repeated Computer request re-sends `SYST:REM` and refreshes the instrument state even if the UI cache already says Computer. While a real connection is idle or paused in Computer mode, the worker queries `*IDN?` every 10 seconds to confirm the connection and 8845A identity. It skips probes during active continuous acquisition and in LOCAL or MOCK mode. A probe timeout, identity change, or serial error closes the connection and updates status; a paused experiment is finalized as failed. Normal disconnect sends `SYST:LOC`. The meter's RS-232 COMPUTER protocol selection is separate from this Local/Computer control state and must be set on the instrument. Function setup uses `CONF:VOLT:DC`, `CONF:VOLT:AC`, `CONF:CURR:DC`, `CONF:CURR:AC`, `CONF:RES`, or `CONF:FRES`. Autorange uses `SENS:<function>:RANG:AUTO ON`; fixed range uses `...:RANG:AUTO OFF` followed by `...:RANG <SI>`. Only DCV, DCI, RES, and FRES use `...:NPLC`. ACV/ACI set `SENS:DET:BAND <3|20|200>`. DCV explicitly sets and reads back `INP:IMP:AUTO`; ON uses >10 GΩ only at 100 mV, 1 V and 10 V ranges, while OFF is 10 MΩ at every range. For DCV/DCI/RES/FRES, a panel-verified OutG SW below 2.0 means the documented filter feature is unavailable, so the application skips filter commands/queries, reports unsupported, and continues to NPLC. For verified >=2.0 or unknown versions, it sends `SENS:<function>:FILT:DIG OFF`, checks the error queue, then queries `SENS:<function>:FILT:DIG?` before writing NPLC. Unknown versions retain fail-closed behavior if the command errors or OFF cannot be confirmed. Live NPLC changes use the same rule. Filter ON remains unavailable because the manuals give no maximum `READ?` response time. Auto-zero uses `ZERO:AUTO ON|OFF`. Each point uses `READ?`, followed by a query of the actual range and complete response parsing.

The application does not configure capacitance, RTD temperature, source output, or synchronous dual voltage/current channels. A response identifying an 8846A, another model, or malformed identity is rejected. Tests and Mock mode do not establish real-meter accuracy.

### Conservative implementation boundaries

- Parameters are validated against documented discrete values. A non-zero instrument error queue aborts configuration and acquisition.
- After `CONF` and before one-point `READ?`, the application reads `TRIG:SOUR?`, `TRIG:COUN?` and `SAMP:COUN?` and requires `IMM`, 1 and 1. The manual lists these as `CONF` presets. A mismatch clears the cached mode and aborts further configuration/acquisition without sending separate trigger-setting commands; the application supports one-point immediate-trigger acquisition only.
- In `LOCAL` panel-control mode, the application sends no configuration, diagnostic, ZERO, or acquisition commands. After panel operation, switching back to `COMPUTER` re-reads the current function and settings.
- The fixed 400 mA DCI/ACI ranges enforce the documented OutG SW threshold only when manually recorded OutG SW is parseable and explicitly below the threshold. The `*IDN?` revision field may be user-defined, so even a parseable number is not used as firmware evidence. InG SW and numeric IDN candidates are metadata only. Unknown or unparseable OutG SW does not block the range; other fixed ranges are unaffected by this firmware gate. Autorange is blocked only when a known old OutG SW could let the meter enter 400 mA. `SYST:VERS?` returns the SCPI command version, not instrument software. Manual panel entries are distinguished from SCPI data in experiment metadata.
- Fixed 1/3/10 A ranges are exposed only when front-panel terminals are confirmed: DCI marks 1 A front-only, 3 A is part of the 10 A range, and the user manual says rear terminals cannot measure 10 A; ACI marks 1/3/10 A front-only. Rear operation blocks those fixed ranges and current autorange that may enter them. The 400 mA firmware threshold is checked independently.
- Existing `timestamp_local`/`timestamp_utc` retain the host `READ?` request-start time. `host_read_request_start_*` and `host_read_response_end_*` additionally store the host wall-clock and monotonic request/response boundaries. The response end is captured when the `READ?` query returns a response or raises, before any later range query; `read_duration_s` covers only this request/response window. `actual_interval_s` and desktop/Web telemetry use the difference between consecutive valid response-end monotonic timestamps; invalid rows have no interval. The meter supplies no hardware timestamp per sample. Published typical maximum speeds are not treated as guarantees.
- The configured serial timeout is the floor for ordinary SCPI commands. The host allows a longer `READ?` response window based on NPLC/autozero or the selected AC bandwidth; this is a conservative host timeout, not a Fluke conversion-time guarantee. An incomplete `READ?` response is saved as a blank timeout attempt, then acquisition stops and the serial session closes before another query can consume a delayed response.
- DCV automatic input impedance defaults OFF (10 MΩ); ON selects >10 GΩ only for 100 mV, 1 V and 10 V ranges. The application writes and verifies this setting only for DCV.
- Digital filtering is documented to average a function/range-dependent number of readings, but the supplied manual gives no count table or maximum response time. The feature is documented as unavailable below OutG SW 2.0. For a panel-verified version below 2.0, the application skips filter commands and reports unsupported while continuing to configure NPLC. For verified >=2.0 or unknown versions, it sends `FILT:DIG OFF`, checks the error queue, and reads `FILT:DIG?` before writing NPLC. Unknown versions retain fail-closed behavior if OFF cannot be confirmed. Live NPLC changes use the same rule. Enabling filter ON requires real-hardware timing validation and a bounded timeout design.
- The manual shows separate mA/400 mA and 10 A jacks. `ROUT:TERM?` reports only FRONT/REAR, and range readback cannot identify the physical red jack. The operator must verify the lead and front-panel input selection.
