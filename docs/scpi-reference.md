# SCPI 命令与 8845A 限制依据 | SCPI Reference

[简体中文](#简体中文) | [English](#english)

## 简体中文

下表列明本程序采用的命令与显示条件及其依据。页码同时区分PDF页与印刷页。Fluke 手册在研发过程中作为参考；出于版权与发布范围考虑，本仓库不再分发手册 PDF。

| 软件行为或事实 | 依据位置 |
|---|---|
| RS-232出厂为9600、无校验、8位、1停止位、无流控；所有设置可配；波特率1200–230400；校验/数据位组合、停止位、流控和CR/LF/CRLF | `Fluke 8845A/8846A Programmers Manual, Rev. 3`：PDF页16–17，印刷页6–7 |
| COMPUTER/TERMINAL方式、RS-232 `SYST:REM`/`SYST:LOC`、`*IDN?` 安装测试及SCPI终止符；TERMINAL会回显命令/返回提示符，COMPUTER不回显/不返回提示符 | 同上：PDF页18–19和28，印刷页8–9和18；软件据命令回显识别并提示切换Computer |
| `MEASure?` 是配置预设后测量，等价于CONFigure + READ?；`CONFigure`本身不触发，须后跟READ?或INIT/FETCh；`READ?`在下一触发条件满足时返回，且读取每次响应后再继续 | 同上：PDF页48–52，印刷页38–42 |
| 六种量测的CONF function；D/SENS:FILT/NPLC、AC带宽3/20/200、ZERO:AUTO、ROUT:TERM?、SYST:ERR? | 同上：PDF页41–47，印刷页31–37；具体量测配置为PDF页42–44、印刷页32–34 |
| 8845A与8846A区分；DCV范围100 mV至1000 V，ACV最高750 V；电阻8845A从100 Ω档起（10 Ω为8846A专属） | `Fluke 8845A/8846A Extended Specifications`：PDF页3–6，印刷页2–5；型号专属能力摘要PDF页1 |
| 电流范围、400 mA固件条件、前面板1/3/10 A条件、负担电压、保险丝、400 mA连续上限；8845A直流电流从100 µA档起、交流电流从10 mA档起，交流100 µA/1 mA档仅8846A支持 | 同上：PDF页7–9，印刷页6–8；电流端子和保险丝说明亦见中文用户手册PDF页73–74、印刷页4-9至4-10 |
| NPLC可选0.02、0.2、1、10、100（用于DCV/DCI/电阻）；交流滤波带宽档；速率数字仅在典型的Auto Zero off、0延迟、显示关闭、固定量程等条件下成立，RS-232速率另受波特率限制 | 同上：PDF页12，印刷页11；编程手册PDF页42–43，印刷页32–33 |
| 电流必须串联；≤400 mA使用独立mA/400 mA与LO插孔，>400 mA至10 A使用独立10 A与LO插孔；mA/10 A输入按仪器面板选择，过流会熔断保险丝；电阻2线/4线分别使用HI/LO和SENSE | `Fluke 8845A/8846A Users Manual (Chinese)`：PDF页73–76，印刷页4-9至4-12（图4-4/4-5及mA/10A软键选择步骤）；保险丝说明及增补 `Fluke 8845A/8846A Users Manual Supplement`：PDF页2，印刷页1 |
| 交流读数为RMS；真有效值AC耦合；ACV范围3Hz–300kHz、ACI范围3Hz–10kHz，各有慢/中/快三档滤波 | `Fluke 8845A/8846A Extended Specifications`：PDF页4–5和8–9，印刷页3–4和7–8；交流滤波误差修订见用户手册补遗PDF页3–4 |
| 8845A具有10,000点内部存储；TrendPlot会自动合并时间段并保留min/max，不是逐点原始下载。应用按逐点CSV独立保存，不使用TrendPlot压缩 | 扩展规格PDF页2；`Fluke Data Logging and TrendPlot Application Note`：PDF页2–3，特别是第3页“How to Trendplot, and when to collect raw data” |

## 实际写入命令

连接：`*IDN?` 识别后写 `SYST:REM`；收到/检查仪器错误队列。正常断开写 `SYST:LOC`。

功能配置用 `CONF:VOLT:DC`、`CONF:VOLT:AC`、`CONF:CURR:DC`、`CONF:CURR:AC`、`CONF:RES`、`CONF:FRES`。设置自动量程用 `SENS:<function>:RANG:AUTO ON`；固定量程用 `...:RANG:AUTO OFF` 后写 `...:RANG <SI>`。仅DCV/DCI/RES/FRES写 `...:NPLC`；ACV/ACI写 `SENS:DET:BAND <3|20|200>`；受支持的直流模式才写 `SENS:<function>:FILT:STAT ON|OFF`。自动零点写 `ZERO:AUTO ON|OFF`。每次数据以单点 `READ?` 触发，之后读取实际 `...:RANG?` 并立即完整解析响应。

程序没有配置电容、RTD温度、外部电源输出或电压/电流同步双通道功能。若 `*IDN?` 返回8846A、其它型号或格式异常，连接会被拒绝。测试/Mock不提供实际电表精度保证。

**电流插孔边界：**官方用户手册将mA/400 mA和10 A画为两个独立前面板插孔，并要求按预期电流选择相应输入。编程手册的`ROUT:TERM?`只返回`FRON`或`REAR`；本程序另回读测量量程，但没有可据以确认红表笔实际插孔的状态。因此界面依选定量程提示mA/10 A接线，最终插孔和仪器面板输入选择仍须由操作者核对。这是对两个官方资料中端子图和SCPI查询范围的保守处理。

## 重要保守处理

- 模式参数先按手册的离散枚举验证。仪器错误队列非零时配置失败并中止，不继续保存看似有效的读数。
- 400 mA的DCI/ACI分别检查资料所列固件门槛；未识别固件时禁用固定档及可能跨入该档的电流自动量程。
- 1/3/10 A档按规格仅在前面板端子显示可用；后端子下固定档被禁用且电流自动量程被阻止。
- `READ?`的时间戳是PC写命令前时间，仪器转换并无逐样本硬件时间戳。读数周期与range-query耗时一并保存在`read_duration_s`。并未使用资料中有额外条件的典型高速度作为保证。
- 用户设置的串口响应超时是普通SCPI命令的下限。程序按NPLC/自动零点或交流带宽为`READ?`增加主机等待时间；这是保守通信保护，不是Fluke转换时间保证。若响应不完整，软件记录一条空值超时行后停止采集并关闭串口，避免后续请求误读迟到数据。

## English

This reference maps application behavior to the Fluke 8845A documentation consulted during development. Page numbers distinguish PDF pages from printed pages. The manuals are not redistributed with this repository.

### Documentation basis

| Topic | Fluke reference |
|---|---|
| RS-232 defaults and configurable serial parameters | *8845A/8846A Programmers Manual*, Rev. 3, PDF pp. 16–17 (printed pp. 6–7) |
| COMPUTER/TERMINAL modes, `SYST:REM`, `SYST:LOC`, `*IDN?`, and terminators | Same manual, PDF pp. 18–19, 28 (printed pp. 8–9, 18) |
| `MEASure?`, `CONFigure`, `READ?`, `INIT/FETCh`, and trigger behavior | Same manual, PDF pp. 48–52 (printed pp. 38–42) |
| Six measurement functions, range, NPLC, filter, bandwidth, auto-zero, terminal and error queries | Same manual, PDF pp. 41–47 (printed pp. 31–37) |
| 8845A/8846A differences; voltage and resistance ranges | *Extended Specifications*, PDF pp. 1–6 (printed pp. 2–5) |
| Current ranges, 400 mA firmware conditions, front-terminal 1/3/10 A ranges, burden voltage and fuses | Same specifications, PDF pp. 7–9; Chinese *Users Manual*, PDF pp. 73–74 |
| Typical rate conditions, NPLC and AC bandwidth | Same specifications, PDF p. 12; programmers manual, PDF pp. 42–43 |
| Current-series wiring, jacks, fuses, and 2-/4-wire resistance wiring | Chinese *Users Manual*, PDF pp. 73–76 (printed pp. 4-9–4-12), and its supplement, PDF p. 2 |
| RMS readings, AC coupling, AC ranges and filter bands | *Extended Specifications*, PDF pp. 4–5, 8–9, and user-manual supplement, PDF pp. 3–4 |
| Internal memory and TrendPlot behavior | *Extended Specifications*, PDF p. 2; *Data Logging and TrendPlot Application Note*, pp. 2–3 |

### Commands sent by the application

After `*IDN?` identification, connection sends `SYST:REM` and checks the instrument error queue. Normal disconnect sends `SYST:LOC`. Function setup uses `CONF:VOLT:DC`, `CONF:VOLT:AC`, `CONF:CURR:DC`, `CONF:CURR:AC`, `CONF:RES`, or `CONF:FRES`. Autorange uses `SENS:<function>:RANG:AUTO ON`; fixed range uses `...:RANG:AUTO OFF` followed by `...:RANG <SI>`. Only DCV, DCI, RES, and FRES use `...:NPLC`. ACV/ACI set `SENS:DET:BAND <3|20|200>`. Supported DC modes set `SENS:<function>:FILT:STAT ON|OFF`; auto-zero uses `ZERO:AUTO ON|OFF`. Each point uses `READ?`, followed by a query of the actual range and complete response parsing.

The application does not configure capacitance, RTD temperature, source output, or synchronous dual voltage/current channels. A response identifying an 8846A, another model, or malformed identity is rejected. Tests and Mock mode do not establish real-meter accuracy.

### Conservative implementation boundaries

- Parameters are validated against documented discrete values. A non-zero instrument error queue aborts configuration and acquisition.
- The 400 mA DCI/ACI ranges check documented firmware thresholds. If firmware cannot be identified, the fixed range and potentially crossing autorange are disabled.
- 1/3/10 A ranges are exposed only when front-panel terminals are confirmed; rear-terminal operation blocks these fixed ranges and current autorange.
- `READ?` timestamps are host timestamps taken before sending the command. The meter supplies no hardware timestamp per sample; `read_duration_s` records the read and range-query interval. Published typical maximum speeds are not treated as guarantees.
- The configured serial timeout is the floor for ordinary SCPI commands. The host allows a longer `READ?` response window based on NPLC/autozero or the selected AC bandwidth; this is a conservative host timeout, not a Fluke conversion-time guarantee. An incomplete `READ?` response is saved as a blank timeout attempt, then acquisition stops and the serial session closes before another query can consume a delayed response.
- The manual shows separate mA/400 mA and 10 A jacks. `ROUT:TERM?` reports only FRONT/REAR, and range readback cannot identify the physical red jack. The operator must verify the lead and front-panel input selection.

