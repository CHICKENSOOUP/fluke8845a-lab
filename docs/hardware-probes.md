# 短时真机 GUI 探测

`--probe-real-gui*` 是给开发者做短时通信、实时曲线、采样速度调整和 CSV 保存核验的诊断入口，覆盖 RES2、DCV、DCI 和 ACI。ACV 没有独立的真机短测命令。探测不会代替仪表准确度校准，也不会证明长时间稳定性。它仅在仪表已经处于指定测量模式且 `ROUT:TERM?` 回报前面板时继续；模式不符会拒绝采集，不会自动切换测量功能。成功或失败后，程序尝试恢复探测前的模式参数并以 `SYST:LOC` 释放仪表。

采集时间参数可设 8–50 秒，程序总运行墙钟上限为 55 秒，并预留最多 15 秒停止、恢复设置和收尾；启动较慢时实际采集时间会相应缩短。确认对应真实接线后再执行；不要把真机诊断选项用于 Mock 或不匹配的端子。

## 命令

在已安装工程依赖的 PowerShell 中，从项目目录运行：

```powershell
python -m fluke8845a.app --probe-real-gui-dcv --port COM3 --baudrate 115200 --duration 8 --report build/dcv-probe.json --confirm-front-voltage-source
```

DCV 要求仪表已在 DCV，确认可调直流源接在前面板 HI/LO。探测使用 10 V 固定量程、1 NPLC。

```powershell
python -m fluke8845a.app --probe-real-gui-dci --port COM3 --baudrate 115200 --duration 8 --report build/dci-probe.json --confirm-front-dci-5ma-series
```

DCI 要求仪表已在 DCI，确认 5 mA 直流源串联，红线接 mA/400 mA、黑线接 LO。探测使用 10 mA 固定量程、1 NPLC。电流测量前仍须按实际电路断电、检查保险丝和接线；软件无法识别表笔实际插孔。

```powershell
python -m fluke8845a.app --probe-real-gui-aci --port COM3 --baudrate 115200 --duration 8 --report build/aci-probe.json --confirm-front-aci-current-series --aci-current-arms 0.005 --aci-frequency-hz 60 --aci-range-a 0.01 --ac-bandwidth-hz 20
```

ACI 要求仪表已在 ACI，确认交流电流源串联并提供标称 RMS 电流、频率、固定量程和 3/20/200 Hz 带宽。RMS 电流必须为正、小于所选量程且不超过 400 mA。软件只记录操作者声明的信号源条件，不会校准信号源。

RES2 可用 `--probe-real-gui`，或者 `--probe-real-gui-visible` 显示窗口并保存截图。二者都要求 `--confirm-front-2w-30mohm` 或 `--confirm-front-2w-short`。电阻和短路线分别用 100 MΩ、100 Ω固定量程。

探测会创建 JSON 报告及独立实验目录。报告含仪器身份字段、通信日志和运行路径，应作为本机诊断记录保管；对外分享前请移除设备序列号、个人路径和原始读数。`scripts/create_hardware_probe_manifest.py` 生成的摘要清单会排除仪器序列号和完整身份字符串。
