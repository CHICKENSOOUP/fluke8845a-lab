# 短时真机 GUI 探测

`--probe-real-gui*` 是开发者使用真实 Fluke 8845A 核对 GUI 采集、实时曲线、速度/间隔调整和 CSV 保存的诊断入口，支持 DCV、ACV、DCI、ACI、RES2 和 RES4。调用这些命令会打开串口并向仪表发送 SCPI；本页命令不是 Mock 示例。

探测只在仪表已经处于目标模式且 `ROUT:TERM?` 回报前面板时继续，不会自动切换测量功能。它会在 8–50 秒采集窗口内切换两次有效速度参数和两次采样间隔；程序总运行墙钟上限为 55 秒，并为停止、恢复设置和收尾预留时间。探测不代替仪表准确度校准，也不能证明长时间稳定性。当前源码含 ACV/RES4 探测路径，但尚无这两种模式的真机运行证据；软件测试不等同于硬件验收。

## 使用前检查

- 将仪表设为 RS-232 / COMPUTER，确认波特率和 8N1、无流控、CRLF 等串口参数与命令匹配。
- 目标测量模式必须先由操作者在仪表上选好；命令只读核对模式和 `FRON` 端子，不会替用户确认物理接线。
- 运行前检查被测回路、输入端子、保险丝和安全限制。探测需要已确认的适配源/负载；不得让软件示例中的声明值替代实际测量。
- 每份报告路径必须是新的文件。报告及实验子目录含原始读数、仪器身份和通信信息，生成可分享的摘要时使用 `scripts/create_hardware_probe_manifest.py`；摘要会排除设备序列号和完整身份字符串。

## DCV 与 ACV

DCV 确认可调直流源接在前面板 HI/LO：

```powershell
python -m fluke8845a.app --probe-real-gui-dcv --port COM3 --baudrate 115200 --duration 8 --report build/dcv-probe.json --confirm-front-voltage-source
```

ACV 确认交流源接在前面板 HI/LO，并明确提供 RMS、峰值、频率、固定量程和带宽：

```powershell
python -m fluke8845a.app --probe-real-gui-acv --port COM3 --baudrate 115200 --duration 8 --report build/acv-probe.json --confirm-front-acv-voltage-source --acv-source-vrms 1 --acv-source-vpeak 1.41421356 --acv-frequency-hz 50 --acv-range-v 10 --ac-bandwidth-hz 20
```

ACV 命令会拒绝超过固定量程、750 V RMS、1000 V peak 或 `8×10^7 V·Hz` 限制的声明条件；频率还必须符合选择及探测切换的带宽档。电压峰值是操作者声明值，软件无法验证波形。ACV 档位、接线和限制依据《Fluke 8845A/8846A Users Manual (Chinese)》PDF pp.69–70（印刷 pp.4-5–4-6）、《Programmers Manual Rev. 3》Table 13 / PDF pp.42–43，以及《Extended Specifications》PDF p.4（印刷 p.3）。以上限制使用 8845A 数据，不采用 8846A 的更高 AC 输入指标。

## DCI 与 ACI

DCI 确认 5 mA 直流源串联，红线接前面板 mA/400 mA、黑线接 LO：

```powershell
python -m fluke8845a.app --probe-real-gui-dci --port COM3 --baudrate 115200 --duration 8 --report build/dci-probe.json --confirm-front-dci-5ma-series
```

ACI 确认交流电流源串联，并声明 RMS、频率、固定量程和带宽：

```powershell
python -m fluke8845a.app --probe-real-gui-aci --port COM3 --baudrate 115200 --duration 8 --report build/aci-probe.json --confirm-front-aci-current-series --aci-current-arms 0.005 --aci-frequency-hz 60 --aci-range-a 0.01 --ac-bandwidth-hz 20
```

两种电流探测都要求电流输入串联并确认保险丝和端子；软件无法识别红表笔实际插孔。AC 带宽选择必须覆盖所声明的源频率，ACI 最高不超过 10 kHz。400 mA 档位仍按主界面的官方版本提示处理，本探测示例使用 10 mA 固定档。

## RES2 与 RES4

RES2 可测前面板 HI/LO 短接或标称 30 MΩ 电阻；两种接线确认只能选择一个：

```powershell
python -m fluke8845a.app --probe-real-gui --port COM3 --baudrate 115200 --duration 8 --report build/res2-probe.json --confirm-front-2w-30-megaohm
```

RES4 必须确认前面板 INPUT HI/LO FORCE 与 SENSE HI/LO 四线连接、被测电阻已断电、隔离、放电且没有外加电压；明确选择 8845A 支持的固定量程：

```powershell
python -m fluke8845a.app --probe-real-gui-res4 --port COM3 --baudrate 115200 --duration 8 --report build/res4-probe.json --confirm-front-res4-four-wire --res4-range-ohm 100000000
```

`ROUT:TERM?` 只能报告前/后端子，不能检测四根线的实际拓扑；此确认由操作者承担。8845A 四线电阻量程为 100 Ω–100 MΩ，不包含 8846A 专有的 10 Ω 或 1 GΩ 档。接线依据《Users Manual (Chinese)》PDF pp.71–73（印刷 pp.4-7–4-9），量程依据《Extended Specifications》PDF p.6（印刷 p.5），FRES/NPLC 依据编程手册 Table 13 PDF p.43（印刷 p.33）。

## 产物与限制

探测会保存 JSON 报告、独立实验目录、原始 CSV、配置、事件、运行日志；可选 `--probe-real-gui-visible` 截取 RES2 可见窗口。结束时程序尝试停止采集、恢复探测前参数并发送 `SYST:LOC`。若连接中断或收尾失败，报告会标记失败，不应把缺失/异常读数当成有效数据。所有源条件均为操作员声明；这些探测不提供可追溯精度验证。任何真实电压/电流记录不得超过 60 秒。

报告在探测开始时记录运行来源，并在结束时确认身份未变：源码运行记录整个 `fluke8845a` Python 包的 SHA-256；冻结版读取相邻的 `BUILD-PROVENANCE.json`（便携包）或 `BUILD-IDENTITY.json`（Nuitka `.dist` 目录），并记录构建来源和运行 EXE 的 SHA-256。生成 manifest 时只引用报告里的运行时身份，不再用之后的当前源码代替。若要把冻结版 EXE 纳入证据摘要，`--exe` 必须指向与报告记录哈希一致的文件。manifest 对采集报告本身做哈希，但不提供数字签名；其范围说明也不会把应用报告中的探测检查描述为独立硬件或精度验证。
