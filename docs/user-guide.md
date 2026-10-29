# Fluke 8845A Lab 使用指南 | User Guide

[简体中文](#简体中文) | [English](#english)

## 简体中文

版本 2.1.3 · 中文 Windows 桌面应用

## 1. 启动与工作模式

v2.1.3 更新了桌面与网页模式切换动效，提供带应用图标的 Windows 桌面和 Web 快捷方式，并改用无控制台启动器。源码首次运行会准备 Python 环境；后续启动直接复用。桌面与 Web 保持同一六种测量模式和实时曲线布局。绘图缩放只改变视图，不修改原始读数。本次验证范围见[硬件验证说明](hardware-validation.md)。

- **真实仪器**：将仪器后面板设为 RS-232，并选择 8845A/8846A 指令集的 COMPUTER 协议模式。连接后本程序默认以 COMPUTER/REMOTE 控制仪表；仪器设置区可切换为 LOCAL 面板控制。连接时先发送 `SYST:REM`，再查询 `*IDN?`；只接受设备型号字段为 `8845A` 的 Fluke 仪器，不会重置仪器。若身份识别失败，程序会尽力发送 `SYST:LOC` 后关闭串口。先 REMOTE 再进行 RS-232 收发遵循编程手册 PDF 第76页（印刷第66页）的说明。

程序启动时扫描串口，也可在端口框直接输入 COM 号。默认值为 9600 baud、8 data bits、无校验、1 stop bit、无流控、CRLF 结束符、2秒普通命令响应超时。`READ?`会根据当前积分时间或交流滤波设置自动使用更长的主机等待上限；这只是避免过早超时的通信保护，不代表仪器的采样时间保证。手册中 RS-232 出厂参数包含 9600、8位、无校验、1停止位和无流控；EOL需按仪器面板设置。可配置选项包括手册列出的全部波特率、7/8数据位、无/奇/偶校验、1/2停止位、无/XON-XOFF/RTS-CTS流控以及CR/LF/CRLF结束符。真实仪器的读取超时（包括普通串口 `TimeoutError`）或读取过程中的通信异常会立即停止本次实验、保存当前空值失败行并关闭串口；软件不自动重试或续接旧实验。操作者需检查仪器/线缆，手动重新连接，并以新实验目录开始下一段采集，避免迟到响应被误当作后续样本。真实仪器连接在 COMPUTER 模式且停止或暂停采集时，软件每 10 秒查询一次 `*IDN?` 确认连接；连续采集期间不会插入探测查询，LOCAL 模式也不会探测。探测发现断线、响应超时或仪器身份变化时会关闭连接并更新状态；若实验处于暂停状态，软件会结束并保存为通信异常。

首次连接失败时，确认物理RS-232/USB转串口线、COM号、线缆类型、波特率和EOL。仪表菜单中的 RS-232 `COMPUTER` 协议模式与软件里的 `COMPUTER/LOCAL` 控制权是两项设置：协议模式须在仪表菜单选择；控制权可在连接窗口切换。`COMPUTER` 使用 `SYST:REM`，除仪表的 `LOCAL` 键外，其他面板按键会被禁用。选择 `LOCAL` 会发送 `SYST:LOC`，将控制权交给面板；软件停用测量、设置、诊断和ZERO，串口保持连接。面板操作完成后，在软件选择 `COMPUTER` 即可切回远程控制；软件会重新读取当前测量模式和参数。采集中不能切换控制权。若直接按仪表 `LOCAL` 导致 `+550,"Command not allowed in local"`，软件会保存异常行且不伪造读数，停止采集并关闭串口；重新连接可恢复远程控制。正常断开或关闭程序时会尝试发送 `SYST:LOC` 恢复本地控制。

## 2. 测量模式与安全接线

支持 **DCV、ACV、DCI、ACI、2W电阻、4W电阻**。每个模式单独记住量程、NPLC/交流带宽、适用滤波和自动零点设置；DCV 另可设置自动输入阻抗。设置存于当前 Windows 用户配置目录。无关参数显示“不适用”且不能修改。ACV/ACI显示真 RMS 有效值，不是峰值或波形采样数据。

切换测量模式时直接应用配置；开始真实采集或修改电流量程时，会弹出简短的接线确认窗口。接线要求依据官方中文《用户手册》第 4-4、4-9 至 4-11 页。软件不能观察被测电路、测试线、保险丝或插孔，这些检查必须由操作者完成：

- **电压**：电压 HI/LO 端子并联接入被测节点；核对电压等级和测量类别。
- **两线电阻**：被测电路断电，使用相应 HI/LO 端子。引线电阻会影响结果。
- **四线电阻**：被测电路断电；SOURCE/FORCE 线和 SENSE HI/LO 线接到正确端子。
- **电流**：必须先关闭被测电路电源，再将仪器串联接入回路。前面板的 mA/400 mA 与 10 A 是两个独立红色插孔：预期电流不超过 400 mA 时红表笔接 mA/400 mA、黑表笔接 LO；预期电流大于 400 mA 且不超过 10 A 时，红表笔接 10 A、黑表笔接 LO；两个红色电流孔不能同时连接。应用会按当前固定量程显示应选的插孔；自动量程时必须按最大预期电流选孔，并核对仪表显示的 mA/10 A 输入选择。绝不可将电流输入并联到电压源。

8845A 的 mA 输入有 440 mA 保险丝，10 A 输入有 11 A 保险丝；资料列出的允许值为 400 mA 连续、550 mA 最多2分钟后停1分钟。保险丝额定值不代表可连续测量值。**1 A、3 A、10 A固定量程只在前面板可用**：DCI 的规格表把 1 A 标为仅前面板；3 A 属于 10 A 量程，而用户手册说明后面板不能进行 10 A 电流测量；ACI 的规格表直接把 1/3/10 A 标为仅前面板。因此后面板会禁用这三档，并禁用可能进入这些档位的电流自动量程。400 mA 固定量程有独立的固件条件：DCI 要求 OutG SW 至少 2.0；ACI 扩展规格要求软件版本至少 1.0.700.18，但没有注明该版本对应 OutG SW 还是 InG SW。软件在 ACI 界面提示操作者核对仪表面板版本，不根据 OutG SW 或 InG SW 自动拒绝 ACI 400 mA 固定档或自动量程。两个门槛**不应用于** 10 mA、100 mA 或 10 A 等其它固定量程。8845A 的 ACI 不提供 100 µA 和 1 mA 档（这两档仅 8846A 支持）。编程手册说明 `*IDN?` 第四字段可由用户自定义；即使可解析为点分数字，也不能据此确认 OutG SW。`SYST:VERS?` 只返回 SCPI 命令版本，不读取 OutG/InG SW。提供的编程手册未记载读取 OutG/InG SW 的 SCPI 查询；用户手册说明可在仪表面板 `SYSTEM > VERSIONS + SN` 查看。按序列号保存的人工核对 OutG SW 只用于 DCI 400 mA 门槛；InG SW 和 IDN 数字候选只作元数据。仅当 OutG SW 可解析且明确低于 2.0 时，才禁用 DCI 固定 400 mA 档和可能进入该档的 DCI 自动量程；版本未知或无法解析不会触发拒绝。面板版本记录是人工提供的设备元数据，不是 SCPI 回读。

改变模式或固定量程不会移动测试线或切换物理插孔。手册列出的 mA/10 A 前面板输入选择应与实际表笔插孔一致。`ROUT:TERM?`只报告FRONT/REAR，量程回读也不能识别红表笔接在mA/400 mA孔还是10 A孔；软件无法替操作者验证。量程切换跨越400 mA分界时，请再次断电、检查端子和保险丝。

## 3. 测量配置

量程列表来源于8845A扩展规格，并以SI单位传给仪器：

| 模式 | 软件提供的范围 | 可用参数 |
|---|---|---|
| DCV | 100 mV、1 V、10 V、100 V、1000 V | NPLC 0.02、0.2、1、10、100；模拟滤波；自动零点；自动输入阻抗 |
| ACV | 100 mV、1 V、10 V、100 V、750 V | 交流滤波 3、20、200 Hz；自动零点 |
| DCI | 100 µA、1 mA、10 mA、100 mA、400 mA¹、1 A²、3 A²、10 A² | NPLC 0.02、0.2、1、10、100；模拟滤波；自动零点 |
| ACI | 10 mA、100 mA、400 mA³、1 A²、3 A²、10 A² | 交流滤波 3、20、200 Hz；自动零点 |
| 2W | 100 Ω、1 kΩ、10 kΩ、100 kΩ、1 MΩ、10 MΩ、100 MΩ | NPLC 0.02、0.2、1、10、100；模拟滤波；自动零点 |
| 4W | 100 Ω、1 kΩ、10 kΩ、100 kΩ、1 MΩ、10 MΩ、100 MΩ | NPLC 0.02、0.2、1、10、100；模拟滤波；自动零点 |

¹ DCI 的 400 mA 档要求 OutG SW ≥2.0；² 1/3/10 A 档仅限前面板端子（DCI 的 3 A 是 10 A 档的一部分，用户手册注明 10 A 电流测量不能使用后面板端子）；³ ACI 的 400 mA 档要求软件版本 ≥1.0.700.18，但官方资料未说明对应 OutG SW 还是 InG SW。软件提示操作者核对仪表面板版本，不根据 OutG/InG 字段自动拒绝 ACI 400 mA 固定档或自动量程。8845A 的 ACI 不含 100 µA、1 mA（8846A 专有）。只有 DCI 400 mA 固定档会在人工核对的 OutG SW 可解析且明确低于 2.0 时禁用；IDN数字候选不作为固件证据，版本未知不会误判为过旧。其他固定档不受 DCI 400 mA 固件门槛影响。

DCV 的自动输入阻抗默认关闭，表示 10 MΩ。启用后，8845A 只在 100 mV、1 V、10 V 量程使用大于 10 GΩ 的输入阻抗；100 V、1000 V 量程仍为 10 MΩ。自动量程时仪表按当前实际量程应用相应输入阻抗。该设置只出现在 DCV，配置后会查询并核对 `INP:IMP:AUTO?`，且写入实验配置和每条测量 CSV。

可打开自动量程，或取消勾选后选择固定量程。后端子下1 A、3 A和10 A固定量程仍不可用，可能进入这些档位的电流自动量程也会被阻止。DCI 自动量程在已核实 OutG SW 明确低于2.0时会被拒绝。ACI 的软件版本要求字段无法从 OutG/InG 记录自动判定；使用 ACI 400 mA 固定档或自动量程前，请核对仪表面板版本。按“应用仪器参数”后，软件写入SCPI命令并读取量程、功能、带宽、端子等可用状态；仪器错误队列非零或功能回读不匹配时不会启动实验。模拟滤波器只出现在DCV/DCI/电阻模式；NPLC只出现在这些直流积分模式。AC滤波档的3/20/200 Hz分别是慢/中/快三档截止频率选择，不是采样频率。

自动量程仍保持“自动量程”设置；界面另外显示最近一次仪器 `RANG?` 回读的档位快照，并提示它可能随输入改变。这个快照不是锁定的固定量程。

8845A 数字滤波仅适用于 DCV、DCI、两线电阻和四线电阻；软件只允许在 NPLC 10 或 100 启用（慢于 1 PLC）。`CONF` 会将数字滤波预设为 ON，因此配置时软件在 `CONF` 后先发送 `SENS:<function>:FILT:DIG OFF`，检查错误队列并回读 `...:FILT:DIG?`；确认 OFF 后才写量程和 NPLC。请求开启时，写 NPLC 后再发送 `SENS:<function>:FILT:DIG ON`，并再次检查错误队列和回读确认。已核实 OutG SW <2.0 时拒绝开启；版本未知时尝试命令并以错误队列和回读确认，未确认则配置失败。已核实版本低于 2.0 且请求关闭时，跳过滤波命令并继续配置 NPLC。平均读数数量随 DC 功能和量程变化，手册未给最大 `READ?` 响应时间；软件只有有限的主机端超时保护，不保证仪器时限。超时会保存空值失败记录、停止采集并关闭串口，不伪造读数。依据《Programmers Manual Rev. 3》Table 13（PDF pp. 42–43 / printed pp. 32–33）、数字滤波命令（PDF pp. 58–60 / printed pp. 48–50）、CONF 预设 Table 18（PDF p. 51 / printed p. 41）、READ?（PDF p. 52 / printed p. 42）；《Users Manual Rev. 3》OutG SW <2.0 限制（PDF p. 47 / printed p. 3-3），滤波适用速率及平均数量（PDF p. 75 / printed p. 4-5）。

**零点校正与校准、去皮不同。**将鼠标悬停在“自动零点校正”或“执行 ZERO 校正”上，可查看各自说明。“自动零点校正”让仪表按自动零点流程测量内部零点并补偿偏移，有助于稳定读数，但额外测量会降低读数速度；它不会把被测输入强制显示为0。“执行 ZERO 校正”发送 `ZERO:AUTO ONCE`，按 Fluke 编程手册的定义立即启动一次零点测量。此操作不是校准调整，也不是对当前被测物去皮。只能在停止采集后执行；若启用模拟滤波，量程、NPLC和触发条件须与 ZERO 测量要求匹配。

ZERO 完成后的桌面和 Web 状态显示命令反馈及配置回读；回读仅确认设置，不能当成零点测量值。自动量程还会显示该时刻的量程回读快照；它可能随输入改变。模拟滤波下须匹配量程、NPLC和触发条件，依据《Programmers Manual Rev. 3》PDF第60页（印刷第50页）的 `ZERO:AUTO ONCE` 定义和《Users Manual Rev. 3》附录D-1（PDF第99页）的 ZERO 条件说明。

**仪器测量速度和软件采样间隔是两项独立设置。** 在主界面选择自动或固定量程，在“测量参数”窗口设置 NPLC 或交流带宽、适用滤波和目标采样间隔；直流电压、直流电流及电阻使用 NPLC，交流电压和电流使用 3/20/200 Hz 带宽。停止采集后可点击“应用仪器参数”写入并等待仪器回读。实验启动、运行、暂停和保存收尾期间参数与目标间隔锁定，保存结束后才可修改。目标间隔为 0 表示读数后不额外等待；实际采样速度仍受量程、自动零点、滤波、仪器转换和串口通信影响。



“恢复默认设置”会将六种测量模式恢复为软件默认设置，并把目标采样间隔设为1秒；会清除各模式保存的基准值，但保留串口通信设置和当前保存目录。它只更改软件配置，不会向仪器发送复位或配置命令；若需把当前模式参数写入已连接的仪器，请随后单独点击“应用仪器参数”。采集时须先停止才能初始化。



启动时会弹出连接窗口。桌面主界面显示连接状态、六个模式按钮、测量控制与曲线。点击“连接仪器”打开连接窗口；连接成功后返回主界面。需要调整时可在空闲状态点击“测量参数”；原来的主导航和“设置与分析”入口已移除。NPLC 和交流带宽仅在适用模式中可用。

采集速率会受测量模式、量程、NPLC/交流带宽、自动零点、串口波特率和被测条件共同影响。请以当前实验中实际记录的时间戳和有效读数为准，不要把某一台设备或某次接线的速率当作准确度或通用性能保证。

主界面同时显示原始曲线和滑动平均曲线。“平均点数”可在采集中调整；均值使用最近 N 个连续有效显示点，起始不足 N 点时使用已有点。异常读数或显示丢点会中断并重新开始平均。曲线缓存最多保留最近 100,000 点；原始 CSV 逐点保存，不受平均计算影响。

## 4. 单次测量与连续采集

1. 启动软件，在连接窗口选择串口参数并连接仪器。
2. 在主界面选择直流电压、交流电压、直流电流、交流电流、两线电阻或四线电阻。切换模式前请自行检查接线；软件不会检测表笔和被测电路。
3. 在主界面选择量程。需要修改其他参数时，打开“测量参数”，应用参数并等待回读确认。实验自动保存在已有的保存目录；未设置过时默认为 Windows 文档目录下的 Fluke8845A_Experiments。
4. 点击“单次测量”或“开始连续采集”。连续采集可暂停、继续和停止；停止后等待保存结束，再调整参数或切换模式。

每次单次或连续测量自动生成带时间和模式的实验名称，并在保存根目录创建独立实验目录。目标采样间隔在“测量参数”窗口设置，范围为 0 至 86400 秒、精度为 0.001 秒；0 表示每次读数后不额外等待。目标间隔保存在当前 Windows 用户设置中，重启后仍保留。实际间隔受仪器转换和通信耗时影响，请以显示的实测间隔和 CSV 时间戳为准。



真实后端使用 `CONF:<function>` 配置，之后逐次 `READ?` 触发/读取，并在每点读取实际量程。它不会调用 `INIT/FETCh` 批次模式，因此不占用或压缩仪器内部缓存，也不会把缓存批次的主机读取时刻误当作每个样本的采样时刻。

CSV保留原有`timestamp_local`、`timestamp_utc`和`elapsed_s`字段，它们表示主机开始`READ?`请求的时间。新增`host_read_request_start_*`与`host_read_response_end_*`字段，分别记录主机请求开始和收到响应/请求抛错时的本地、UTC及单调时钟值；它们不是仪器内部采样时间戳。`read_duration_s`只计算这两个`READ?`边界之间的时间，不包括后续量程回读。`actual_interval_s`只为有效读数填写，计算相邻有效响应结束单调时钟的差；无效/超时尝试保持空值。桌面和Web的有效频率及区间统计使用相同的响应结束时钟口径。仪器没有逐点硬件时间戳，因此转换器真实采样时刻仍未知。每次失败尝试仍占一个序号，`value_si`留空并写明状态，不伪造零值。若`READ?`已经返回但后续量程回读失败，失败行仍保留该次`READ?`原文，同时保持`value_si`为空，不把它当作已验证的有效样本。

串口、触发/读数解析、CSV写入、统计累积均在专用采集线程完成。测量数据逐点追加并立即刷新文件。独立同步线程每秒检查 CSV 系统同步，即使采集线程正在等待仪器响应，也可同步已写入的记录；同步仍可能受文件系统负载影响。事件写入和正常停止/关闭时立即同步。任一次 CSV 同步失败都会保留为持久化故障；即使停止时重试成功，也不会写入干净的实验完成标记。停止或退出会等待正在执行的文件系统同步结束，再关闭数据文件。系统调用仍不能保证磁盘控制器在断电时已把缓存写入介质；同步耗时也会计入真实采样间隔。磁盘检查每5秒执行一次，当前使用10 MiB固定余量保护，不预测剩余可运行时长。暂停或停止会在当前同步串口读数结束后生效，响应延迟受仪器读数转换和串口超时影响。GUI绘图按小批次刷新，最多保留最近100,000个点在内存中。若GUI线程暂时卡顿，界面通知会合并，待处理显示数据最多保留最近10,000点；发生邮箱溢出时，采集状态卡会提示“曲线显示丢点，CSV 原始数据完整”并显示丢弃的曲线点数。该提示只表示实时曲线显示缓存有点被跳过；CSV和全程统计不会因此丢失。长实验全程min/max/平均值/样本标准差采用流式统计，不受绘图缓存长度影响。程序退出会请求停止当前采集、关闭文件和串口并尝试恢复仪器本地控制。

## 5. 分析、事件与历史数据

桌面新版精简了主界面入口；下文中基准、事件、历史加载和安全副本的操作说明适用于保留这些入口的 localhost 页面，桌面功能以当前可见控件为准。

- 桌面版普通滚轮缩放两轴，Shift+滚轮缩放时间轴，Ctrl+滚轮缩放纵轴。`Full（全部）` 显示从 0 到最新数据；`滚动跟随` 保留手动调整的时间窗口宽度并跟随最新数据，两者互斥。`纵轴自动` 根据当前可见时间窗口适配读数。手动纵轴缩放会关闭纵轴自动，横轴拖动不关闭滚动跟随。`保存曲线` 在后台导出两个 CSV：简洁文件包含时间、原始值、滑动平均值和单位，详细文件保留原始测量列、滑动平均值和点数；采集中导出的是当时已完整写入的数据快照。导出按保存时选定的点数对完整快照重新计算；原始实验 CSV 保持不变。导出保留原始文本，如果需要用表格软件打开，请按文本导入文本列。
- 基准可以手动输入，也可取最新读数、图上锁定点或当前图中缓存均值。Web 端可在 V/mV/µV、A/mA/µA、Ω/kΩ/MΩ/GΩ 间选择输入单位；内部计算和原始数据仍统一使用 SI 值。鼠标悬停只预览读数；在曲线上单击会锁定黄色圆圈标出的数据点，移动鼠标不会改变它。点击“锁定点设为基准”后，软件使用该固定点的读数。电压显示ΔV/V₀，电流显示ΔI/I₀，电阻显示ΔR/R₀。阈值栏使用SI值；基准绝对值小于或等于阈值、阈值无效或基准未设置时不计算相对变化率。
- 暂停和继续等实验事件写入事件 CSV。
- “加载历史CSV”只读现存测量文件；若同一实验目录中有配套`events.csv`，会一并恢复落在当前时间范围内的事件标记，Web 端的事件文件也由后台 Worker 解析。超过100,000点时，图形显示均匀抽稀视图，原文件不变；拖选区间后，软件在后台扫描完整原始CSV计算统计，图表抽稀不影响统计。实时采集期间，统计完成后会把区间内新到的有效读数增量纳入，后台读取不阻塞采集线程。

## 6. 实验目录和CSV字段

每次实验在根目录下新建唯一时间戳目录，包含：

- `measurements.csv`：每次测量/失败尝试一行。`raw_reading`保留仪器响应文本，`value_si`保存未经滤波/舍入的SI值；`voltage_v`、`current_a`、`resistance_ohm`三列供常见工具直接筛选。无效值为空字符串，状态列写`timeout`、`overflow`、`invalid`、`instrument_local`或其他通信错误。
- `config.json`：原始仪器身份、IDN修订字段、*IDN?修订字段中的未验证数字候选、按序列号手动核对的 OutG/InG SW（若填写）、400 mA 判断采用的版本来源、串口、请求的模式设置、仪器回读、数据是真机还是模拟、采样时间语义及最终状态。手动版本会与SCPI读回分开保存。
- `events.csv`：人工事件、暂停/继续和相对基准变更。
- `run.log`：实验启动/停止、SCPI配置命令和异常。逐点原始响应在CSV；为避免长期高速采集产生巨型日志，测量响应不重复写入run.log。实时SCPI发送/接收在GUI日志面板可见；显示队列有界，界面跟不上时会提示丢弃的旧日志行。
- `CSV_打开说明.txt`：正常情况下会自动生成，说明事件文本以及测量 CSV 中原始读数/错误文本的公式风险，并介绍事件和测量安全副本；不会改写测量或事件记录。若文件系统阻止创建，桌面实验目录摘要会明确提示并给出安全副本文件名。

采集运行期间，每条测量CSV会立即刷新；CSV到期同步由采集工作线程或后台线程执行，`run.log`则每秒由后台线程同步。事件写入会强制同步`events.csv`；正常停止时会强制同步全部记录文件。任一同步失败都会被保留、停止采集并阻止写入“干净完成”标记。突然断电时，最多约一秒的未强制同步内容仍可能丢失，存储设备也可能有自身写入缓存。

CSV以UTF-8 BOM编码，含样本序号、主机本地和UTC ISO时间、单调经过时间、读数通信耗时、实际间隔、模式、SI单位、原始响应、仪器量程、NPLC、交流带宽、滤波/零点状态和异常信息。Python `repr(float)`不做小数位截断；`raw_reading`保留SCPI返回的十进制文本。

## 7. 自动化测试

自动化测试使用内部模拟仪器，不在软件界面提供模拟连接。

## 8. 依据、限制和状态

此前记录的冻结程序构建流程曾逐模式执行六种测量的 Mock GUI 采集和文件保存检查；这些记录不是当前源码快照的硬件验收结果。

可追溯 SCPI 与规格请看 [SCPI 参考](scpi-reference.md)。硬件检查范围与当前版本的验证限制见[硬件验证说明](hardware-validation.md)。


本源码发布未附带预编译 Windows 二进制。测试与硬件验证的当前范围见[硬件验证说明](hardware-validation.md)。

## 9. localhost 浏览器界面

双击桌面的“Fluke 8845A Web”快捷方式或运行 `python -m fluke8845a.webapp`，打开本机网页界面。主界面提供六种测量模式、仪器连接、测量参数、量程、采集控制和曲线。普通滚轮缩放两轴，Shift + 滚轮缩放时间轴，Ctrl + 滚轮缩放纵轴。可选完整显示、滚动显示和纵轴自动；滑动平均点数可在采集中修改。点击“保存曲线”下载 ZIP，内含简洁 CSV 和详细 CSV。

启动服务不会自动打开串口。连接前先断开桌面版占用的 COM 端口，因为同一端口不能同时由两个程序打开。电流和电阻测量仍须由操作者确认接线、输入端子、保险丝和被测电路状态。页面的“退出服务”会停止采集、完成待写数据并安全关闭串口。

## English

Version 2.1.3 updates the desktop and Web mode-switch animations, adds Windows desktop and Web shortcuts with the app icon, and replaces console launchers. The source package prepares its Python environment on first start and reuses it afterward. Both interfaces keep the same six measurement modes and live chart layout. Plot zoom changes only the view and does not modify raw readings. See [hardware validation](hardware-validation.md) for the verification scope.

### 1. Connection and modes

For a real meter, set the rear interface to RS-232 and select the COMPUTER protocol with the 8845A/8846A command set. The application connects using COMPUTER/REMOTE control by default; its instrument settings panel can switch between this state and LOCAL front-panel control. On connection, it sends `SYST:REM` before querying `*IDN?`, accepts only a Fluke model identified as `8845A`, and does not reset the instrument. If identification fails, it makes a best-effort `SYST:LOC` before closing the serial port. Sending REMOTE before RS-232 traffic follows the *Programmers Manual*, PDF p. 76 (printed p. 66).

The application scans serial ports at startup and also accepts a manually entered COM port. Defaults are 9600 baud, 8 data bits, no parity, one stop bit, no flow control, CRLF termination, and a two-second response timeout. Confirm the instrument's settings. The instrument menu's RS-232 COMPUTER protocol and the application's COMPUTER/LOCAL control state are separate settings. COMPUTER control disables front-panel keys except LOCAL. The application can send `SYST:LOC` to hand control to the panel; measurement and settings commands are paused while LOCAL. Switching back to COMPUTER sends `SYST:REM` and refreshes the active function and settings. A normal disconnect attempts `SYST:LOC`.

On a real instrument, any `READ?` timeout (including a plain serial `TimeoutError`) or communication failure during a reading immediately ends the current experiment, saves the failed attempt with an empty value, and closes the serial connection. The application does not retry or resume the old CSV. Check the instrument and cable, reconnect manually, and start a new experiment directory for the next segment so a delayed response cannot be mistaken for a later sample.

### 2. Measurement modes and safe wiring

Supported modes are DCV, ACV, DCI, ACI, 2-wire resistance, and 4-wire resistance. AC readings are RMS values, not peak values or waveform samples. DCV also offers automatic input impedance, off by default (10 MΩ); when enabled it selects >10 GΩ only for 100 mV, 1 V, and 10 V ranges. Other DCV ranges remain 10 MΩ. The setting is queried back from the instrument and saved with experiment data. The application cannot inspect the circuit, test leads, terminals, or fuses. Connect voltage inputs in parallel. De-energize the circuit before resistance measurements. For current, de-energize first and connect the meter in series. Select the appropriate mA/400 mA or 10 A input, never connect both red current inputs simultaneously, and never place a current input in parallel with a voltage source. Check wiring, range, front-panel input selection, and fuses before each measurement. Software cannot identify which physical red jack contains a lead. The `*IDN?` revision field may be user-defined, so even a parseable number is not accepted as OutG SW evidence. The operator may record OutG SW and InG SW from the front-panel versions page, saved by instrument serial number; only the OutG SW record is used for the DCI 400 mA gate, while InG SW and any numeric IDN candidate are metadata only. DCI's fixed 400 mA range and autorange are blocked only when manually recorded OutG SW is parseable and explicitly below 2.0. The ACI 400 mA specification requires software version 1.0.700.18 or later, but does not identify OutG versus InG; the application prompts the operator to verify the panel version and does not automatically reject ACI 400 mA fixed range or autorange based on either field. Rear terminals still block autorange that may enter front-only current ranges.

The mA input has a 440 mA fuse and the 10 A input an 11 A fuse. The documented limit is 400 mA continuous; 550 mA is limited to two minutes followed by one minute off. Fuse ratings are not continuous operating limits. The fixed 1/3/10 A ranges require front terminals: the DCI table marks 1 A front-only, 3 A is part of the 10 A range, and the user manual says rear terminals cannot measure 10 A; the ACI table marks 1/3/10 A front-only. Autorange is blocked on rear terminals because it may enter those ranges. DCI 400 mA requires OutG SW 2.0 or later; ACI 400 mA requires software version 1.0.700.18 or later, but the official specification does not identify OutG versus InG. The application prompts the operator to verify the panel version and does not automatically reject the ACI 400 mA fixed range or autorange based on either field. Only DCI 400 mA fixed range and DCI autorange are blocked when manually recorded OutG SW is parseable and explicitly below 2.0. The `*IDN?` revision field may be user-defined and is not used as firmware evidence, even when it contains a parseable number. InG SW and numeric IDN candidates are metadata only. Unknown or unparseable OutG SW does not block DCI. ACI's 100 µA and 1 mA ranges are 8846A-only. The supplied Programmer's Manual documents `SYST:VERS?` as returning the SCPI command version and does not document a query for OutG/InG SW; the Users Manual directs users to the front-panel `SYSTEM > VERSIONS + SN` page. Manual panel entries are labelled separately from SCPI data in experiment metadata.

### 3. Measurement settings and speed

DCV ranges are 100 mV–1000 V; ACV ranges are 100 mV–750 V. DCI ranges are 100 µA–10 A and ACI ranges 10 mA–10 A, subject to firmware and terminal conditions. Resistance ranges are 100 Ω–100 MΩ. DC voltage/current and resistance provide NPLC values 0.02, 0.2, 1, 10, and 100. AC voltage/current provide 3, 20, and 200 Hz bandwidth settings; these are filter bandwidths, not sample rates. The high input impedance setting is DCV-only and effective on 100 mV, 1 V, and 10 V ranges.

Digital filtering is supported for DCV, DCI, 2-wire resistance, and 4-wire resistance; the application permits enabling it only at NPLC 10 or 100 (rates slower than 1 PLC). `CONF` presets the filter ON, so configuration sends `SENS:<function>:FILT:DIG OFF`, checks the error queue, and verifies `...:FILT:DIG?` is OFF before writing range/NPLC. When ON is requested, it writes NPLC first, then sends `SENS:<function>:FILT:DIG ON`, checks the error queue, and verifies the ON readback. A panel-verified OutG SW below 2.0 rejects an ON request; an unknown version is probed and must pass the error-queue and readback checks. If a verified older version is configured with filtering OFF, the unsupported filter command is skipped and NPLC remains configurable. The manuals say the number of averaged readings varies by DC function and range and do not specify a maximum `READ?` response time. The software has only a finite host-side timeout; it does not guarantee a meter response deadline. On timeout it saves a blank failed attempt, stops acquisition, closes the serial connection, and does not fabricate a reading. See the *Programmers Manual*, Rev. 3, Table 13, PDF pp. 42–43 (printed pp. 32–33), filter commands pp. 58–60 (printed pp. 48–50), `CONF` preset Table 18 p. 51 (printed p. 41), `READ?` p. 52 (printed p. 42), and the *Users Manual*, Rev. 3, PDF pp. 47 and 75 (printed pp. 3-3 and 4-5).

Instrument conversion speed and the application's target interval are independent. Set both before starting; they remain locked during startup, acquisition, pause, and final save. Actual rate depends on range, auto-zero, filtering, serial settings, instrument conversion, and communication. A target interval of zero means no additional host delay, not zero instrument or serial latency.

**Restore default settings** restores the software defaults for all six measurement modes, clears their saved baselines, and sets the target interval to one second. It preserves serial communication settings and the current output directory. The button changes software preferences only; it does not reset or configure the instrument. Stop acquisition before initializing. To apply the selected mode to a connected meter, click **Apply instrument parameters** separately.

### 4. Acquisition and data files

Connect the instrument, select one of the six modes, then configure the instrument and target interval in the measurement settings dialog. Start a single reading or continuous acquisition; continuous acquisition can be paused, resumed, or stopped. Each run receives an automatic time-and-mode name and a separate timestamped directory. Wait for saving to finish before changing mode or settings.

The real backend configures the measurement and performs one `READ?` per point, followed by a range query. It does not use `INIT/FETCh` batches. Existing `timestamp_local`, `timestamp_utc`, and `elapsed_s` retain their host `READ?` request-start meaning. New `host_read_request_start_*` and `host_read_response_end_*` columns record the host wall-clock and monotonic request/response boundaries; they are not instrument sampling timestamps. `read_duration_s` covers only this `READ?` request/response window and excludes the later range query. `actual_interval_s`, desktop telemetry, and Web telemetry use the difference between consecutive valid response-end monotonic timestamps; failed attempts have no interval value. The instrument provides no per-reading hardware timestamp, so the exact sampling instant is unknown. Failed attempts retain a sequence and status with an empty value, never a fabricated zero.

Each experiment folder contains `measurements.csv`, `config.json`, `events.csv`, and `run.log`; the app also attempts to create `CSV_打开说明.txt`. The notice warns against opening raw event text directly in a spreadsheet and points to the safe-copy export; it does not modify either raw CSV. If the filesystem prevents creating the note, the desktop experiment summary explicitly warns the user and names the safe-copy file. `config.json` preserves the raw instrument identity and IDN revision field, any parseable number from the `*IDN?` revision field, labelled as an unverified candidate, separately recorded front-panel OutG/InG software versions when entered, and the source selected for the 400 mA check. Measurement rows preserve raw instrument text separately from the SI value and include host local/UTC timestamps, elapsed time, communication duration, mode, range, NPLC or AC bandwidth, filter/zero state, the DCV input impedance setting where applicable, and error status. CSV is UTF-8 with BOM. Acquisition and file writing run in a worker thread; rows are flushed as they arrive. A separate background sync thread checks due CSV and `run.log` synchronization at one-second intervals while acquiring, paused, or waiting for a long target interval, including while the acquisition thread waits for instrument communication; filesystem load can still delay synchronization. Event writes and normal stop/close force synchronization immediately. Any CSV or `run.log` sync failure stays latched; a successful final retry does not write a clean completion marker. Stop and exit wait for an in-progress filesystem sync before closing the data files. A synchronization call cannot guarantee that a storage controller has persisted its cache through power loss, and its duration contributes to the actual acquisition interval. The graph retains at most 100,000 display points. If the GUI thread stalls, sample notifications are coalesced and at most the latest 10,000 pending display points are kept; the SCPI display-log mailbox also stays bounded at 5,000 lines and reports when earlier display entries are dropped. The curve may skip intermediate display points while the CSV and full-run statistics remain complete.

### 5. Analysis, events, and history

The desktop window shows connection status, six mode buttons, acquisition controls, and one plot. Use the connection dialog and the separate measurement settings dialog before starting a run. Settings and target interval remain locked during startup, acquisition, pause, and final save. Desktop plots support wheel zoom of both axes, Shift+wheel for X only, and Ctrl+wheel for Y only. Alt has no separate gesture. Drag to pan; manual navigation holds the view until **Auto scale** or a double-click restores automatic scaling.

Historical CSV loading is read-only. Matching event markers are restored where available. The bounded overview preserves endpoints, local peaks and invalid-reading gaps; full-run statistics still scan the complete source. Zooming into an earlier interval loads that visible detail from CSV in a background task, and Auto scale returns to the full overview. The original CSV is unchanged. The browser worker reads history in 256 KiB chunks, supports files up to 2 GiB, and reports progress with cancellation.

### 6. Mock and verification limits

Automated tests use an internal simulator; the application interfaces connect to real instruments. See the [SCPI reference](scpi-reference.md), [hardware verification notes](hardware-validation.md), and [known limitations](known-limitations.md).

### 7. Local browser interface

Start the browser interface with the desktop shortcut “Fluke 8845A Web” or `python -m fluke8845a.webapp`. The local interface provides six measurement modes, connection and measurement settings, range controls, acquisition, and a live chart. The wheel zooms both axes; Shift and Ctrl restrict zoom to the time or vertical axis. Curve export downloads a ZIP containing the simple and detailed CSV files.

Starting the browser server does not open a serial port. Disconnect the desktop app from the meter before connecting the browser interface because Windows cannot open the same COM port from two applications simultaneously. Operators must still verify wiring, terminals, fuses, and circuit state for current and resistance measurements. **Exit service** stops acquisition, finishes pending writes, and closes the serial connection.
