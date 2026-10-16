"use strict";

const $ = (selector) => document.querySelector(selector);
const el = (id) => document.getElementById(id);
const ui = {
  mode: el("mode"), options: el("modeOptions"), safety: el("safetyText"), confirm: el("safetyConfirm"),
  canvas: el("plot"), overlay: el("plotOverlay"), empty: el("plotEmpty"), toast: el("toast"), logs: el("logList"), events: el("eventList"),
};
const app = {
  meta: null, state: null, points: [], latestInvalidPoint: null, imported: null, generation: null, lastSample: 0, lastLog: 0,
  modes: new Map(), selectedMode: "DCV", baseline: null, baselineByMode: new Map(), dcvInputImpedanceAuto: false, toastTimer: 0, pollTimer: 0, pollInFlight: false,
  portDescriptions: new Map(),
  importedEvents: null, importedExperimentId: null, importedFileName: "", chartEventHits: [], hoveredEventIndex: null, selectedEventIndex: null,
  chartFramePending: false, pointerFramePending: false, pointIndexCache: null,
  logRows: [], logFilter: "all",
  activePointers: new Map(), pinchState: null,
  activeDeviceSerial: "",
  safetyReadback: null,
  xView: null, yView: null, dragging: null, cursor: null, selectedPointIndex: null, bufferTruncated: false,
  awaitInitialSettings: false, awaitSettingsReadback: false, awaitSpeedReadback: false,
  mockConnectPending: false, startPending: false, pendingCommands: new Set(), commandOriginalDisabled: new Map(),
  historyEventsWorker: null, cancelEventImport: null,
};
const COLORS = { ink: "#526064", muted: "#87918b", grid: "#e5e4da", axis: "#aeb6ad", line: "#138b92", lineFill: "#138b9218", event: "#d39130" };

function htmlText(value) {
  return String(value ?? "").replace(/[&<>"']/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[char]);
}

function numberText(value, digits = 6) {
  const number = Number(value);
  if (!Number.isFinite(number)) return "—";
  if (number === 0) return "0";
  const abs = Math.abs(number);
  if (abs >= 1e6 || abs < 1e-4) return number.toExponential(Math.max(0, digits - 1)).replace(/\.0+(?=e)|(?<=\d)0+(?=e)/, "").replace("e+", "e");
  return Number(number.toPrecision(digits)).toLocaleString("en-US", { maximumSignificantDigits: digits, useGrouping: false });
}

function samplingIntervalText(rateHz) {
  const rate = Number(rateHz);
  if (!Number.isFinite(rate) || rate <= 0) return { value: "—", unit: "" };

  const intervalS = 1 / rate;
  if (!Number.isFinite(intervalS)) return { value: "—", unit: "" };
  if (intervalS >= 3600) return { value: numberText(intervalS / 3600, 4), unit: "h" };
  if (intervalS >= 60) return { value: numberText(intervalS / 60, 4), unit: "min" };
  if (intervalS >= 1) return { value: numberText(intervalS, 4), unit: "s" };
  if (intervalS >= 1e-3) return { value: numberText(intervalS * 1e3, 4), unit: "ms" };
  return { value: numberText(intervalS * 1e6, 4), unit: "µs" };
}

function scaledValue(value, unit) {
  const number = Number(value);
  if (!Number.isFinite(number)) return { value: "—", unit: unit || "" };
  const absolute = Math.abs(number);
  let factor = 1;
  let prefix = "";
  if (unit === "V") {
    if (absolute && absolute < 1) { factor = 1e-3; prefix = "m"; }
  } else if (unit === "A") {
    if (absolute && absolute < 1e-3) { factor = 1e-6; prefix = "µ"; }
    else if (absolute && absolute < 1) { factor = 1e-3; prefix = "m"; }
  } else if (unit === "Ohm") {
    if (absolute >= 1e6) { factor = 1e6; prefix = "M"; }
    else if (absolute >= 1e3) { factor = 1e3; prefix = "k"; }
  }
  return { value: numberText(number / factor), unit: prefix + (unit === "Ohm" ? "Ω" : unit) };
}

function baselineUnits(unit) {
  if (unit === "V") return ["V", "mV", "µV"];
  if (unit === "A") return ["A", "mA", "µA"];
  if (unit === "Ohm") return ["Ω", "kΩ", "MΩ", "GΩ"];
  return [unit || "SI"];
}

function configureBaselineUnits(unit) {
  const select = el("baselineScale");
  if (!select) return;
  const choices = baselineUnits(unit);
  const previous = select.value;
  select.innerHTML = choices.map((choice) => `<option value="${htmlText(choice)}">${htmlText(choice)}</option>`).join("");
  select.value = choices.includes(previous) ? previous : choices[0];
  if (!choices.includes(select.value)) select.value = choices[0];
  const input = el("baselineValue");
  if (input?.setAttribute) input.setAttribute("aria-label", `基准值，单位 ${select.value}；内部按 SI 存储`);
}

function baselineScaleFactor(scale) {
  return ({ V: 1, mV: 1e-3, "µV": 1e-6, A: 1, mA: 1e-3, "µA": 1e-6,
    "Ω": 1, "kΩ": 1e3, "MΩ": 1e6, "GΩ": 1e9 })[scale] || 1;
}

function baselineInputToSI(value) {
  const number = Number(value);
  return Number.isFinite(number) ? number * baselineScaleFactor(el("baselineScale")?.value) : null;
}

function setBaselineInputFromSI(value) {
  if (value !== null && value !== undefined && value !== "" && Number.isFinite(Number(value))) {
    el("baselineValue").value = String(Number(value) / baselineScaleFactor(el("baselineScale")?.value));
  } else {
    el("baselineValue").value = "";
  }
}

function setActiveBaseline(value) {
  app.baseline = value === null || value === undefined || !Number.isFinite(Number(value)) ? null : Number(value);
  app.baselineByMode.set(app.selectedMode, app.baseline);
}

function activeMode() { return app.modes.get(app.selectedMode) || null; }
function settingsPayload() {
  const mode = app.selectedMode;
  const settings = { mode, autorange: el("autorange").checked, autozero: el("autozero").checked };
  if (!settings.autorange) settings.range_si = Number(el("range").value);
  const spec = app.modes.get(mode);
  if (spec.nplc.length) settings.nplc = Number(el("nplc").value);
  if (spec.bandwidths.length) settings.ac_bandwidth_hz = Number(el("bandwidth").value);
  if (spec.analog_filter) settings.analog_filter = el("analogFilter").checked;
  if (spec.digital_filter) settings.digital_filter = false;
  if (spec.input_impedance_auto) settings.input_impedance_auto = el("inputImpedanceAuto").checked;
  return settings;
}

function serialPayload() {
  return {
    baudrate: Number(el("baudrate").value), bytesize: Number(el("bytesize").value),
    parity: el("parity").value, stopbits: Number(el("stopbits").value),
    eol: el("eol").value, flow_control: el("flow").value, timeout_s: Number(el("serialTimeout").value),
  };
}

function populateModeOptions(spec) {
  if (app.selectedMode === "DCV" && el("inputImpedanceAuto")) {
    app.dcvInputImpedanceAuto = el("inputImpedanceAuto").checked;
  }
  clearPreflightSummary();
  app.selectedMode = spec.value;
  const rangeItems = spec.ranges.map((range) => `<option value="${range}">${htmlText(rangeLabel(range, spec.unit))}</option>`).join("");
  let html = `<div class="option-row"><label class="option-check"><input id="autorange" type="checkbox" checked> 自动量程</label><label>固定量程<select id="range" disabled>${rangeItems}</select></label></div>`;
  const supportedTiming = [];
  if (spec.nplc.length) {
    html += `<label>积分时间 / NPLC<select id="nplc">${spec.nplc.map((value) => `<option value="${value}" ${value === 1 ? "selected" : ""}>${value} NPLC</option>`).join("")}</select></label>`;
    supportedTiming.push("积分时间");
  }
  if (spec.bandwidths.length) {
    html += `<label>交流响应带宽<select id="bandwidth">${spec.bandwidths.map((value) => `<option value="${value}" ${value === 20 ? "selected" : ""}>${value} Hz</option>`).join("")}</select></label>`;
    supportedTiming.push("交流响应带宽");
  }
  const timingEffects = [];
  if (spec.nplc.length) timingEffects.push("NPLC 越高，积分越久，实测采样间隔通常越长");
  if (spec.bandwidths.length) timingEffects.push("交流响应带宽会影响稳定时间和噪声，较低带宽可能拉长实测采样间隔");
  if (spec.analog_filter) timingEffects.push("启用模拟滤波可能增加稳定等待并拉长实测采样间隔");
  timingEffects.push("若启用自动零点，额外内部测量可能拉长实测采样间隔");
  timingEffects.push("软件目标间隔仅是仪器读数完成后的额外等待；实测采样间隔还包括仪器转换和串口通信耗时");
  const timingHint = `${supportedTiming.length ? `当前模式可调${supportedTiming.join("与")}。采集前用“应用仪器参数”；采集中调整后用“应用速度”。` : "当前模式没有可调的积分时间或交流响应带宽。"} ${timingEffects.join("；")}。`;
  html += `<p id="modeParameterHint" class="mode-parameter-hint" aria-live="polite">${timingHint}</p>`;
  if (spec.analog_filter) html += `<label class="option-check"><input id="analogFilter" type="checkbox"> 启用直流模拟滤波</label>`;
  if (spec.input_impedance_auto) html += `<label class="option-check" title="默认关闭为10 MΩ。启用后仅在100 mV、1 V、10 V DCV量程使用大于10 GΩ的输入阻抗，其他量程仍为10 MΩ。"><input id="inputImpedanceAuto" type="checkbox" ${app.dcvInputImpedanceAuto ? "checked" : ""}> 自动高输入阻抗（仅DCV）</label><p class="field-hint">仅100 mV、1 V、10 V量程可用，启用时输入阻抗大于10 GΩ。</p>`;
  if (spec.digital_filter) html += `<label class="option-check" title="${htmlText(app.meta?.digital_filter_disabled_reason || "数字滤波当前保持关闭")}"><input id="digitalFilter" type="checkbox" disabled> 直流数字滤波（暂时关闭）</label>`;
  html += `<label class="option-check" title="仪表自动测量内部零点并补偿偏移，可提升稳定性但会增加测量时间；不会把被测读数强制改成0。"><input id="autozero" type="checkbox" checked> 自动零点校正</label>`;
  html += `<p id="autozeroSpeedHint" class="mode-parameter-hint">启用自动零点会增加测量时间；不会把被测读数强制改成 0。</p>`;
  ui.options.innerHTML = html;
  applyDigitalFilterNplcGuard(app.state);
  el("autorange").addEventListener("change", () => {
    el("range").disabled = el("autorange").checked;
    clearSafetyConfirmation();
    clearPreflightSummary();
    renderSettingsComparison(app.state);
    updateCurrentRangeGuidance(app.state);
  });
  el("range").addEventListener("change", () => { clearSafetyConfirmation(); clearPreflightSummary(); renderSettingsComparison(app.state); updateCurrentRangeGuidance(app.state); });
  for (const id of ["nplc", "bandwidth", "analogFilter", "inputImpedanceAuto", "autozero"]) {
    const control = el(id);
    if (control) control.addEventListener("change", () => { clearPreflightSummary(); renderSettingsComparison(app.state); });
  }
  ui.safety.textContent = spec.safety;
  const diagram = el("terminalDiagram");
  const current = spec.quantity === "current";
  const fourWire = spec.value === "RES4";
  const terminals = current
    ? ["HI", "LO", "mA / 400 mA", "10 A"]
    : fourWire ? ["HI", "LO", "SENSE HI", "SENSE LO"] : ["HI", "LO"];
  const terminalNames = terminals.map((name) => {
    const type = name.includes("HI") ? "terminal-hi" : name.includes("LO") ? "terminal-lo" : "terminal-sense";
    return `<span class="terminal-socket ${type}"><i aria-hidden="true"></i>${htmlText(name)}</span>`;
  }).join("");
  diagram.innerHTML = `<span class="terminal-diagram-title">${current ? "电流串联端子" : fourWire ? "四线电阻端子" : spec.quantity === "resistance" ? "两线电阻端子" : "电压并联端子"}</span><span class="terminal-bank-label">INPUT / SENSE</span><div class="terminal-bank ${current ? "terminal-bank-current" : ""}" aria-label="${htmlText(terminals.join("、"))}">${terminalNames}</div><span class="terminal-diagram-note">按前面板插孔名称接线；此图示不检测实际接线。</span>`;
  updateCurrentRangeGuidance(app.state, spec);
  clearSafetyConfirmation();
  el("readingMode").textContent = spec.label;
  el("chartTitle").textContent = `${spec.label} · 随时间变化`;
  el("legendName").textContent = `${spec.label} 原始读数`;
  drawChart();
}

function clearSafetyConfirmation() { ui.confirm.checked = false; }

function updateSafetyReadback(state) {
  const next = state?.connected && !state.simulation
    ? JSON.stringify({
      serial: String(state.device?.serial_number || "").trim(),
      controlMode: String(state.control_mode || "").trim().toLowerCase(),
      mode: String(state.active_settings?.mode || state.configuration?.function || "").trim().toUpperCase(),
      autorange: state.active_settings?.autorange === true,
      range: state.active_settings?.autorange === true ? "AUTO" : state.active_settings?.range_si ?? null,
      terminals: String(state.configuration?.input_terminals || "").trim().toUpperCase(),
    })
    : null;
  if (next !== app.safetyReadback) clearSafetyConfirmation();
  app.safetyReadback = next;
}

function setServiceStatus(online, error = "") {
  const status = el("serviceStatus");
  const dot = el("serviceDot");
  status.textContent = online ? "Web 服务在线" : `Web 服务中断${error ? ` · ${String(error).slice(0, 90)}` : ""}`;
  status.title = online ? "本机 Web 服务可正常响应" : "本机 Web 服务暂不可访问；仪器与采集显示保留最近一次状态";
  dot.classList.toggle("offline", !online);
}

function rangeLabel(value, unit) {
  const n = Number(value);
  if (unit === "V") return n < 1 ? `${n * 1000} mV` : `${n} V`;
  if (unit === "A") return n < 1 ? `${n * 1000} mA` : `${n} A`;
  if (unit === "Ohm") return n >= 1e6 ? `${n / 1e6} MΩ` : n >= 1e3 ? `${n / 1e3} kΩ` : `${n} Ω`;
  return String(value);
}

function compareRangeFirmware(firmware, minimum) {
  const match = String(firmware || "").match(/\d+(?:\.\d+){1,3}/);
  if (!match) return null;
  const parts = match[0].split(".").map(Number);
  if (![2, 4].includes(parts.length) || parts.some((part) => !Number.isInteger(part))) return null;
  const length = Math.max(parts.length, minimum.length);
  for (let index = 0; index < length; index += 1) {
    const actual = parts[index] || 0;
    const required = minimum[index] || 0;
    if (actual !== required) return actual > required;
  }
  return true;
}

function updateCurrentRangeGuidance(state = app.state, selectedSpec = null) {
  const hint = el("rangeLimitHint");
  const mode = el("mode")?.value || app.selectedMode;
  const spec = selectedSpec || app.modes.get(mode) || activeMode();
  if (!spec || spec.quantity !== "current") {
    hint.textContent = "";
    hint.classList.add("hidden");
    return;
  }

  const minimum = mode === "DCI" ? [2, 0] : [1, 0, 700, 18];
  const minimumText = mode === "DCI" ? "2.0" : "1.0.700.18";
  const isAuto = !!el("autorange")?.checked;
  const selectedRange = isAuto ? null : Number(el("range")?.value);
  const selectedText = isAuto
    ? "自动量程"
    : Number.isFinite(selectedRange) ? `固定 ${rangeLabel(selectedRange, spec.unit)}` : "固定量程未选择";
  const device = state?.device || {};
  const firmware = String(device.range_firmware || "").trim();
  const firmwareSource = String(device.range_firmware_source || "未知").trim() || "未知";
  const firmwareGate = firmware ? compareRangeFirmware(firmware, minimum) : null;
  const hasTerminalReadback = !!(state?.connected && !state.simulation && state.control_mode !== "local");
  const terminal = hasTerminalReadback ? String(state?.configuration?.input_terminals || "").trim() : "";
  const terminalKnown = !!terminal;
  const frontTerminal = terminalKnown && terminal.toUpperCase().startsWith("FRON");
  const pieces = [`量程限制：当前选择 ${mode} ${selectedText}`, `策略门槛：${mode} 400 mA 档要求 OutG SW ≥ ${minimumText}；1 A、3 A、10 A 档只允许前面板电流端子`];
  const active = state?.active_settings;
  if (state?.connected && !state.simulation && state.control_mode !== "local") {
    if (active?.mode && active.mode !== mode) pieces.push(`设备当前回读 ${active.mode}，与界面选择不同`);
    else if (active?.mode === mode && active.autorange) pieces.push("设备当前回读自动量程");
    else if (active?.mode === mode && active.range_si !== null && active.range_si !== undefined && active.range_si !== "" && Number.isFinite(Number(active.range_si))) pieces.push(`设备当前回读 ${rangeLabel(active.range_si, spec.unit)}`);
    else pieces.push("设备当前量程未回读");
  } else if (state?.control_mode === "local") {
    pieces.push("Local 模式无法回读仪器配置");
  } else {
    pieces.push("当前没有真实仪器配置回读");
  }

  if (firmware) pieces.push(`量程固件来源：${firmwareSource}，版本 ${firmware}`);
  else pieces.push(`版本未核实（来源：${firmwareSource}）`);
  pieces.push(terminalKnown ? `端子状态 ${terminal} 回读` : "端子未回读");

  const needs400mAGate = isAuto || selectedRange === 0.4;
  const needsFrontTerminal = isAuto || [1, 3, 10].includes(selectedRange);
  if (needs400mAGate) {
    if (firmwareGate === false) {
      pieces.push(`400 mA 档使用版本 ${firmware}（来源 ${firmwareSource}），低于门槛 ${minimumText}，该档按软件策略受限；下一步：人工核对仪表面板 VERSIONS + SN 中的 OutG SW，并确认缓存来源版本`);
    } else if (firmwareGate === true) {
      pieces.push(`400 mA 档版本门槛 ${minimumText} 已满足；这只说明固件门槛，不验证实际接线和保险丝`);
    } else {
      pieces.push(`400 mA 档版本门槛 ${minimumText} 未核实；未知版本不能证明设备可用；下一步：人工核对仪表面板 OutG SW，并确认缓存来源版本`);
    }
  }
  if (needsFrontTerminal) {
    if (terminalKnown && frontTerminal) {
      pieces.push(isAuto ? "自动量程所需的前面板端子条件已回读；实际接线仍需人工确认" : "该量程只允许前面板电流端子，当前端子回读符合此条件；仍需人工核对接线和保险丝");
    } else if (terminalKnown) {
      pieces.push(`${selectedText} 涉及的 1 A、3 A、10 A 档仅允许前面板电流端子；当前 ${terminal} 端子回读与要求不符，按软件策略受限；下一步：按面板端子标识人工核对接线与保险丝`);
    } else {
      pieces.push(`${isAuto ? "自动量程可能进入" : selectedText + "可能涉及"}仅允许前面板的 1 A、3 A、10 A 档；先在 Computer 模式读取端子状态并核对实际接线`);
    }
  } else if (selectedRange !== 0.4) {
    pieces.push("此固定档未触发已知的 400 mA 固件门槛或 1/3/10 A 前面板端子限制；仍需人工核对接线和保险丝");
  }
  hint.textContent = pieces.join("；");
  hint.classList.remove("hidden");
}

function renderIdnFirmwareInfo(state, device = state?.device || {}) {
  const info = el("idnFirmwareInfo");
  if (!info) return;
  if (!state?.connected) {
    info.textContent = "未连接，尚未读取 *IDN?。";
  } else if (state.simulation) {
    info.textContent = "MOCK 模拟器身份不是实测 *IDN? 固件版本证据；面板 OutG/InG 手工记录另行显示。";
  } else if (device.firmware) {
    const revision = String(device.idn_revision || "未返回");
    info.textContent = `来源：*IDN? 自动解析 · 版本 ${device.firmware}（已解析） · 原始修订字段：${revision}。`;
  } else {
    const revision = String(device.idn_revision || "未返回");
    info.textContent = `来源：*IDN? 自动读取 · 版本无法解析 · 原始修订字段：${revision}。`;
  }
}

function renderAcquisitionFeedback(state, telemetry, activeExperiment) {
  const output = el("acquisitionFeedback");
  const status = String(state.status || "");
  const invalidPoint = app.latestInvalidPoint;
  const invalidIndex = Number(invalidPoint?.id ?? invalidPoint?.sample_index);
  const latestIndex = Number(state.latest?.sample_index ?? state.latest?.id);
  const hasNewerValidSample = state.latest?.status === "ok"
    && Number.isFinite(invalidIndex) && Number.isFinite(latestIndex) && latestIndex > invalidIndex;
  const currentInvalidPoint = hasNewerValidSample ? null : invalidPoint;
  const invalidText = [currentInvalidPoint?.error, currentInvalidPoint?.raw].map((value) => String(value || "")).join(" ");
  const scpiTimeout = /SCPI\s*响应超时/i.test(status) || /SCPI\s*响应超时/i.test(invalidText);
  const readTimeout = currentInvalidPoint?.status === "timeout" || /READ\??[^\n]*(?:响应)?超时/i.test(invalidText);
  const attempts = telemetry.attempt_count ?? 0;
  const valid = telemetry.valid_count ?? 0;
  const abnormal = telemetry.abnormal_count ?? 0;
  const savedDirectory = state.last_experiment_dir || state.experiment_dir || "";
  const savedFile = savedDirectory ? ` · 已保存文件 ${savedDirectory}/measurements.csv` : "";
  const disconnectDetails = `${status} ${String(state.connection_text || "")}`;
  const explicitConnectionFailure = /连接中断|(?:串口|端口|COM\d*)[^。\n]{0,24}(?:故障|失败|断开|关闭|不存在)|仪器已切回LOCAL|RS-232[^。\n]*(?:终端模式|不是Computer模式)|控制方式切换失败/i.test(disconnectDetails);

  if (scpiTimeout) {
    output.textContent = `SCPI 响应超时，立即停止采集并关闭连接；不会自动重连。已保存尝试 ${attempts} · 有效 ${valid} · 异常 ${abnormal}${savedFile}。安全恢复：检查 COM 端口、波特率、结束符和仪表 Computer 模式；修正后手动重新连接并新建实验。`;
  } else if (!activeExperiment && state.connected === false && !state.simulation && state.last_experiment_dir && explicitConnectionFailure) {
    output.textContent = `仪器连接已中断，采集已停止；不会自动重连。已保存尝试 ${attempts} · 有效 ${valid} · 异常 ${abnormal}${savedFile}。安全恢复：检查 COM 端口、波特率、结束符和仪表 Computer 模式；手动重新连接后新建实验。`;
  } else if (/连续超时.*停止/.test(status)) {
    output.textContent = `普通 READ 超时连续 3 次后停止采集；已保存尝试 ${attempts} · 有效 ${valid} · 异常 ${abnormal}${savedFile}。检查连接和仪表 Computer 模式，恢复后重新连接并新建实验。`;
  } else if (activeExperiment && readTimeout) {
    output.textContent = `普通 READ 超时会在下一采样周期重试；连续 3 次才停止。当前统计：尝试 ${attempts} · 有效 ${valid} · 异常 ${abnormal} · 样本逐点写入 CSV。`;
  } else if (activeExperiment) {
    const paused = status.includes("暂停");
    const kind = state.single_shot ? "单次测量" : "连续采集";
    output.textContent = `${kind}${paused ? "已暂停" : "中"} · 当前统计：尝试 ${attempts} · 有效 ${valid} · 异常 ${abnormal}。普通 READ 超时在下一采样周期重试，连续 3 次才停止；SCPI 响应超时会立即停止并关闭连接。样本逐点写入 CSV。`;
  } else if (state.last_experiment_dir) {
    const kind = /单次/.test(status) ? "单次测量完成" : "实验已结束";
    output.textContent = `${kind} · 已保存尝试 ${attempts} · 有效 ${valid} · 异常 ${abnormal} · ${state.last_experiment_dir}/measurements.csv${state.last_error ? " · 异常详情见运行日志" : ""}`;
  } else {
    output.textContent = "尚未开始实验；开始后会逐点保存 CSV。普通 READ 超时按下一采样周期重试，连续 3 次才停止；SCPI 响应超时会立即停止并关闭连接。";
  }
}

function axisScaleForValues(values, unit) {
  let magnitude = 0;
  for (const value of values) {
    const candidate = Math.abs(Number(value));
    if (Number.isFinite(candidate) && candidate > magnitude) magnitude = candidate;
  }
  if (unit === "V" && magnitude > 0 && magnitude < 1) return { factor: 1e-3, unit: "mV" };
  if (unit === "A" && magnitude > 0 && magnitude < 1e-3) return { factor: 1e-6, unit: "µA" };
  if (unit === "A" && magnitude >= 1e-3 && magnitude < 1) return { factor: 1e-3, unit: "mA" };
  if (unit === "Ohm" && magnitude >= 1e6) return { factor: 1e6, unit: "MΩ" };
  if (unit === "Ohm" && magnitude >= 1e3) return { factor: 1e3, unit: "kΩ" };
  return { factor: 1, unit: unit === "Ohm" ? "Ω" : unit };
}

function toast(message, isError = false) {
  clearTimeout(app.toastTimer);
  ui.toast.textContent = message;
  ui.toast.classList.toggle("error", isError);
  ui.toast.classList.add("visible");
  app.toastTimer = setTimeout(() => ui.toast.classList.remove("visible"), 3300);
}

async function command(action, payload = {}) {
  if (app.pendingCommands.has(action)) {
    return { success: false, message: "此操作正在执行，请等待完成" };
  }
  app.pendingCommands.add(action);
  renderCommandProgress();
  try {
    const response = await fetch("/api/command", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action, payload }),
    });
    const result = await response.json();
    toast(result.message || result.error || (result.success ? "操作完成" : "操作失败"), !result.success);
    return result;
  } catch (error) {
    toast(`本地服务没有响应：${error.message}`, true);
    return { success: false, message: String(error) };
  } finally {
    app.pendingCommands.delete(action);
    renderCommandProgress();
    if (app.state) updateStateDisplay(app.state);
  }
}

const COMMAND_LABELS = {
  connect_real: "连接仪器", connect_mock: "连接模拟器", disconnect: "断开连接",
  diagnose: "诊断", zero: "ZERO 校正", start: "开始采集", single: "单次测量",
  pause: "暂停采集", resume: "继续采集", stop: "停止并保存",
  configure: "应用仪器参数", speed: "应用速度", firmware_versions: "保存面板版本", control_mode: "切换控制方式",
  event: "记录事件", open_experiment_directory: "打开实验目录", shutdown: "退出服务",
};
const COMMAND_PROGRESS_DETAILS = {
  zero: "正在发出 ZERO:AUTO ONCE；随后检查错误队列并回读仪器配置，不会把读数设为零。",
  configure: "正在写入所选测量设置并回读核对；完成后报告模式与参数应用结果。",
  stop: "正在停止采集，等待采集线程完成写入并关闭 CSV；完成后报告样本数和保存目录。",
  single: "正在执行单次测量并逐点保存结果；完成后显示读数状态和实验文件位置。",
  disconnect: "正在停止串口读写并安全关闭连接；完成后连接状态会更新。",
};
const COMMAND_BUTTONS = {
  connect_real: ["connectReal"], connect_mock: ["connectMock"], disconnect: ["disconnect"],
  diagnose: ["diagnose"], zero: ["zero"], start: ["start"], single: ["single"],
  pause: ["pause"], resume: ["resume"], stop: ["stop"], configure: ["applySettings"],
  speed: ["applySpeed"], control_mode: ["applyControlMode"], event: ["markEvent"],
  firmware_versions: ["outguardSoftware", "inguardSoftware"], open_experiment_directory: ["openExperimentDir"], shutdown: ["shutdownService"],
};

function renderCommandProgress() {
  const pending = [...app.pendingCommands];
  const status = el("commandStatus");
  status.classList.toggle("hidden", pending.length === 0);
  status.textContent = pending.map((action) => {
    const label = COMMAND_LABELS[action] || "操作";
    if (COMMAND_PROGRESS_DETAILS[action]) return COMMAND_PROGRESS_DETAILS[action];
    return action.startsWith("connect_")
      ? `${label}正在等待打开串口、识别仪器和配置回读…`
      : `${label}进行中…`;
  }).join("；");
  for (const [action, ids] of Object.entries(COMMAND_BUTTONS)) {
    for (const id of ids) {
      const button = el(id);
      const busy = app.pendingCommands.has(action);
      if (busy) {
        if (!app.commandOriginalDisabled.has(id)) app.commandOriginalDisabled.set(id, button.disabled);
        button.disabled = true;
      } else if (app.commandOriginalDisabled.has(id)) {
        button.disabled = app.commandOriginalDisabled.get(id);
        app.commandOriginalDisabled.delete(id);
      }
    }
  }
  const connectionPending = pending.some((action) => action.startsWith("connect_"));
  el("connectionProgress").classList.toggle("hidden", !connectionPending);
  el("connectionProgress").textContent = connectionPending
    ? "正在打开连接并等待识别与配置回读；请勿重复点击。" : "";
}

function intervalMetricText(value) {
  const seconds = Number(value);
  if (!Number.isFinite(seconds) || seconds < 0) return "—";
  if (seconds >= 1) return `${numberText(seconds, 4)} s`;
  if (seconds >= 1e-3) return `${numberText(seconds * 1e3, 4)} ms`;
  return `${numberText(seconds * 1e6, 4)} µs`;
}

function serialFormDisabled(disabled) {
  for (const id of ["port", "portList", "baudrate", "bytesize", "parity", "stopbits", "eol", "flow", "serialTimeout", "scanPorts", "connectReal", "connectMock"]) el(id).disabled = disabled;
}

function manualSoftwareProfiles() {
  try {
    const value = JSON.parse(localStorage.getItem("8845a-front-panel-software") || "{}");
    if (!value || typeof value !== "object" || Array.isArray(value)) return {};
    return Object.fromEntries(Object.entries(value).flatMap(([serial, versions]) => {
      if (!serial || serial.length > 64 || !versions || typeof versions !== "object") return [];
      return [[serial, {
        outguard_sw: String(versions.outguard_sw || "").slice(0, 64),
        inguard_sw: String(versions.inguard_sw || "").slice(0, 64),
        source: String(versions.source || "仪表面板人工核对").slice(0, 40),
        updated_at: String(versions.updated_at || "").slice(0, 40),
      }]];
    }));
  } catch (_) { return {}; }
}

async function saveManualSoftware() {
  if (!app.state?.connected || app.state.simulation) return;
  const versions = {
    outguard_sw: el("outguardSoftware").value.trim(),
    inguard_sw: el("inguardSoftware").value.trim(),
  };
  const result = await command("firmware_versions", versions);
  if (!result.success || !app.activeDeviceSerial) return;
  const profiles = manualSoftwareProfiles();
  if (versions.outguard_sw || versions.inguard_sw) profiles[app.activeDeviceSerial] = {
    ...versions, source: "仪表面板人工核对", updated_at: new Date().toISOString(),
  };
  else delete profiles[app.activeDeviceSerial];
  try { localStorage.setItem("8845a-front-panel-software", JSON.stringify(profiles)); }
  catch (_) { toast("已发送面板版本记录，但浏览器无法保存到本机", true); }
  renderManualSoftwareInfo(profiles[app.activeDeviceSerial]);
}

function renderManualSoftwareInfo(profile) {
  const info = el("manualSoftwareInfo");
  if (!info) return;
  const hasVersion = !!(profile?.outguard_sw || profile?.inguard_sw);
  if (!hasVersion) info.textContent = "来源：仪表面板人工核对；尚无记录";
  else {
    const updated = profile.updated_at ? new Date(profile.updated_at) : null;
    const time = updated && Number.isFinite(updated.getTime()) ? updated.toLocaleString() : "时间未记录";
    info.textContent = `来源：仪表面板人工核对 · 更新时间：${time}`;
  }
  el("clearManualSoftware").disabled = !hasVersion || !app.state?.connected || !!app.state?.simulation;
}

async function clearManualSoftware() {
  if (!app.activeDeviceSerial || !app.state?.connected || app.state?.simulation) return;
  if (!window.confirm("清除当前仪器序列号对应的人工面板版本记录？这只会清除本机记录，不会修改仪器固件。")) return;
  const result = await command("firmware_versions", { outguard_sw: "", inguard_sw: "" });
  if (!result.success) return;
  el("outguardSoftware").value = "";
  el("inguardSoftware").value = "";
  const profiles = manualSoftwareProfiles();
  delete profiles[app.activeDeviceSerial];
  try { localStorage.setItem("8845a-front-panel-software", JSON.stringify(profiles)); }
  catch (_) { toast("仪表侧记录已清除，但浏览器无法更新本机缓存", true); }
  renderManualSoftwareInfo(null);
}

function serialPreferences() {
  return Object.fromEntries(["port", "baudrate", "bytesize", "parity", "stopbits", "eol", "flow", "serialTimeout", "outputRoot"]
    .map((key) => [key, el(key).value]));
}

function saveSerialPreferences() {
  try { localStorage.setItem("8845a-web-preferences", JSON.stringify(serialPreferences())); }
  catch (_) { /* A reconnect remains available for this session even if browser storage is disabled. */ }
}

function safetyPayload() {
  const mockConnected = app.mockConnectPending || (app.state?.connected && app.state?.simulation);
  return mockConnected ? {} : { safety_confirmed: ui.confirm.checked };
}

async function connectReal() {
  clearSafetyConfirmation();
  saveSerialPreferences();
  const result = await command("connect_real", {
    port: el("port").value.trim(), serial: serialPayload(), settings: settingsPayload(),
    front_panel_software_by_serial: manualSoftwareProfiles(),
  });
  if (result.success) {
    serialFormDisabled(true); app.awaitInitialSettings = true; app.mockConnectPending = false;
    try { localStorage.setItem("8845a-last-real-connection", "1"); } catch (_) { /* Session-only reconnect still works. */ }
  }
}

async function connectMock() {
  clearSafetyConfirmation();
  const result = await command("connect_mock", {
    settings: settingsPayload(), scenario: el("mockScenario").value,
  });
  if (result.success) {
    serialFormDisabled(true);
    app.awaitInitialSettings = true;
    app.mockConnectPending = true;
    el("safetyControls").classList.add("hidden");
    el("mockSafetyNote").classList.remove("hidden");
    el("mockScenario").disabled = true;
  }
}

async function applySettings() {
  const result = await command("configure", { ...safetyPayload(), settings: settingsPayload() });
  if (result.success) app.awaitSettingsReadback = true;
}

async function applySpeed() {
  const result = await command("speed", { settings: settingsPayload() });
  if (result.success) app.awaitSpeedReadback = true;
}

function startPayload() {
  return {
    ...safetyPayload(), settings: settingsPayload(), name: el("experimentName").value.trim(),
    root: el("outputRoot").value.trim(), interval_s: Number(el("interval").value),
  };
}

function experimentNameError(name) {
  const value = String(name ?? "").trim();
  if (!value) return "请填写实验名称。";
  if ([...value].length > 100) return "实验名称最多100个字符。";
  if (/[<>:"\/\\|?*\u0000-\u001f]/u.test(value)) return "实验名称不能包含 Windows 路径非法字符或控制字符。";
  return "";
}

function updateExperimentNameValidation() {
  const error = experimentNameError(el("experimentName").value);
  const message = el("experimentNameError");
  message.textContent = error;
  message.classList.toggle("hidden", !error);
  clearPreflightSummary();
  if (app.state) updateStateDisplay(app.state);
  else {
    el("start").disabled = !!error;
    el("single").disabled = !!error;
  }
  return error;
}

async function startContinuous() {
  await submitAcquisition("start");
}
async function startSingle() {
  await submitAcquisition("single");
}

function clearPreflightSummary() {
  const summary = el("preflightSummary");
  summary.textContent = "";
  summary.classList.remove("error");
}

async function submitAcquisition(action) {
  if (updateExperimentNameValidation() || app.startPending) return;
  app.startPending = true;
  const summary = el("preflightSummary");
  summary.classList.remove("error");
  summary.textContent = "正在核对仪器连接、配置回读、接线确认和保存目录…";
  if (app.state) updateStateDisplay(app.state);
  const payload = startPayload();
  try {
    const preflight = await command("preflight", payload);
    if (!preflight.success) {
      summary.textContent = `采集预检未通过：${preflight.message || "请检查配置和保存目录"}`;
      summary.classList.add("error");
      return;
    }
    const checks = Array.isArray(preflight.checks) ? preflight.checks.map(String) : [];
    summary.textContent = `采集预检通过：${checks.join(" · ")}`;
    if (preflight.duplicate_name) {
      const choice = await chooseDuplicateNameAction();
      if (choice === "rename") {
        summary.textContent = "请修改实验名称后重新开始；旧实验数据未修改。";
        el("experimentName").focus();
        el("experimentName").select();
        return;
      }
      if (choice !== "create-copy") {
        summary.textContent = "已取消启动；旧实验数据未修改。";
        return;
      }
      summary.textContent = "将创建新的独立目录；旧实验数据不会覆盖。";
    }
    await command(action, payload);
  } finally {
    app.startPending = false;
    if (app.state) updateStateDisplay(app.state);
    else {
      const nameError = !!experimentNameError(el("experimentName").value);
      el("start").disabled = nameError;
      el("single").disabled = nameError;
    }
  }
}

function chooseDuplicateNameAction() {
  const dialog = el("duplicateRunDialog");
  return new Promise((resolve) => {
    app.duplicateRunResolve = resolve;
    dialog.showModal();
  });
}

function finishDuplicateNameAction(choice) {
  const resolve = app.duplicateRunResolve;
  app.duplicateRunResolve = null;
  el("duplicateRunDialog").close();
  resolve?.(choice);
}

async function scanPorts() {
  const button = el("scanPorts");
  button.disabled = true;
  el("portScanStatus").textContent = "正在扫描 Windows 串口…也可以继续手动输入 COM 端口。";
  try {
    const response = await fetch("/api/ports", { cache: "no-store" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const result = await response.json();
    const list = el("portList");
    app.portDescriptions = new Map(result.ports.map((port) => [String(port.device), String(port.description || "串口")]));
    list.innerHTML = `<option value="">选择已发现端口</option>` + result.ports.map((port) => `<option value="${htmlText(port.device)}">${htmlText(port.device)} · ${htmlText(port.description || "串口")}</option>`).join("");
    updatePortSelectionInfo();
    if (!result.ports.length) {
      el("portScanStatus").textContent = `未发现串口；可手动输入 COM 端口后连接。${result.error ? ` ${result.error}` : ""}`;
      toast("没有发现串口；仍可手动输入 COM 端口");
    } else {
      el("portScanStatus").textContent = `发现 ${result.ports.length} 个串口；列表显示端口号和设备描述，也可手动输入。`;
      toast(`发现 ${result.ports.length} 个串口`);
    }
  } catch (error) {
    el("portScanStatus").textContent = `串口扫描失败：${error.message}；可手动输入 COM 端口，或重试扫描。`;
    toast(`扫描失败：${error.message}`, true);
  } finally {
    button.disabled = false;
    el("port").disabled = !!app.state?.connected;
  }
}

function updatePortSelectionInfo(connectedState = app.state) {
  const port = String(el("port").value || "").trim();
  const description = app.portDescriptions.get(port);
  const info = el("portSelectionInfo");
  if (connectedState?.connected && connectedState.simulation) {
    info.textContent = "模拟器已连接（未打开串口）";
  } else if (connectedState?.connected) {
    const identity = connectedState.device?.idn || connectedState.connection_text || description || "设备已识别";
    info.textContent = `已连接 ${port || "串口"} · ${identity}`;
  } else {
    info.textContent = `准备连接：${port || "未选择端口"}${description ? ` · ${description}` : ""}`;
  }
}

function classifyLog(row) {
  const text = String(row?.text || "");
  if (String(row?.level || "").toUpperCase() === "ERROR" || /^!\s/.test(text) || /超时|异常|错误|failed|failure|timeout/i.test(text)) return "error";
  if (/^(?:\s*[<>]\s|\s*(?:\*|SENS:|CONF:|READ\?|MEAS:))/i.test(text) || /SCPI\s*(?:命令|发送|响应)/i.test(text)) return "scpi";
  if (/\bCOM\d+\b|串口|连接|断开|识别|设备/i.test(text)) return "connection";
  return "acquisition";
}

function filterLogRows(rows, filter = "all") {
  return filter === "all" ? rows.slice() : rows.filter((row) => classifyLog(row) === filter);
}

function formatLogsText(rows) {
  return rows.map((row) => {
    const stamp = row.time ? new Date(row.time).toLocaleString("zh-CN", { hour12: false }) : "";
    return `${stamp} [${classifyLog(row).toUpperCase()}] ${String(row.text || "").replace(/[\r\n]+/g, " ")}`.trim();
  }).join("\n");
}

function renderLogRows() {
  const rows = filterLogRows(app.logRows, app.logFilter);
  if (!rows.length) ui.logs.innerHTML = '<div class="empty-log">当前筛选没有日志记录。</div>';
  else ui.logs.innerHTML = rows.map((row) => {
    const time = row.time ? new Date(row.time).toLocaleTimeString("zh-CN", { hour12: false }) : "";
    return `<div class="log-entry${classifyLog(row) === "error" ? " error" : ""}"><time>${htmlText(time)}</time><span>${htmlText(row.text)}</span></div>`;
  }).join("");
  const count = el("logCount");
  if (count) count.textContent = app.logFilter === "all" ? `${app.logRows.length} 条` : `${rows.length} / ${app.logRows.length} 条`;
}

function appendLogRows(rows) {
  if (!rows?.length) return;
  app.logRows.push(...rows.map((row) => ({ ...row, category: classifyLog(row) })));
  if (app.logRows.length > 300) app.logRows.splice(0, app.logRows.length - 300);
  renderLogRows();
  ui.logs.scrollTop = ui.logs.scrollHeight;
}

function exportFilteredLogs() {
  const rows = filterLogRows(app.logRows, app.logFilter);
  if (!rows.length) { toast("当前筛选没有日志可导出", true); return; }
  const blob = new Blob([formatLogsText(rows)], { type: "text/plain;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = `8845A_${app.logFilter}_logs_${localExperimentName().replace(/^实验_/, "")}.txt`;
  link.click();
  URL.revokeObjectURL(url);
  toast(`已导出 ${rows.length} 条日志`);
}

function appendSampleRows(rows) {
  if (!rows?.length) return;
  for (const row of rows) {
    const id = Number(row.sample_index);
    if (Number.isFinite(id) && id <= app.lastSample) continue;
    const value = Number(row.value_si);
    const point = {
      id: Number.isFinite(id) ? id : app.points.length + 1,
      x: Number(row.elapsed_s), y: Number.isFinite(value) ? value : null,
      status: row.status, raw: row.raw_reading, mode: row.mode, unit: row.unit_si,
      range: row.range_si, timestamp: row.timestamp_local, error: row.error,
    };
    app.points.push(point);
    if (point.status !== "ok") app.latestInvalidPoint = point;
    app.lastSample = Math.max(app.lastSample, Number.isFinite(id) ? id : app.lastSample);
  }
  if (app.points.length > 5000) app.points.splice(0, app.points.length - 5000);
  updateAccessiblePointTable();
}

async function pollState() {
  if (app.pollInFlight) return;
  app.pollInFlight = true;
  const parameters = new URLSearchParams({ since_sample: String(app.lastSample), since_log: String(app.lastLog) });
  if (app.generation !== null) parameters.set("generation", String(app.generation));
  try {
    const response = await fetch(`/api/state?${parameters}`, { cache: "no-store" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const state = await response.json();
    setServiceStatus(true);
    app.mockConnectPending = false;
    const wasConnected = !!app.state?.connected;
    if (app.generation !== null && state.generation !== app.generation) {
      app.points = [];
      app.latestInvalidPoint = null;
      app.lastSample = 0;
      app.imported = null;
      app.importedFileName = "";
      app.xView = null;
      app.yView = null;
      app.bufferTruncated = false;
      el("restoreLive").classList.add("hidden");
    }
    app.generation = state.generation;
    if (state.sample_buffer_start > 0 && app.lastSample > 0 && state.sample_buffer_start > app.lastSample + 1) app.bufferTruncated = true;
    appendSampleRows(state.samples);
    appendLogRows(state.logs);
    app.lastLog = Math.max(app.lastLog, Number(state.log_seq || 0));
    app.state = state;
    if (wasConnected && !state.connected) clearSafetyConfirmation();
    updateStateDisplay(state);
    if (state.events && !app.imported) renderEvents(state.events);
    if (state.logs?.some((line) => String(line.text).startsWith("! "))) {
      // The actual error remains in the status panel; avoid repeated popups during polling.
    }
    drawChart();
    updateStats();
  } catch (error) {
    setServiceStatus(false, error.message);
  } finally {
    app.pollInFlight = false;
  }
}

function updateStateDisplay(state) {
  updateSafetyReadback(state);
  app.state = state;
  syncSettingsReadback(state);
  const device = state.device || {};
  renderIdnFirmwareInfo(state, device);
  applyDigitalFilterNplcGuard(state);
  const serialNumber = state.connected && !state.simulation ? String(device.serial_number || "") : "";
  if (serialNumber !== app.activeDeviceSerial) {
    app.activeDeviceSerial = serialNumber;
    const stored = serialNumber ? manualSoftwareProfiles()[serialNumber] || device.front_panel_software || {} : {};
    el("outguardSoftware").value = String(stored.outguard_sw || "");
    el("inguardSoftware").value = String(stored.inguard_sw || "");
    renderManualSoftwareInfo(stored);
  }
  const badge = el("connectionBadge");
  const badgeText = badge.querySelector("span");
  badge.classList.toggle("connected", state.connected && !state.simulation);
  badge.classList.toggle("mock", state.connected && state.simulation);
  badgeText.textContent = !state.connected ? "未连接" : state.simulation ? "MOCK 模拟" : `已连接 · 8845A / ${state.control_mode === "local" ? "LOCAL" : "COMPUTER"}`;
  el("connectionText").textContent = state.connection_text || "串口尚未打开";
  updatePortSelectionInfo(state);
  el("connectionText").parentElement.classList.toggle("is-connected", !!state.connected);
  const connectedPending = app.pendingCommands.has("connect_real") || app.pendingCommands.has("connect_mock");
  const connectionProgress = String(state.connection_progress || "");
  if (!connectedPending) {
    el("connectionProgress").textContent = connectionProgress;
    el("connectionProgress").classList.toggle("hidden", !connectionProgress);
  }
  const connectionStages = el("connectionStages");
  connectionStages.replaceChildren();
  const stageNames = { device: "设备已选", port: "串口", identify: "8845A识别", configuration: "配置读取", ready: "就绪" };
  const stageStates = { running: "进行中", success: "成功", failure: "失败", skipped: "模拟跳过" };
  for (const stage of (Array.isArray(state.connection_stages) ? state.connection_stages : [])) {
    const item = document.createElement("li");
    item.dataset.status = String(stage.status || "");
    item.textContent = `${stageNames[stage.stage] || String(stage.stage || "连接")}: ${stageStates[stage.status] || "待开始"}`;
    item.title = String(stage.message || "");
    connectionStages.append(item);
  }
  el("disconnect").classList.toggle("hidden", !state.connected);
  let hasSavedRealConnection = false;
  try { hasSavedRealConnection = localStorage.getItem("8845a-last-real-connection") === "1"; } catch (_) { /* Optional browser preference. */ }
  el("reconnectReal").classList.toggle("hidden", !!state.connected || !!state.simulation || !hasSavedRealConnection);
  el("connectReal").classList.toggle("hidden", !!state.connected);
  el("connectMock").classList.toggle("hidden", !!state.connected);
  el("mockScenarioRow").classList.toggle("hidden", !!state.connected && !state.simulation);
  el("mockScenario").disabled = !!state.connected || app.mockConnectPending;
  const scenario = state.simulation
    ? String(state.mock_scenario?.name ?? state.mock_scenario ?? "")
    : "";
  if (scenario) {
    el("mockScenario").value = scenario;
    el("scenarioBadge").textContent = `模拟 · ${scenario}`;
  } else {
    el("scenarioBadge").textContent = "";
  }
  el("scenarioBadge").classList.toggle("hidden", !scenario);
  const mockConnected = !!state.connected && !!state.simulation;
  el("safetyControls").classList.toggle("hidden", mockConnected);
  el("mockSafetyNote").classList.toggle("hidden", !mockConnected);
  serialFormDisabled(!!state.connected);
  el("controlMode").value = state.control_mode || "disconnected";
  el("controlModeStatus").textContent = !state.connected
    ? "未连接，无法读取仪表控制状态。"
    : state.simulation
      ? "MOCK 模拟器没有实际仪表面板控制状态；当前控制方式不代表真实仪器回读。"
      : state.control_mode === "local"
        ? "Local 命令已发送；Local 下 SCPI 控制状态和测量配置无法回读。请以仪表面板为准；切回 Computer 后软件会重新读取。"
        : state.control_mode === "computer"
          ? "Computer 模式：软件已切换到远程控制，并读取仪器身份与当前配置。"
          : "当前控制方式未知，无法确认仪表是否接受。请查看面板状态。";
  const activeModeKey = state.active_settings?.mode;
  const modeApplied = !state.connected || !activeModeKey || activeModeKey === app.selectedMode;
  const badgeName = state.configuration?.function || activeModeKey || "未应用";
  el("modeFunction").textContent = state.connected
    ? (state.simulation ? (modeApplied ? "模拟配置" : `当前 ${activeModeKey}`) : modeApplied ? `回读 ${badgeName}` : `待应用 · 当前 ${activeModeKey}`)
    : "待连接";
  el("modeFunction").classList.toggle("is-active", !!state.connected);
  el("runStatus").textContent = state.status || "等待连接";
  el("runDot").className = `run-dot${String(state.status).includes("暂停") ? " paused" : /(采集中|测量中)/.test(state.status || "") ? " running" : ""}`;
  const telemetry = state.stats || {};
  const latest = state.latest;
  const mode = latest?.mode ? app.modes.get(latest.mode) : activeMode();
  if (latest) {
    const scaled = scaledValue(latest.value_si, latest.unit_si || mode?.unit);
    el("readingValue").textContent = scaled.value;
    el("readingUnit").textContent = scaled.unit;
    el("readingMode").textContent = mode?.label || latest.mode || "测量读数";
    const actualRange = latest.range_si === null || latest.range_si === undefined || latest.range_si === ""
      ? "实际量程未回读" : `实际量程 ${rangeLabel(latest.range_si, latest.unit_si || mode?.unit || "")}`;
    el("readingRaw").textContent = `原始响应 ${latest.raw_reading || "—"}  ·  ${actualRange}  ·  ${latest.timestamp_local || ""}`;
    el("sampleCount").textContent = String(telemetry.valid_count ?? app.points.filter((point) => point.status === "ok").length);
    const elapsedSeconds = Number(latest.elapsed_s);
    el("elapsed").textContent = Number.isFinite(elapsedSeconds) ? durationText(elapsedSeconds) : "—";
  } else {
    el("readingValue").textContent = "—";
    el("readingUnit").textContent = "";
    el("readingMode").textContent = mode?.label || "等待仪器读数";
    el("readingRaw").textContent = state.last_error ? `最近异常：${state.last_error}` : "原始响应将保存在实验 CSV";
    el("sampleCount").textContent = String(telemetry.valid_count ?? 0);
    el("elapsed").textContent = "—";
  }
  const samplingInterval = samplingIntervalText(state.actual_hz);
  const samplingUnit = samplingInterval.unit ? ` ${samplingInterval.unit}` : "";
  const oneValidPoint = Number(telemetry.valid_count) === 1;
  const noIntervalYet = oneValidPoint && !(Number(state.actual_hz) > 0);
  el("actualRate").textContent = noIntervalYet ? "等待第二个有效读数" : `${samplingInterval.value}${samplingUnit}`;
  el("attemptCount").textContent = String(telemetry.attempt_count ?? 0);
  el("validCount").textContent = String(telemetry.valid_count ?? 0);
  el("abnormalCount").textContent = String(telemetry.abnormal_count ?? 0);
  el("intervalMedian").textContent = noIntervalYet ? "等待第二个有效读数" : intervalMetricText(telemetry.interval_median_s);
  el("intervalMin").textContent = intervalMetricText(telemetry.interval_min_s);
  el("intervalMax").textContent = intervalMetricText(telemetry.interval_max_s);
  el("intervalJitter").textContent = intervalMetricText(telemetry.interval_range_jitter_s);
  renderSettingsComparison(state);
  updateCurrentRangeGuidance(state);
  const directory = state.experiment_dir || state.last_experiment_dir;
  el("experimentPath").textContent = directory || "实验目录尚未创建";
  el("experimentPathDetail").textContent = directory || "实验目录将在开始测量后创建";
  el("experimentFiles").textContent = directory
    ? "当前目录：measurements.csv · events.csv · config.json · run.log；曲线 PNG 按需另存。"
    : "采集后包含：measurements.csv · events.csv · config.json · run.log；曲线 PNG 按需另存。";
  el("openExperimentDir").disabled = !directory;
  el("copyExperimentPath").disabled = !directory;
  el("downloadCsv").disabled = !state.last_experiment_dir;
  el("downloadCsv").textContent = state.experiment_dir ? "下载进行中快照 CSV" : state.last_experiment_dir ? "下载最近已保存 CSV" : "暂无可下载 CSV";
  el("downloadEventsSafe").disabled = !state.last_experiment_dir || !!state.experiment_dir;
  el("downloadCsv").title = state.experiment_dir
    ? "下载时生成进行中快照，只包含最后一条完整 CSV 记录；响应会标明服务器快照时间。"
    : state.last_experiment_dir ? "下载最近一次已完成并保存的原始 CSV。" : "开始并保存一次实验后即可下载原始 CSV。";
  const activeExperiment = !!state.experiment_dir;
  renderAcquisitionFeedback(state, telemetry, activeExperiment);
  el("markEvent").disabled = !activeExperiment;
  el("markEvent").title = activeExperiment ? "记录带有当前样本号和读数的实验事件。" : "请先启动实验，再记录事件。";
  const invalid = app.latestInvalidPoint;
  const latestSampleIndex = Number(state.latest?.sample_index ?? state.latest?.id);
  const invalidSampleIndex = Number(invalid?.id ?? invalid?.sample_index);
  const recoveredAfterInvalid = state.latest?.status === "ok"
    && Number.isFinite(latestSampleIndex)
    && Number.isFinite(invalidSampleIndex)
    && latestSampleIndex > invalidSampleIndex;
  const criticalError = recoveredAfterInvalid ? "" : String(state.last_error || "").trim();
  const invalidText = invalid && !recoveredAfterInvalid
    ? `最近无效样本 #${invalid.id} · ${invalid.status || "无效"} · ${invalid.error || invalid.raw || "仪器未返回有效读数"}`
    : "";
  const failureText = invalidText || (criticalError ? `最近异常：${criticalError}` : "");
  el("criticalError").textContent = failureText ? `${failureText} · 查看日志` : "";
  el("criticalError").classList.toggle("hidden", !failureText);
  const zeroResult = String(state.zero_result || "").trim();
  el("zeroResult").textContent = zeroResult;
  el("zeroResult").classList.toggle("hidden", !zeroResult);
  const running = /采集中|测量中/.test(state.status || "");
  const paused = String(state.status || "").includes("暂停");
  const acquiring = running || paused;
  const nameError = experimentNameError(el("experimentName").value);
  el("experimentNameError").textContent = nameError;
  el("experimentNameError").classList.toggle("hidden", !nameError);
  el("outguardSoftware").disabled = !state.connected || state.simulation || acquiring;
  el("inguardSoftware").disabled = !state.connected || state.simulation || acquiring;
  el("clearManualSoftware").disabled = !state.connected || state.simulation || acquiring ||
    !(el("outguardSoftware").value.trim() || el("inguardSoftware").value.trim());
  const computerControl = !state.connected || state.simulation || state.control_mode === "computer";
  el("controlMode").disabled = !state.connected || state.simulation || acquiring;
  el("applyControlMode").disabled = !state.connected || state.simulation || acquiring;
  el("start").disabled = !state.connected || !computerControl || acquiring || !!nameError || app.startPending;
  el("single").disabled = !state.connected || !computerControl || acquiring || !!nameError || app.startPending;
  const singleMeasurement = String(state.status || "").includes("单次测量中");
  const continuousRunning = running && !singleMeasurement;
  el("pause").disabled = !continuousRunning;
  el("pause").title = singleMeasurement
    ? "单次测量不支持暂停；可停止当前读数。"
    : continuousRunning
      ? "暂停当前连续采集。"
      : "当前没有连续采集；开始连续采集后可暂停。";
  el("pause").classList.toggle("hidden", paused);
  el("resume").classList.toggle("hidden", !paused);
  el("resume").title = paused ? "继续当前已暂停的采集。" : "当前没有已暂停的采集。";
  el("stop").disabled = !running && !paused;
  el("stop").title = singleMeasurement
    ? "停止当前单次测量并保存已写入的数据。"
    : running || paused
      ? "停止当前采集并保存已写入的数据。"
    : "当前没有进行中的采集；开始连续采集后可停止并保存。";
  el("applySettings").disabled = !state.connected || !computerControl || acquiring;
  el("disconnect").disabled = acquiring;
  const settingsDisabled = acquiring || !computerControl;
  el("mode").disabled = settingsDisabled;
  el("autorange").disabled = settingsDisabled;
  el("range").disabled = settingsDisabled || el("autorange").checked;
  if (el("analogFilter")) el("analogFilter").disabled = settingsDisabled;
  if (el("inputImpedanceAuto")) el("inputImpedanceAuto").disabled = settingsDisabled;
  if (el("digitalFilter")) {
    el("digitalFilter").checked = false;
    el("digitalFilter").disabled = true;
  }
  el("autozero").disabled = settingsDisabled;
  if (el("nplc")) el("nplc").disabled = !computerControl;
  if (el("bandwidth")) el("bandwidth").disabled = !computerControl;
  const active = activeMode();
  const canTune = active && (active.nplc.length || active.bandwidths.length);
  el("applySpeed").disabled = !state.connected || !computerControl || !canTune || !acquiring;
  const applyHint = !computerControl && state.connected
    ? "Local 模式下不发送配置；切回 Computer 后可继续软件测量。"
    : acquiring && canTune
      ? "采集中：适用的积分时间或带宽可编辑，点“应用速度”后生效。"
      : acquiring
        ? "采集中：当前模式没有适用的速度调整项。"
        : state.connected
          ? "采集前：选择参数后点“应用仪器参数”。"
          : "可先选择模式与参数；连接仪器或模拟器后应用。";
  el("settingsApplyHint").textContent = applyHint;
  el("zero").disabled = !state.connected || !computerControl || acquiring;
  const localPhysicalControl = !!state.connected && !state.simulation && !computerControl;
  el("diagnose").disabled = localPhysicalControl;
  el("diagnose").title = localPhysicalControl
    ? "Local 模式下不运行通信诊断，也不发送 SCPI 查询；切换到 Computer 后再诊断。"
    : "读取仪器身份和当前通信/配置状态，不启动采集。";
  el("controlModeHelp").textContent = "Computer：软件可发送 SCPI 命令、配置和采集，仪表 RS-232 协议须设为 COMPUTER。Local：仪表面板控制，软件采集、配置、ZERO 和诊断均禁用，串口连接保留。只能在停止采集后切换。";
  const lockReason = !state.connected ? "请先连接仪器或模拟器。"
    : !computerControl ? "仪表处于 Local 模式；切换到 Computer 后可操作。"
      : acquiring ? "采集中不能更改仪器参数。" : "";
  for (const id of ["mode", "autorange", "range", "analogFilter", "inputImpedanceAuto", "autozero", "applySettings", "zero"]) {
    const control = el(id);
    if (control.disabled) control.title = lockReason;
  }
  el("start").title = nameError || (!state.connected ? "请先连接仪器或模拟器。"
    : !computerControl ? "Local 模式下不能启动软件采集。"
      : acquiring ? "已有采集正在运行。" : "开始持续采集并逐点保存 CSV。");
  el("single").title = el("start").title.replace("持续采集", "单次测量");
  el("zero").title = lockReason || "ZERO 是仪器内部零点校正，不是读数归零或校准调整。";
  if (state.last_error && state.last_error !== app.lastShownError) {
    app.lastShownError = state.last_error;
    // Errors stay visible inline and in the log, without interrupting long sessions.
  }
  if (state.simulation) document.title = "Fluke 8845A · MOCK 模拟 · 实验测量台";
  else document.title = "Fluke 8845A · 实验测量台";
  updateChartAccessibilitySummary(state);
}

function applyDigitalFilterNplcGuard(state) {
  const select = el("nplc");
  if (!select) return;
  const blockedValues = state?.device?.digital_filter_blocked_nplc_values?.[app.selectedMode] || [];
  const blocked = !!state?.connected && !state?.simulation && blockedValues.length > 0;
  const options = select.options || [];
  for (let index = 0; index < options.length; index += 1) {
    const option = options[index];
    option.disabled = blocked && blockedValues.includes(Number(option.value));
  }
  select.title = blocked
    ? (state?.device?.digital_filter_slow_read_block_reasons?.[app.selectedMode] || "")
    : "";
}

function syncSettingsReadback(state) {
  const active = state.active_settings;
  if (!state.connected || !active?.mode || (!app.awaitInitialSettings && !app.awaitSettingsReadback && !app.awaitSpeedReadback)) return;
  const spec = app.modes.get(active.mode);
  if (!spec) return;
  if (app.awaitInitialSettings || app.awaitSettingsReadback) {
    if (app.selectedMode !== active.mode) {
      el("mode").value = active.mode;
      populateModeOptions(spec);
    }
    el("autorange").checked = !!active.autorange;
    el("range").disabled = !!active.autorange;
    if (active.range_si !== null && active.range_si !== undefined) el("range").value = String(active.range_si);
    if (spec.nplc.length && active.nplc !== null && active.nplc !== undefined) el("nplc").value = String(active.nplc);
    if (spec.bandwidths.length && active.ac_bandwidth_hz !== null && active.ac_bandwidth_hz !== undefined) el("bandwidth").value = String(active.ac_bandwidth_hz);
    if (spec.analog_filter) el("analogFilter").checked = !!active.analog_filter;
    if (spec.digital_filter) el("digitalFilter").checked = false;
    el("autozero").checked = !!active.autozero;
    if (spec.input_impedance_auto && el("inputImpedanceAuto")) {
      el("inputImpedanceAuto").checked = !!active.input_impedance_auto;
      app.dcvInputImpedanceAuto = !!active.input_impedance_auto;
    }
    app.awaitInitialSettings = false;
    app.awaitSettingsReadback = false;
  } else if (app.awaitSpeedReadback && app.selectedMode === active.mode) {
    if (spec.nplc.length && active.nplc !== null && active.nplc !== undefined) el("nplc").value = String(active.nplc);
    if (spec.bandwidths.length && active.ac_bandwidth_hz !== null && active.ac_bandwidth_hz !== undefined) el("bandwidth").value = String(active.ac_bandwidth_hz);
  }
  app.awaitSpeedReadback = false;
}

function renderSettingsComparison(state) {
  const container = el("settingsComparison");
  if (!container) return;
  const pendingMode = el("mode").value || app.selectedMode;
  const pendingSpec = app.modes.get(pendingMode);
  if (!pendingSpec) { container.textContent = "설정 선택 후 적용 상태가 표시됩니다."; return; }
  const pending = settingsPayload();
  const actual = state?.active_settings || {};
  const modeLabel = (value) => app.modes.get(value)?.label || value || "未回读";
  const rangeLabelFor = (settings, spec) => settings?.autorange
    ? "自动量程" : settings?.range_si === null || settings?.range_si === undefined
      ? "未回读" : rangeLabel(settings.range_si, spec?.unit || "");
  const timingLabel = (settings, spec) => spec?.bandwidths?.length
    ? settings?.ac_bandwidth_hz === null || settings?.ac_bandwidth_hz === undefined ? "未回读" : `${settings.ac_bandwidth_hz} Hz 带宽`
    : spec?.nplc?.length ? settings?.nplc === null || settings?.nplc === undefined ? "未回读" : `${settings.nplc} NPLC`
      : "不适用";
  const values = [
    ["模式", modeLabel(actual.mode), modeLabel(pending.mode)],
    ["量程", rangeLabelFor(actual, app.modes.get(actual.mode)), rangeLabelFor(pending, pendingSpec)],
    ["积分 / 带宽", timingLabel(actual, app.modes.get(actual.mode)), timingLabel(pending, pendingSpec)],
    ["模拟滤波", actual.analog_filter === undefined ? "未回读" : actual.analog_filter ? "开" : "关", pending.analog_filter ? "开" : "关"],
    ...(pendingSpec.input_impedance_auto ? [[
      "自动高输入阻抗",
      actual.input_impedance_auto === undefined ? "未回读" : actual.input_impedance_auto ? "开" : "关（10 MΩ）",
      pending.input_impedance_auto ? "开（仅100 mV/1 V/10 V为>10 GΩ）" : "关（10 MΩ）",
    ]] : []),
    ...(pendingSpec.digital_filter || app.modes.get(actual.mode)?.digital_filter ? [[
      "数字滤波",
      actual.digital_filter_state === "readback_on" ? "开"
        : actual.digital_filter_state === "readback_off" ? "关"
          : actual.digital_filter_state === "unprobed_inactive" ? "未探测 / 当前不活动"
            : actual.digital_filter_state === "unprobed" ? "未探测 / 慢速设置受限"
            : actual.digital_filter_state === "simulation" ? "MOCK 模拟"
              : "未回读 / 当前不可用",
      pending.digital_filter ? "开" : "关",
    ]] : []),
    ["自动零点", actual.autozero === undefined ? "未回读" : actual.autozero ? "开" : "关", pending.autozero ? "开" : "关"],
  ];
  const unavailable = !state?.connected || !actual.mode;
  container.innerHTML = `<div class="settings-comparison-heading"><strong>仪器设置核对</strong><span>${unavailable ? "当前没有仪器回读" : "显示仪器回读与当前选择"}</span></div><div class="settings-comparison-grid"><span></span><strong>当前生效</strong><strong>待应用</strong>${values.map(([name, active, wanted]) => {
    const differs = !unavailable && active !== wanted;
    return `<span>${htmlText(name)}</span><span>${htmlText(unavailable ? "未回读" : active)}</span><span class="${differs ? "setting-diff" : ""}">${htmlText(wanted)}${differs ? " · 不同" : ""}</span>`;
  }).join("")}</div>`;
}

function durationText(seconds) {
  const s = Math.max(0, Math.floor(seconds));
  if (s < 60) return `${s} 秒`;
  if (s < 3600) return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
  return `${Math.floor(s / 3600)}:${String(Math.floor(s / 60) % 60).padStart(2, "0")}:${String(s % 60).padStart(2, "0")}`;
}

function dataPoints() {
  return app.imported || app.points;
}

function validPoints() { return dataPoints().filter((point) => point.status === "ok" && Number.isFinite(point.x) && Number.isFinite(point.y)); }

function updateStats() {
  const points = validPoints();
  const spec = app.imported ? { unit: app.imported.unit } : activeMode();
  const unit = points[0]?.unit || spec?.unit || "";
  const labelUnit = unit === "Ohm" ? "Ω" : unit || "—";
  configureBaselineUnits(unit);
  if (el("baselineUnit")) el("baselineUnit").textContent = `${labelUnit}（SI 基本单位）`;
  if (el("analysisContext")) {
    const source = app.imported ? "当前历史文件" : (app.bufferTruncated ? "当前缓存（早期点已滚动）；原始 CSV 保留全程数据" : "当前实时缓存");
    el("analysisContext").textContent = `统计范围：${source}中的全部有效读数 · n=${points.length} · 单位 ${labelUnit}`;
  }
  for (const id of ["statMeanCount", "statStdCount", "statMinCount", "statMaxCount"]) {
    el(id).textContent = `n=${points.length}`;
  }
  if (!points.length) {
    for (const id of ["statMean", "statStd", "statMin", "statMax"]) el(id).textContent = "—";
    updateBaseline(null, spec?.unit || "");
    updateChartAccessibilitySummary();
    return;
  }
  let sum = 0, min = Infinity, max = -Infinity;
  for (const point of points) { sum += point.y; min = Math.min(min, point.y); max = Math.max(max, point.y); }
  const mean = sum / points.length;
  let squares = 0;
  for (const point of points) squares += (point.y - mean) ** 2;
  const std = points.length > 1 ? Math.sqrt(squares / (points.length - 1)) : 0;
  el("statMean").textContent = statWithUnit(mean, unit);
  el("statStd").textContent = statWithUnit(std, unit);
  el("statMin").textContent = statWithUnit(min, unit);
  el("statMax").textContent = statWithUnit(max, unit);
  if (!el("baselineValue").value && app.baseline === null && app.state?.connected && points.length === 1) updateBaseline(null, unit);
  updateBaseline(mean, unit);
  updateChartAccessibilitySummary();
}

function statWithUnit(value, unit) {
  const scaled = scaledValue(value, unit);
  return `${scaled.value} ${scaled.unit}`.trim();
}

function formatLocalDateTime(value) {
  if (!value) return "—";
  const date = new Date(value);
  if (!Number.isFinite(date.getTime())) return "—";
  return date.toLocaleString("zh-CN", {
    year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false,
  }).replaceAll("/", "-");
}

function updateAccessiblePointTable() {
  const body = el("pointTableBody");
  if (!body) return;
  const rows = dataPoints().slice(-50);
  if (!rows.length) {
    body.innerHTML = '<tr><td colspan="5">等待测量数据</td></tr>';
    updateChartAccessibilitySummary();
    return;
  }
  body.innerHTML = rows.map((point) => {
    const value = Number(point.y);
    const converted = point.status === "ok" && Number.isFinite(value)
      ? scaledValue(value, point.unit || activeMode()?.unit || "")
      : { value: "—", unit: "" };
    const convertedText = `${converted.value} ${converted.unit}`.trim();
    const status = point.status === "ok" ? "有效" : `${point.status || "异常"}${point.error ? ` · ${point.error}` : ""}`;
    const pointSpec = app.modes.get(point.mode) || activeMode();
    const range = point.range === null || point.range === undefined || point.range === "" ? "" : ` · ${rangeLabel(point.range, point.unit || pointSpec?.unit || "")}`;
    return `<tr><td>#${htmlText(point.id)}</td><td>${htmlText(formatLocalDateTime(point.timestamp))}</td><td>${htmlText(`${point.raw || "—"}${range}`)}</td><td>${htmlText(convertedText)}</td><td>${htmlText(status)}</td></tr>`;
  }).join("");
  updateChartAccessibilitySummary();
}

function updateChartAccessibilitySummary(state = app.state) {
  const summary = el("plotAccessibilitySummary");
  if (!summary) return;
  const attempts = dataPoints();
  let latest = null;
  for (let index = attempts.length - 1; index >= 0; index--) {
    const point = attempts[index];
    if (point.status === "ok" && Number.isFinite(point.x) && Number.isFinite(point.y)) { latest = point; break; }
  }
  const modeKey = latest?.mode || attempts.at(-1)?.mode || app.selectedMode;
  const mode = app.modes.get(modeKey) || activeMode();
  const unit = latest?.unit || mode?.unit || "";
  const parts = [`${mode?.label || modeKey || "测量"}曲线`, app.imported ? `历史文件 ${app.importedFileName || "已加载 CSV"}` : "实时数据"];
  const actual = samplingIntervalText(state?.actual_hz);
  parts.push(`采集状态 ${state?.status || "等待连接"}`);
  if (actual.unit) parts.push(`实测间隔 ${actual.value} ${actual.unit}`);
  const indexedCount = app.chartScale?.rows?.length;
  const validCount = Number(state?.stats?.valid_count);
  const displayedValidCount = Number.isFinite(indexedCount)
    ? indexedCount : Number.isFinite(validCount) ? validCount : validPoints().length;
  parts.push(`${displayedValidCount} 个有效读数，${attempts.length} 次测量尝试`);
  if (latest) {
    const displayed = scaledValue(latest.y, unit);
    parts.push(`最新读数样本 ${latest.id}：${displayed.value} ${displayed.unit}，${formatLocalDateTime(latest.timestamp)}`);
    const scope = el("analysisContext")?.textContent || "当前显示数据";
    parts.push(`${scope}；平均值 ${el("statMean")?.textContent || "—"}；样本标准差 ${el("statStd")?.textContent || "—"}；最小值 ${el("statMin")?.textContent || "—"}；最大值 ${el("statMax")?.textContent || "—"}`);
    const scale = app.chartScale;
    const start = Number.isFinite(scale?.xMin) ? scale.xMin : attempts.find((point) => point.status === "ok" && Number.isFinite(point.x) && Number.isFinite(point.y))?.x ?? latest.x;
    const end = Number.isFinite(scale?.xMax) ? scale.xMax : latest.x;
    parts.push(`当前视图时间范围 t=${timeLabel(start)} 至 ${timeLabel(end)}`);
  } else {
    parts.push("尚无有效读数");
  }
  const baseline = app.baseline;
  if (Number.isFinite(baseline)) parts.push(`基准值 ${statWithUnit(baseline, unit)}`);
  parts.push(`展开数据表可查看最近 ${Math.min(attempts.length, 50)} 条测量尝试的样本号、本地时间、原始响应、换算值和状态；可复制为制表文本`);
  summary.textContent = parts.join("。 ");
}

async function copyAccessiblePointTable() {
  const rows = dataPoints().slice(-50);
  const lines = ["样本\t本地时间\t原始响应 / 实际量程\t换算值\t状态"];
  for (const point of rows) {
    const value = Number(point.y);
    const converted = point.status === "ok" && Number.isFinite(value)
      ? scaledValue(value, point.unit || activeMode()?.unit || "")
      : { value: "—", unit: "" };
    const status = point.status === "ok" ? "有效" : `${point.status || "异常"}${point.error ? ` · ${point.error}` : ""}`;
    const pointSpec = app.modes.get(point.mode) || activeMode();
    const range = point.range === null || point.range === undefined || point.range === "" ? "" : ` · ${rangeLabel(point.range, point.unit || pointSpec?.unit || "")}`;
    lines.push([`#${point.id}`, formatLocalDateTime(point.timestamp), `${point.raw || "—"}${range}`, `${converted.value} ${converted.unit}`.trim(), status]
      .map((cell) => String(cell).replace(/[\t\r\n]/g, " ")).join("\t"));
  }
  try {
    await navigator.clipboard.writeText(lines.join("\n"));
    toast("最近 50 条测量尝试已复制为制表文本");
  } catch (error) {
    toast(`复制失败：${error.message || "浏览器未授予剪贴板权限"}`, true);
  }
}

function updateBaseline(mean, unit) {
  if (!Number.isFinite(app.baseline)) {
    el("baselineDelta").textContent = "尚未设置基准值";
    el("baselineDelta").classList.remove("is-set");
    updateChartAccessibilitySummary();
    return;
  }
  const latest = validPoints().at(-1);
  if (!latest) { updateChartAccessibilitySummary(); return; }
  const delta = latest.y - app.baseline;
  const relative = app.baseline === 0 ? null : delta / Math.abs(app.baseline) * 100;
  el("baselineDelta").textContent = `Δ ${statWithUnit(delta, unit)}${relative === null ? " · 基准为 0" : ` · ${numberText(relative, 5)}%`}`;
  el("baselineDelta").classList.add("is-set");
  updateChartAccessibilitySummary();
}

function renderEvents(events) {
  el("eventCount").textContent = `${events.length} 条`;
  if (!events.length) {
    ui.events.innerHTML = `<li class="empty-event">实验事件会按实际记录时间显示在这里。</li>`;
    return;
  }
  ui.events.innerHTML = events.slice().reverse().map((event, reversedIndex) => {
    const eventIndex = events.length - reversedIndex - 1;
    const elapsed = Number(event.elapsed_s);
    const value = event.value_si !== null && event.value_si !== undefined ? scaledValue(event.value_si, event.unit_si) : null;
    const sample = event.sample_index ? `#${event.sample_index}` : "";
    return `<li><button class="event-jump" type="button" data-event-index="${eventIndex}" title="定位图表到该事件"><time>t=${Number.isFinite(elapsed) ? durationText(elapsed) : "—"}</time></button><span>${htmlText(event.text)}</span><span class="event-value">${sample}${value ? ` · ${value.value} ${value.unit}` : ""}</span></li>`;
  }).join("");
}

function displayedEvents() {
  return app.imported ? (app.importedEvents || []) : (app.state?.events || []);
}

function focusEvent(index) {
  const events = displayedEvents();
  const event = events[index];
  const time = Number(event?.elapsed_s);
  if (!Number.isFinite(time)) return;
  const span = Math.max(1, (app.chartScale?.xMax - app.chartScale?.xMin) || 120);
  const start = Math.max(0, time - span / 2);
  app.xView = [start, start + span];
  el("autoFollow").checked = false;
  app.selectedEventIndex = index;
  el("pointCursor").textContent = `事件 · t=${timeLabel(time)} · ${formatLocalDateTime(event.timestamp_local)} · ${event.text}`;
  drawChart();
}

function nearestEvent(canvasX) {
  const scale = app.chartScale;
  if (!scale) return null;
  let closest = null;
  for (const marker of app.chartEventHits || []) {
    const distance = Math.abs(marker.x - canvasX);
    if (distance <= 7 && (!closest || distance < closest.distance)) closest = { ...marker, distance };
  }
  return closest;
}

function plotContext(canvas = ui.canvas) {
  const rect = canvas.getBoundingClientRect();
  const dpr = Math.max(1, window.devicePixelRatio || 1);
  const width = Math.max(1, rect.width);
  const height = Math.max(1, rect.height);
  if (canvas.width !== Math.round(width * dpr) || canvas.height !== Math.round(height * dpr)) {
    canvas.width = Math.round(width * dpr);
    canvas.height = Math.round(height * dpr);
  }
  const context = canvas.getContext("2d");
  context.setTransform(dpr, 0, 0, dpr, 0, 0);
  return { context, width, height, dpr, rect, plot: { left: width < 480 ? 52 : 68, top: 18, right: 19, bottom: 37 } };
}

function niceStep(span, divisions) {
  const rough = span / divisions;
  const power = 10 ** Math.floor(Math.log10(rough || 1));
  const normalized = rough / power;
  return (normalized <= 1 ? 1 : normalized <= 2 ? 2 : normalized <= 5 ? 5 : 10) * power;
}

function timeLabel(seconds) {
  if (Math.abs(seconds) < 60) return `${numberText(seconds, 3)} s`;
  if (Math.abs(seconds) < 3600) return `${numberText(seconds / 60, 3)} min`;
  return `${numberText(seconds / 3600, 3)} h`;
}

function downsampleForRender(rows, xMin, xMax, pixelWidth) {
  const limit = Math.max(1, Math.floor(pixelWidth * 4));
  if (rows.length <= limit || xMax <= xMin) return rows;
  const buckets = new Map();
  for (const point of rows) {
    if (point.x < xMin || point.x > xMax) continue;
    const pixel = Math.max(0, Math.min(Math.floor(pixelWidth) - 1, Math.floor((point.x - xMin) / (xMax - xMin) * pixelWidth)));
    let bucket = buckets.get(pixel);
    if (!bucket) {
      bucket = { first: point, last: point, min: point, max: point, breaks: [] };
      buckets.set(pixel, bucket);
    }
    bucket.last = point;
    if (point.y < bucket.min.y) bucket.min = point;
    if (point.y > bucket.max.y) bucket.max = point;
    if (point.breakBefore) bucket.breaks.push(point);
  }
  const result = [];
  for (const bucket of buckets.values()) {
    const unique = new Set([bucket.first, bucket.min, bucket.max, bucket.last, ...bucket.breaks]);
    for (const point of Array.from(unique).sort((a, b) => a.x - b.x)) result.push(point);
  }
  return result;
}

function scheduleChartRedraw() {
  if (app.chartFramePending) return;
  app.chartFramePending = true;
  requestAnimationFrame(() => {
    app.chartFramePending = false;
    drawChart();
  });
}

function schedulePointerRedraw() {
  if (app.pointerFramePending) return;
  app.pointerFramePending = true;
  requestAnimationFrame(() => {
    app.pointerFramePending = false;
    drawCursor();
  });
}

function clampRangeToData(range, dataMin, dataMax) {
  const span = Math.max(Number.EPSILON, range[1] - range[0]);
  const dataSpan = Math.max(Number.EPSILON, dataMax - dataMin);
  const visible = Math.min(dataSpan, span * .25);
  const minimumStart = dataMin - span + visible;
  const maximumStart = dataMax - visible;
  const start = Math.max(minimumStart, Math.min(maximumStart, range[0]));
  return [start, start + span];
}

function updateViewStatus() {
  const label = app.imported ? "历史文件视图" : el("autoFollow").checked ? "自动跟随最新数据" : "手动视图 · 自动跟随已关闭";
  if (el("viewModeStatus")) el("viewModeStatus").textContent = label;
}

function updateEmptyPlotState(hasPoints = validPoints().length > 0) {
  const state = app.state || {};
  const status = String(state.status || "");
  const acquiring = /(采集中|测量中)/.test(status) || status.includes("暂停");
  const connected = Boolean(state.connected);
  const localControl = connected && !state.simulation && state.control_mode === "local";
  el("plotEmpty").classList.toggle("hidden", hasPoints);
  el("measurementArea").classList.toggle("chart-empty", !hasPoints);
  if (hasPoints) return;
  if (!connected) {
    el("emptyTitle").textContent = "图表还没有数据";
    el("emptyDetail").textContent = "连接 8845A，或先连接模拟器查看采集流程。";
    el("emptyConnectMock").classList.remove("hidden");
    el("emptyStart").classList.add("hidden");
  } else if (localControl) {
    el("emptyTitle").textContent = "仪表处于 Local 模式";
    el("emptyDetail").textContent = "切换到 Computer 后，软件才能开始采集。";
    el("emptyConnectMock").classList.add("hidden");
    el("emptyStart").classList.add("hidden");
  } else if (acquiring) {
    el("emptyTitle").textContent = "等待首个有效读数";
    el("emptyDetail").textContent = state.last_error
      ? `最近异常：${String(state.last_error).slice(0, 100)}`
      : "采集已开始，仪表返回读数后曲线会显示在这里。";
    el("emptyConnectMock").classList.add("hidden");
    el("emptyStart").classList.add("hidden");
  } else {
    el("emptyTitle").textContent = "设备已连接";
    el("emptyDetail").textContent = "开始采集后，读数会沿时间轴逐点显示。";
    el("emptyConnectMock").classList.add("hidden");
    el("emptyStart").classList.remove("hidden");
  }
}

function drawChart() {
  if (!ui.canvas) return;
  const { context: ctx, width, height, plot } = plotContext();
  if (ui.overlay) {
    const overlay = plotContext(ui.overlay);
    overlay.context.clearRect(0, 0, overlay.width, overlay.height);
  }
  ctx.clearRect(0, 0, width, height);
  const rows = validPoints();
  updateEmptyPlotState(rows.length > 0);
  updateViewStatus();
  if (!rows.length) {
    app.chartScale = null;
    app.chartEventHits = [];
    el("plotRange").textContent = "等待数据";
    el("pointCursor").textContent = "暂无可读数点";
    updateChartAccessibilitySummary();
    return;
  }
  const maxX = rows[rows.length - 1].x;
  const minDataX = rows[0].x;
  const values = rows.map((row) => row.y);
  let xMin, xMax;
  if (app.xView) {
    [xMin, xMax] = app.xView;
    if (el("autoFollow").checked) app.xView = null;
  }
  if (!app.xView) {
    if (el("autoFollow").checked) {
      const windowSeconds = Math.max(120, maxX - minDataX);
      xMax = Math.max(windowSeconds / 4, maxX + Math.max(1, windowSeconds * .015));
      xMin = Math.max(minDataX, xMax - windowSeconds);
      if (maxX < 120) { xMin = minDataX; xMax = Math.max(30, maxX * 1.08); }
    } else {
      xMin = minDataX;
      xMax = Math.max(minDataX + 1, maxX);
    }
  }
  if (!Number.isFinite(xMin) || !Number.isFinite(xMax) || xMax <= xMin) { xMin = minDataX; xMax = Math.max(minDataX + 1, maxX + 1); }
  let dataMin = Infinity, dataMax = -Infinity;
  for (const value of values) {
    if (value < dataMin) dataMin = value;
    if (value > dataMax) dataMax = value;
  }
  const dataSpan = dataMax - dataMin;
  const padding = dataSpan === 0 ? Math.max(Math.abs(dataMax) * .025, 1e-9) : dataSpan * .12;
  let yMin = dataMin - padding;
  let yMax = dataMax + padding;
  if (app.yView) [yMin, yMax] = app.yView;
  const left = plot.left, top = plot.top, right = width - plot.right, bottom = height - plot.bottom;
  const plotWidth = Math.max(1, right - left), plotHeight = Math.max(1, bottom - top);
  const xToPixel = (x) => left + (x - xMin) / (xMax - xMin) * plotWidth;
  const yToPixel = (y) => bottom - (y - yMin) / (yMax - yMin) * plotHeight;
  const unit = rows[0].unit || activeMode()?.unit || "";

  ctx.fillStyle = "#faf9f2";
  ctx.fillRect(left, top, plotWidth, plotHeight);
  ctx.font = "11px Segoe UI, Microsoft YaHei UI, sans-serif";
  ctx.textBaseline = "middle";
  ctx.strokeStyle = COLORS.grid;
  ctx.lineWidth = 1;
  const yStep = niceStep(yMax - yMin, 5);
  const axis = axisScaleForValues(values, unit);
  for (let value = Math.ceil(yMin / yStep) * yStep; value <= yMax + yStep * 0.01; value += yStep) {
    const y = yToPixel(value);
    ctx.beginPath(); ctx.moveTo(left, y); ctx.lineTo(right, y); ctx.stroke();
    const label = numberText(value / axis.factor, 6);
    ctx.fillStyle = COLORS.ink;
    ctx.textAlign = "right";
    ctx.fillText(label, left - 9, y);
  }
  const xStep = niceStep(xMax - xMin, Math.max(3, Math.floor(plotWidth / 125)));
  ctx.textAlign = "center";
  for (let value = Math.ceil(xMin / xStep) * xStep; value <= xMax + xStep * .01; value += xStep) {
    const x = xToPixel(value);
    ctx.beginPath(); ctx.moveTo(x, top); ctx.lineTo(x, bottom); ctx.stroke();
    ctx.fillStyle = COLORS.ink;
    ctx.fillText(timeLabel(value), x, bottom + 17);
  }
  ctx.strokeStyle = COLORS.axis;
  ctx.beginPath(); ctx.moveTo(left, top); ctx.lineTo(left, bottom); ctx.lineTo(right, bottom); ctx.stroke();
  ctx.save();
  ctx.translate(14, top + plotHeight / 2);
  ctx.rotate(-Math.PI / 2);
  ctx.fillStyle = "#697a80";
  ctx.textAlign = "center";
  const plottedMode = app.modes.get(rows[0]?.mode || app.selectedMode);
  const quantityLabel = plottedMode?.quantity === "voltage" ? "电压" : plottedMode?.quantity === "current" ? "电流" : plottedMode?.quantity === "resistance" ? "电阻" : "读数";
  ctx.fillText(`${quantityLabel} (${axis.unit})`, 0, 0);
  ctx.restore();
  ctx.fillStyle = "#697a80";
  ctx.textAlign = "center";
  ctx.fillText("实验经过时间", left + plotWidth / 2, height - 7);

  let previousId = null;
  const segmentedRows = rows.map((point, index) => {
    const id = Number(point.id);
    const breakBefore = index > 0 && Number.isFinite(id) && Number.isFinite(previousId) && id > previousId + 1;
    previousId = Number.isFinite(id) ? id : previousId;
    return { ...point, breakBefore };
  });
  const renderRows = downsampleForRender(segmentedRows, xMin, xMax, plotWidth);

  ctx.save();
  ctx.beginPath(); ctx.rect(left, top, plotWidth, plotHeight); ctx.clip();
  const baseline = app.baseline;
  if (Number.isFinite(baseline) && baseline >= yMin && baseline <= yMax) {
    const y = yToPixel(baseline);
    ctx.strokeStyle = "#c7842f";
    ctx.lineWidth = 1.4;
    ctx.setLineDash([6, 4]);
    ctx.beginPath(); ctx.moveTo(left, y); ctx.lineTo(right, y); ctx.stroke();
    ctx.setLineDash([]);
    const baselineLabel = scaledValue(baseline, unit);
    ctx.fillStyle = "#a4651d";
    ctx.textAlign = "right";
    ctx.fillText(`基准 ${baselineLabel.value} ${baselineLabel.unit}`.trim(), right - 5, y - 8);
  }
  ctx.beginPath();
  let started = false;
  for (const point of renderRows) {
    if (point.x < xMin || point.x > xMax) continue;
    const x = xToPixel(point.x), y = yToPixel(point.y);
    if (!started || point.breakBefore) ctx.moveTo(x, y);
    else ctx.lineTo(x, y);
    started = true;
  }
  if (started) {
    ctx.strokeStyle = COLORS.line;
    ctx.lineWidth = 1.8;
    ctx.lineJoin = "round";
    ctx.lineCap = "round";
    ctx.stroke();
  }
  const events = displayedEvents();
  app.chartEventHits = [];
  for (let eventIndex = 0; eventIndex < events.length; eventIndex++) {
    const event = events[eventIndex];
    const xValue = Number(event.elapsed_s);
    if (xValue < xMin || xValue > xMax) continue;
    const x = xToPixel(xValue);
    app.chartEventHits.push({ x, index: eventIndex, event });
    ctx.strokeStyle = COLORS.event;
    ctx.setLineDash([3, 4]);
    ctx.beginPath(); ctx.moveTo(x, top); ctx.lineTo(x, bottom); ctx.stroke();
    ctx.setLineDash([]);
    if (app.selectedEventIndex === eventIndex) {
      const label = String(event.text || "实验事件").slice(0, 80);
      ctx.fillStyle = "#72501b";
      ctx.textAlign = x > left + plotWidth * .7 ? "right" : "left";
      ctx.fillText(label, x > left + plotWidth * .7 ? x - 5 : x + 5, top + 11);
    }
  }
  ctx.restore();
  const source = dataPoints();
  const firstId = Number(rows[0]?.id), lastId = Number(rows.at(-1)?.id);
  const cached = app.pointIndexCache;
  const indexKeyMatches = cached && cached.source === source && cached.length === rows.length
    && cached.firstId === firstId && cached.lastId === lastId
    && cached.xMin === xMin && cached.xMax === xMax && cached.yMin === yMin && cached.yMax === yMax
    && cached.left === left && cached.right === right && cached.top === top && cached.bottom === bottom;
  const pointIndex = indexKeyMatches ? cached.index : buildPixelPointIndex(rows, xToPixel, yToPixel, xMin, xMax);
  if (!indexKeyMatches) app.pointIndexCache = { source, length: rows.length, firstId, lastId, xMin, xMax, yMin, yMax, left, right, top, bottom, index: pointIndex };
  app.chartScale = { xMin, xMax, yMin, yMax, dataMinX: minDataX, dataMaxX: maxX, dataMinY: dataMin, dataMaxY: dataMax, left, top, right, bottom, xToPixel, yToPixel, rows, pointIndex, unit };
  const visibleCount = rows.reduce((count, point) => count + (point.x >= xMin && point.x <= xMax ? 1 : 0), 0);
  const sourceLabel = app.imported ? "历史 CSV" : "实时缓存";
  el("plotRange").textContent = `${sourceLabel} · ${visibleCount.toLocaleString("en-US")} 个有效点 · t=${timeLabel(xMin)} — ${timeLabel(xMax)}`;
  el("bufferNote").textContent = app.imported ? "历史文件只在本浏览器读取；不会上传或改写文件" : app.bufferTruncated ? "图表缓存已滚动；CSV 文件仍保存完整原始记录" : "显示缓存最多保留 5000 点；完整数据持续写入 CSV";
  drawCursor();
  updateChartAccessibilitySummary();
}

function drawCursor() {
  const scale = app.chartScale;
  if (!ui.overlay) return;
  const { context: ctx, width, height } = plotContext(ui.overlay);
  ctx.clearRect(0, 0, width, height);
  if (!scale) return;
  if (app.hoveredEventIndex !== null && app.hoveredEventIndex !== undefined) {
    const marker = app.chartEventHits.find((item) => item.index === app.hoveredEventIndex);
    if (marker) {
      ctx.save();
      ctx.strokeStyle = "#9d681c";
      ctx.lineWidth = 1.5;
      ctx.setLineDash([3, 3]);
      ctx.beginPath(); ctx.moveTo(marker.x, scale.top); ctx.lineTo(marker.x, scale.bottom); ctx.stroke();
      ctx.setLineDash([]);
      const label = String(marker.event.text || "实验事件").slice(0, 80);
      ctx.fillStyle = "#72501b";
      ctx.textAlign = marker.x > scale.left + (scale.right - scale.left) * .7 ? "right" : "left";
      ctx.font = "12px Segoe UI, sans-serif";
      ctx.fillText(label, ctx.textAlign === "right" ? marker.x - 5 : marker.x + 5, scale.top + 11);
      ctx.restore();
    }
  }
  if (!app.cursor || app.dragging) return;
  const { x, y } = app.cursor;
  if (x < scale.left || x > scale.right || y < scale.top || y > scale.bottom) return;
  ctx.save();
  ctx.strokeStyle = "#73848b99";
  ctx.setLineDash([3, 4]);
  ctx.beginPath(); ctx.moveTo(x, scale.top); ctx.lineTo(x, scale.bottom); ctx.moveTo(scale.left, y); ctx.lineTo(scale.right, y); ctx.stroke();
  ctx.setLineDash([]);
  const value = scale.yMax - (y - scale.top) / (scale.bottom - scale.top) * (scale.yMax - scale.yMin);
  ctx.fillStyle = "#31464f";
  ctx.beginPath(); ctx.arc(x, y, 3.5, 0, Math.PI * 2); ctx.fill();
  const label = scaledValue(value, scale.unit || activeMode()?.unit || "");
  ctx.font = "12px Segoe UI, sans-serif";
  const text = `${label.value} ${label.unit}`;
  const boxW = ctx.measureText(text).width + 12;
  const boxX = Math.min(Math.max(scale.left, x - boxW / 2), scale.right - boxW);
  ctx.fillStyle = "#fff";
  ctx.fillRect(boxX, scale.top + 2, boxW, 19);
  ctx.strokeStyle = "#d7e0e2";
  ctx.strokeRect(boxX, scale.top + 2, boxW, 19);
  ctx.fillStyle = COLORS.ink;
  ctx.textAlign = "center";
  ctx.textBaseline = "middle";
  ctx.fillText(text, boxX + boxW / 2, scale.top + 11.5);
  ctx.restore();
}

const POINT_INDEX_LEAF_SIZE = 16;
const POINT_INDEX_Y_WEIGHT = Math.sqrt(.6);

function comparePointIndexEntries(a, b, axis) {
  const primaryA = axis === "x" ? a.x : a.y;
  const primaryB = axis === "x" ? b.x : b.y;
  if (primaryA < primaryB) return -1;
  if (primaryA > primaryB) return 1;
  const secondaryA = axis === "x" ? a.y : a.x;
  const secondaryB = axis === "x" ? b.y : b.x;
  if (secondaryA < secondaryB) return -1;
  if (secondaryA > secondaryB) return 1;
  return a.sourceIndex - b.sourceIndex;
}

function pointIndexMedianOfThree(entries, first, middle, last, axis) {
  if (comparePointIndexEntries(entries[first], entries[middle], axis) < 0) {
    if (comparePointIndexEntries(entries[middle], entries[last], axis) < 0) return middle;
    return comparePointIndexEntries(entries[first], entries[last], axis) < 0 ? last : first;
  }
  if (comparePointIndexEntries(entries[first], entries[last], axis) < 0) return first;
  return comparePointIndexEntries(entries[middle], entries[last], axis) < 0 ? last : middle;
}

function pointIndexSelectMedian(entries, start, end, target, axis) {
  let low = start, high = end - 1;
  while (low < high) {
    const middle = low + ((high - low) >> 1);
    const pivot = entries[pointIndexMedianOfThree(entries, low, middle, high, axis)];
    let left = low, right = high;
    while (left <= right) {
      while (left <= high && comparePointIndexEntries(entries[left], pivot, axis) < 0) left++;
      while (right >= low && comparePointIndexEntries(entries[right], pivot, axis) > 0) right--;
      if (left <= right) {
        [entries[left], entries[right]] = [entries[right], entries[left]];
        left++;
        right--;
      }
    }
    if (target <= right) high = right;
    else if (target >= left) low = left;
    else return;
  }
}

function buildPixelPointIndex(rows, xToPixel, yToPixel, xMin, xMax) {
  const entries = [];
  rows.forEach((point, sourceIndex) => {
    if (point.x < xMin || point.x > xMax) return;
    entries.push({ point, x: xToPixel(point.x), y: yToPixel(point.y), sourceIndex });
  });
  if (!entries.length) return { root: null, size: 0 };

  function buildNode(start, end) {
    let minX = Infinity, maxX = -Infinity, minY = Infinity, maxY = -Infinity, minSourceIndex = Infinity;
    for (let index = start; index < end; index++) {
      const entry = entries[index];
      minX = Math.min(minX, entry.x); maxX = Math.max(maxX, entry.x);
      minY = Math.min(minY, entry.y); maxY = Math.max(maxY, entry.y);
      minSourceIndex = Math.min(minSourceIndex, entry.sourceIndex);
    }
    const node = { minX, maxX, minY, maxY, minSourceIndex };
    if (end - start <= POINT_INDEX_LEAF_SIZE) {
      node.entries = entries.slice(start, end);
      return node;
    }

    const axis = (maxY - minY) * POINT_INDEX_Y_WEIGHT > maxX - minX ? "y" : "x";
    const middle = start + ((end - start) >> 1);
    pointIndexSelectMedian(entries, start, end, middle, axis);
    node.left = buildNode(start, middle);
    node.right = buildNode(middle, end);
    return node;
  }

  return { root: buildNode(0, entries.length), size: entries.length };
}

function pointIndexDistanceToBounds(node, x, y) {
  const dx = x < node.minX ? node.minX - x : x > node.maxX ? x - node.maxX : 0;
  const dy = y < node.minY ? node.minY - y : y > node.maxY ? y - node.maxY : 0;
  return dx * dx + dy * dy * .6;
}

function nearestPoint(canvasX, canvasY) {
  const scale = app.chartScale;
  if (!scale) return null;
  const xTarget = scale.xMin + (canvasX - scale.left) / (scale.right - scale.left) * (scale.xMax - scale.xMin);
  let nearest = null;
  let distance = Infinity;
  let nearestSourceIndex = Infinity;
  const index = scale.pointIndex;
  if (index?.root) {
    function visit(node) {
      const lowerBound = pointIndexDistanceToBounds(node, canvasX, canvasY);
      if (lowerBound > distance || (lowerBound === distance && node.minSourceIndex >= nearestSourceIndex)) return;
      if (node.entries) {
        for (const candidate of node.entries) {
          const dx = candidate.x - canvasX;
          const dy = candidate.y - canvasY;
          const score = dx * dx + dy * dy * .6;
          if (score < distance || (score === distance && candidate.sourceIndex < nearestSourceIndex)) {
            nearest = candidate.point;
            distance = score;
            nearestSourceIndex = candidate.sourceIndex;
          }
        }
        return;
      }
      const leftDistance = pointIndexDistanceToBounds(node.left, canvasX, canvasY);
      const rightDistance = pointIndexDistanceToBounds(node.right, canvasX, canvasY);
      if (leftDistance <= rightDistance) {
        visit(node.left);
        visit(node.right);
      } else {
        visit(node.right);
        visit(node.left);
      }
    }
    visit(index.root);
  } else if (!index) {
    for (const point of scale.rows) {
      if (point.x < scale.xMin || point.x > scale.xMax) continue;
      const dx = scale.xToPixel(point.x) - canvasX;
      const dy = scale.yToPixel(point.y) - canvasY;
      const score = dx * dx + dy * dy * .6;
      if (score < distance) { nearest = point; distance = score; }
    }
  }
  if (!nearest) return { x: canvasX, y: canvasY, time: xTarget, value: null };
  return { x: scale.xToPixel(nearest.x), y: scale.yToPixel(nearest.y), point: nearest };
}

ui.canvas.addEventListener("keydown", (event) => {
  const points = validPoints();
  if (event.altKey && ["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown"].includes(event.key)) {
    const scale = app.chartScale;
    if (!scale) return;
    event.preventDefault?.();
    el("autoFollow").checked = false;
    const xRange = app.xView || [scale.xMin, scale.xMax];
    const yRange = app.yView || [scale.yMin, scale.yMax];
    if (event.key === "ArrowLeft" || event.key === "ArrowRight") {
      const shift = (xRange[1] - xRange[0]) * .1 * (event.key === "ArrowLeft" ? -1 : 1);
      app.xView = clampRangeToData([xRange[0] + shift, xRange[1] + shift], scale.dataMinX, scale.dataMaxX);
    } else {
      const shift = (yRange[1] - yRange[0]) * .1 * (event.key === "ArrowDown" ? -1 : 1);
      app.yView = clampRangeToData([yRange[0] + shift, yRange[1] + shift], scale.dataMinY, scale.dataMaxY);
    }
    drawChart();
    return;
  }
  if ((event.key === "+" || event.key === "=" || event.key === "-") && app.chartScale) {
    event.preventDefault?.();
    const scale = app.chartScale;
    const factor = event.key === "-" ? 1.25 : .8;
    const xRange = app.xView || [scale.xMin, scale.xMax];
    const yRange = app.yView || [scale.yMin, scale.yMax];
    const center = (range) => (range[0] + range[1]) / 2;
    app.xView = [center(xRange) + (xRange[0] - center(xRange)) * factor, center(xRange) + (xRange[1] - center(xRange)) * factor];
    app.yView = [center(yRange) + (yRange[0] - center(yRange)) * factor, center(yRange) + (yRange[1] - center(yRange)) * factor];
    el("autoFollow").checked = false;
    drawChart();
    return;
  }
  if (!points.length) return;
  if (event.key === "ArrowLeft" || event.key === "ArrowRight" || event.key === "Home" || event.key === "End") {
    event.preventDefault?.();
    const current = app.selectedPointIndex === null ? 0 : app.selectedPointIndex;
    const next = event.key === "Home" ? 0 : event.key === "End" ? points.length - 1
      : current + (event.key === "ArrowRight" ? 1 : -1);
    selectPointByIndex(next);
  } else if (event.key === "Enter" && app.selectedPointIndex !== null) {
    event.preventDefault?.();
    setBaselineFromPoint(points[app.selectedPointIndex]);
  }
});

ui.canvas.addEventListener("pointermove", (event) => {
  const rect = ui.canvas.getBoundingClientRect();
  const x = event.clientX - rect.left, y = event.clientY - rect.top;
  if (event.pointerType === "touch" && app.activePointers.has(event.pointerId)) {
    app.activePointers.set(event.pointerId, { x, y });
    if (app.activePointers.size >= 2 && app.chartScale) {
      const [first, second] = Array.from(app.activePointers.values()).slice(0, 2);
      const distance = Math.max(1, Math.hypot(second.x - first.x, second.y - first.y));
      const centerX = (first.x + second.x) / 2, centerY = (first.y + second.y) / 2;
      const scale = app.chartScale;
      const factor = app.pinchState ? app.pinchState.distance / distance : 1;
      const xRange = app.xView || [scale.xMin, scale.xMax];
      const yRange = app.yView || [scale.yMin, scale.yMax];
      const xFraction = Math.max(0, Math.min(1, (centerX - scale.left) / (scale.right - scale.left)));
      const yFraction = Math.max(0, Math.min(1, (scale.bottom - centerY) / (scale.bottom - scale.top)));
      const zoomAround = (range, fraction, minimumSpan, maximumSpan) => {
        const anchor = range[0] + fraction * (range[1] - range[0]);
        const span = Math.max(minimumSpan, Math.min(maximumSpan, (range[1] - range[0]) * factor));
        return [anchor - span * fraction, anchor + span * (1 - fraction)];
      };
      app.xView = zoomAround(xRange, xFraction, .05, 1e8);
      app.yView = zoomAround(yRange, yFraction, Math.max(Math.abs(yRange[0]) * 1e-12, 1e-15), Math.max(yRange[1] - yRange[0], 1e-15) * 1e6);
      app.pinchState = { distance };
      el("autoFollow").checked = false;
      app.cursor = null;
      event.preventDefault?.();
      scheduleChartRedraw();
      return;
    }
  }
  if (app.dragging) {
    const scale = app.chartScale;
    const deltaX = -(x - app.dragging.x) / Math.max(1, scale.right - scale.left) * (app.dragging.xRange[1] - app.dragging.xRange[0]);
    const deltaY = (y - app.dragging.y) / Math.max(1, scale.bottom - scale.top) * (app.dragging.yRange[1] - app.dragging.yRange[0]);
    app.xView = clampRangeToData([app.dragging.xRange[0] + deltaX, app.dragging.xRange[1] + deltaX], scale.dataMinX, scale.dataMaxX);
    app.yView = clampRangeToData([app.dragging.yRange[0] + deltaY, app.dragging.yRange[1] + deltaY], scale.dataMinY, scale.dataMaxY);
    el("autoFollow").checked = false;
    app.cursor = null;
    scheduleChartRedraw();
    return;
  }
  const eventMarker = y >= app.chartScale?.top && y <= app.chartScale?.bottom ? nearestEvent(x) : null;
  if (eventMarker) {
    app.hoveredEventIndex = eventMarker.index;
    app.cursor = null;
    const eventTime = Number(eventMarker.event.elapsed_s);
    el("pointCursor").textContent = `事件 · t=${timeLabel(eventTime)} · ${formatLocalDateTime(eventMarker.event.timestamp_local)} · ${eventMarker.event.text}`;
    schedulePointerRedraw();
    return;
  }
  app.hoveredEventIndex = null;
  app.cursor = nearestPoint(x, y);
  if (app.cursor?.point) {
    const point = app.cursor.point;
    const value = scaledValue(point.y, point.unit || "");
    el("pointCursor").textContent = `样本 #${point.id} · ${formatLocalDateTime(point.timestamp)} · t=${timeLabel(point.x)} · ${value.value} ${value.unit} · 原始 ${point.raw || numberText(point.y, 14)}`;
  } else el("pointCursor").textContent = "悬停在曲线上读取读数";
  schedulePointerRedraw();
});
ui.canvas.addEventListener("pointerdown", (event) => {
  if (!app.chartScale) return;
  const rect = ui.canvas.getBoundingClientRect();
  const x = event.clientX - rect.left, y = event.clientY - rect.top;
  if (x < app.chartScale.left || x > app.chartScale.right || y < app.chartScale.top || y > app.chartScale.bottom) return;
  const marker = nearestEvent(x);
  if (marker) {
    focusEvent(marker.index);
    event.preventDefault();
    return;
  }
  if (event.pointerType === "touch") {
    app.activePointers.set(event.pointerId, { x, y });
    if (app.activePointers.size >= 2) {
      const [first, second] = Array.from(app.activePointers.values()).slice(0, 2);
      app.pinchState = { distance: Math.max(1, Math.hypot(second.x - first.x, second.y - first.y)) };
      app.dragging = null;
      ui.canvas.setPointerCapture(event.pointerId);
      return;
    }
  }
  ui.canvas.setPointerCapture(event.pointerId);
  app.dragging = {
    x, y,
    xRange: (app.xView || [app.chartScale.xMin, app.chartScale.xMax]).slice(),
    yRange: (app.yView || [app.chartScale.yMin, app.chartScale.yMax]).slice(),
  };
});
function finishPointer(event) {
  if (event?.pointerType === "touch" && app.activePointers.has(event.pointerId)) {
    app.activePointers.delete(event.pointerId);
    app.pinchState = null;
    if (app.activePointers.size === 1 && app.chartScale) {
      const [remaining] = app.activePointers.values();
      app.dragging = {
        x: remaining.x, y: remaining.y,
        xRange: (app.xView || [app.chartScale.xMin, app.chartScale.xMax]).slice(),
        yRange: (app.yView || [app.chartScale.yMin, app.chartScale.yMax]).slice(),
      };
    } else app.dragging = null;
  } else app.dragging = null;
  scheduleChartRedraw();
}
ui.canvas.addEventListener("pointerup", finishPointer);
ui.canvas.addEventListener("pointercancel", finishPointer);
ui.canvas.addEventListener("pointerleave", () => { if (!app.dragging) { app.cursor = null; app.hoveredEventIndex = null; schedulePointerRedraw(); } });
ui.canvas.addEventListener("wheel", (event) => {
  if (!app.chartScale) return;
  if (!Number.isFinite(event.deltaY) || event.deltaY === 0) return;
  event.preventDefault();
  const scale = app.chartScale;
  const rect = ui.canvas.getBoundingClientRect();
  const x = event.clientX - rect.left;
  const y = event.clientY - rect.top;
  const xFraction = Math.max(0, Math.min(1, (x - scale.left) / (scale.right - scale.left)));
  const yFraction = Math.max(0, Math.min(1, (scale.bottom - y) / (scale.bottom - scale.top)));
  const xAnchor = scale.xMin + xFraction * (scale.xMax - scale.xMin);
  const yAnchor = event.altKey ? 0 : scale.yMin + yFraction * (scale.yMax - scale.yMin);
  const factor = Math.exp(event.deltaY / 120 * 0.18);
  const zoomX = !event.altKey && (event.ctrlKey || event.shiftKey);
  const zoomY = event.altKey || event.ctrlKey || !event.shiftKey;
  const zoomRange = (min, max, anchor, minimumSpan, maximumSpan) => {
    const span = Math.max(minimumSpan, Math.min(maximumSpan, (max - min) * factor));
    const relative = (anchor - min) / (max - min || 1);
    return [anchor - span * relative, anchor + span * (1 - relative)];
  };
  if (zoomX) {
    app.xView = zoomRange(scale.xMin, scale.xMax, xAnchor, .05, 1e8);
  }
  if (zoomY) {
    const span = scale.yMax - scale.yMin;
    const minSpan = Math.max(Math.abs(yAnchor) * 1e-12, span * 1e-9, 1e-15);
    app.yView = zoomRange(scale.yMin, scale.yMax, yAnchor, minSpan, Math.max(span, 1e-15) * 1e6);
  }
  if (zoomY && !zoomX && el("autoFollow").checked) app.xView = [scale.xMin, scale.xMax];
  if (zoomX || zoomY) el("autoFollow").checked = false;
  drawChart();
}, { passive: false });
el("autoFollow").addEventListener("change", () => { if (el("autoFollow").checked) app.xView = null; drawChart(); });
el("autoScale").addEventListener("click", () => {
  app.xView = null;
  app.yView = null;
  el("autoFollow").checked = false;
  drawChart();
});
el("resetRealtimeView").addEventListener("click", () => {
  if (app.imported) {
    toast("当前显示历史文件；先点“返回实时”再恢复实时窗口", true);
    return;
  }
  app.xView = null;
  app.yView = null;
  el("autoFollow").checked = true;
  drawChart();
});
window.addEventListener("resize", scheduleChartRedraw);

async function loadHistory(file) {
  if (!file) return false;
  if (app.cancelEventImport) app.cancelEventImport();
  if (app.cancelHistoryImport) app.cancelHistoryImport();
  if (file.size > 50 * 1024 * 1024) { toast("历史 CSV 超过 50 MB，请先按实验区间拆分文件", true); el("historyFile").value = ""; return false; }
  const worker = new Worker("/history_worker.js");
  app.historyImportWorker = worker;
  toast("正在后台解析历史 CSV…");
  try {
    const result = await new Promise((resolve, reject) => {
      app.cancelHistoryImport = () => reject(Object.assign(new Error("已由更新的历史 CSV 导入替代"), { name: "AbortError" }));
      worker.onmessage = ({ data }) => data?.ok ? resolve(data) : reject(new Error(data?.error || "历史 CSV 解析失败"));
      worker.onerror = () => reject(new Error("历史 CSV 后台解析失败；请检查文件编码和格式后重试"));
      worker.postMessage({ file, modes: [...app.modes.values()].map(({ value, unit }) => ({ value, unit })) });
    });
    const { rows, modeValue, unit, experimentId } = result;
    const mode = app.modes.get(modeValue);
    app.imported = rows;
    app.importedExperimentId = experimentId || null;
    app.importedFileName = file.name || "历史 CSV";
    app.importedEvents = null;
    app.selectedEventIndex = null;
    app.xView = null;
    app.yView = null;
    el("autoFollow").checked = false;
    el("restoreLive").classList.remove("hidden");
    el("chartTitle").textContent = `${file.name} · 历史曲线`;
    el("legendName").textContent = "CSV 历史原始读数";
    el("readingMode").textContent = mode?.label || `历史数据 ${modeValue}`;
    renderEvents([]);
    updateAccessiblePointTable();
    toast(`已在浏览器加载 ${rows.length.toLocaleString("en-US")} 行；原文件未修改`);
    updateStats(); drawChart();
    return true;
  } catch (error) { if (error.name !== "AbortError") toast(`CSV 加载失败：${error.message}`, true); return false; }
  finally {
    worker.terminate();
    if (app.historyImportWorker === worker) {
      app.historyImportWorker = null;
      app.cancelHistoryImport = null;
      el("historyFile").value = "";
    }
  }
}

function historyDirectoryOf(file) {
  const relativePath = String(file?.webkitRelativePath || "").replace(/\\/g, "/");
  const separator = relativePath.lastIndexOf("/");
  return separator > 0 ? relativePath.slice(0, separator) : "";
}

async function loadHistoryDirectory(files) {
  const entries = Array.from(files || []);
  const measurements = entries.filter((file) =>
    String(file.name || "").toLowerCase() === "measurements.csv" && historyDirectoryOf(file));
  if (measurements.length !== 1) {
    toast(measurements.length
      ? "所选文件夹内有多个 measurements.csv；请只选择一个实验文件夹。"
      : "未找到带目录路径的 measurements.csv；请用“加载实验文件夹”选择实验目录。", true);
    return false;
  }
  const measurement = measurements[0];
  const directory = historyDirectoryOf(measurement);
  const pairedEvents = entries.filter((file) =>
    String(file.name || "").toLowerCase() === "events.csv" && historyDirectoryOf(file) === directory);
  if (pairedEvents.length > 1) {
    toast("所选文件夹内有多个同目录 events.csv；请只选择一个实验文件夹。", true);
    return false;
  }
  const loaded = await loadHistory(measurement);
  if (!loaded) return false;
  if (pairedEvents.length) await loadHistoryEvents(pairedEvents[0]);
  else toast("测量 CSV 已加载；所选目录没有同目录 events.csv。");
  return true;
}

async function loadHistoryEvents(file) {
  if (!file) return;
  if (app.cancelEventImport) app.cancelEventImport();
  if (!app.imported) { toast("先加载测量 CSV，再选择配套 events.csv", true); el("eventsHistoryFile").value = ""; return; }
  if (file.size > 10 * 1024 * 1024) { toast("事件 CSV 超过 10 MB，请先按实验区间拆分文件", true); el("eventsHistoryFile").value = ""; return; }
  const worker = new Worker("/history_worker.js");
  app.historyEventsWorker = worker;
  toast("正在后台解析配套事件 CSV…");
  try {
    const result = await new Promise((resolve, reject) => {
      app.cancelEventImport = () => reject(Object.assign(new Error("已由更新的事件 CSV 导入替代"), { name: "AbortError" }));
      worker.onmessage = ({ data }) => data?.ok ? resolve(data) : reject(new Error(data?.error || "事件 CSV 解析失败"));
      worker.onerror = () => reject(new Error("事件 CSV 后台解析失败；请检查文件编码和格式后重试"));
      worker.postMessage({ kind: "events", file, experimentId: app.importedExperimentId });
    });
    const { events } = result;
    app.importedEvents = events;
    app.selectedEventIndex = null;
    renderEvents(events);
    drawChart();
    toast(`已加载 ${events.length.toLocaleString("en-US")} 条配套事件；实验标识一致`);
  } catch (error) {
    if (error.name !== "AbortError") toast(`事件 CSV 加载失败：${error.message}`, true);
  } finally {
    worker.terminate();
    if (app.historyEventsWorker === worker) {
      app.historyEventsWorker = null;
      app.cancelEventImport = null;
      el("eventsHistoryFile").value = "";
    }
  }
}

function setBaselineFromCurrent() {
  const point = validPoints().at(-1);
  if (!point) { toast("当前还没有有效测量值", true); return; }
  setBaselineFromPoint(point);
}

function setBaselineFromPoint(point) {
  if (!point || !Number.isFinite(Number(point.y))) return;
  configureBaselineUnits(point.unit || activeMode()?.unit || "");
  setActiveBaseline(Number(point.y));
  setBaselineInputFromSI(app.baseline);
  updateBaseline(app.baseline, point.unit || activeMode()?.unit || "");
  drawChart();
}

function selectPointByIndex(index) {
  const points = validPoints();
  if (!points.length) return;
  const selected = Math.max(0, Math.min(points.length - 1, Math.trunc(index)));
  app.selectedPointIndex = selected;
  const point = points[selected];
  const scale = app.chartScale;
  app.cursor = scale ? { x: scale.xToPixel(point.x), y: scale.yToPixel(point.y), point } : null;
  const value = scaledValue(point.y, point.unit || activeMode()?.unit || "");
  el("pointCursor").textContent = `已选样本 #${point.id} · ${formatLocalDateTime(point.timestamp)} · t=${timeLabel(point.x)} · ${value.value} ${value.unit} · 原始 ${point.raw || numberText(point.y, 14)} · Enter 锁定基准`;
  drawChart();
}

async function savePlot() {
  try {
    const exportCanvas = createPlotExportCanvas();
    const blob = await new Promise((resolve) => exportCanvas.toBlob(resolve, "image/png"));
    if (!blob) throw new Error("浏览器未能生成 PNG");
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url; link.download = `8845A_plot_${localExperimentName().replace(/^实验_/, "")}.png`;
    link.click(); URL.revokeObjectURL(url);
  } catch (error) { toast(`截图失败：${error.message}`, true); }
}

function plotExportContext() {
  const points = validPoints();
  const modeKey = app.imported ? (points[0]?.mode || app.selectedMode) : app.selectedMode;
  const spec = app.modes.get(modeKey) || activeMode();
  const unit = points[0]?.unit || spec?.unit || "";
  const chartRange = app.chartScale;
  const firstX = chartRange && Number.isFinite(chartRange.xMin) ? chartRange.xMin : points[0]?.x;
  const lastX = chartRange && Number.isFinite(chartRange.xMax) ? chartRange.xMax : points.at(-1)?.x;
  const experiment = app.imported
    ? (app.importedFileName || "历史 CSV")
    : (el("experimentName").value.trim() || "未命名实验");
  const baselineValue = app.baseline;
  const baselineScale = Number.isFinite(Number(baselineValue)) ? scaledValue(Number(baselineValue), unit) : null;
  const baseline = baselineScale ? `${baselineScale.value} ${baselineScale.unit}`.trim() : "未设置";
  const range = Number.isFinite(firstX) && Number.isFinite(lastX)
    ? `t=${timeLabel(firstX)} — ${timeLabel(lastX)}`
    : "无有效数据范围";
  return {
    experiment,
    mode: spec?.label || modeKey || "未选择模式",
    unit: unit === "Ohm" ? "Ω" : unit || "未指定",
    exportedAt: formatLocalDateTime(new Date().toISOString()),
    range,
    baseline,
  };
}

function createPlotExportCanvas() {
  const source = ui.canvas;
  const pixelRatio = Math.max(1, Number(window.devicePixelRatio) || 1);
  const headerLogicalHeight = 84;
  const headerHeight = Math.round(headerLogicalHeight * pixelRatio);
  const canvas = document.createElement("canvas");
  canvas.width = source.width;
  canvas.height = source.height + headerHeight;
  const ctx = canvas.getContext("2d");
  const context = plotExportContext();
  const width = canvas.width / pixelRatio;
  ctx.fillStyle = "#ffffff";
  ctx.fillRect(0, 0, canvas.width, canvas.height);
  ctx.save();
  ctx.scale(pixelRatio, pixelRatio);
  ctx.fillStyle = "#25343a";
  ctx.font = "600 15px sans-serif";
  ctx.textAlign = "left";
  ctx.fillText(`实验：${context.experiment} · 模式：${context.mode} · 单位：${context.unit}`, 14, 23, Math.max(0, width - 28));
  ctx.font = "13px sans-serif";
  ctx.fillStyle = "#53656b";
  ctx.fillText(`导出时间：${context.exportedAt} · 时间范围：${context.range}`, 14, 47, Math.max(0, width - 28));
  ctx.fillText(`基准线：${context.baseline}`, 14, 69, Math.max(0, width - 28));
  ctx.drawImage(source, 0, headerLogicalHeight);
  ctx.restore();
  return canvas;
}

async function downloadLatestCsv() {
  try {
    const response = await fetch("/api/latest.csv", { cache: "no-store" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const exportState = response.headers.get("X-8845A-Export-State") || "saved";
    const snapshotTime = response.headers.get("X-8845A-Snapshot-Time") || "时间未提供";
    const disposition = response.headers.get("Content-Disposition") || "";
    const encodedName = disposition.match(/filename\*=UTF-8''([^;]+)/i)?.[1];
    const filename = encodedName ? decodeURIComponent(encodedName) : "8845A_measurements.csv";
    const blob = await response.blob();
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = filename;
    link.click();
    URL.revokeObjectURL(url);
    toast(exportState === "in-progress-snapshot"
      ? `已下载进行中快照；快照时间 ${snapshotTime}`
      : `已下载已保存 CSV；服务器时间 ${snapshotTime}`);
  } catch (error) {
    toast(`CSV 下载失败：${error.message}`, true);
  }
}

async function downloadLatestEventsSafeCsv() {
  try {
    const response = await fetch("/api/latest-events-safe.csv", { cache: "no-store" });
    if (!response.ok) {
      const error = await response.json().catch(() => ({}));
      throw new Error(error.error || `HTTP ${response.status}`);
    }
    const disposition = response.headers.get("Content-Disposition") || "";
    const encodedName = disposition.match(/filename\*=UTF-8''([^;]+)/i)?.[1];
    const link = document.createElement("a");
    const url = URL.createObjectURL(await response.blob());
    link.href = url;
    link.download = encodedName ? decodeURIComponent(encodedName) : "events_excel_safe.csv";
    link.click();
    URL.revokeObjectURL(url);
    toast("已导出事件安全副本；原始 events.csv 未修改");
  } catch (error) {
    toast(`事件 CSV 导出失败：${error.message}`, true);
  }
}

async function copyExperimentPath() {
  const directory = app.state?.experiment_dir || app.state?.last_experiment_dir || "";
  if (!directory) { toast("实验目录将在开始采集后创建", true); return; }
  try {
    if (!navigator.clipboard?.writeText) throw new Error("浏览器剪贴板不可用");
    await navigator.clipboard.writeText(directory);
    toast("实验目录路径已复制");
  } catch (error) { toast(`复制路径失败：${error.message}`, true); }
}

async function refreshExperimentFiles() {
  el("filesStatus").textContent = "正在读取实验文件…";
  el("filesList").replaceChildren();
  const result = await command("list_experiment_files");
  if (!result.success) {
    el("filesStatus").textContent = result.message || "无法读取实验文件";
    return;
  }
  el("filesDirectory").textContent = result.directory || "实验目录尚未创建";
  el("openFilesDirectory").disabled = !result.directory;
  for (const file of result.files || []) {
    const row = document.createElement("li");
    const name = document.createElement("span");
    const size = document.createElement("span");
    name.textContent = file.name;
    size.textContent = `${Number(file.size_bytes || 0).toLocaleString("zh-CN")} B`;
    row.append(name, size);
    el("filesList").append(row);
  }
  el("filesStatus").textContent = result.files?.length
    ? `列出 ${result.files.length} 个文件；原始测量数据保持原样。`
    : "当前还没有实验文件；开始或完成一次实验后可在这里查看。";
}

function bindControls() {
  el("duplicateCreateCopy").addEventListener("click", () => finishDuplicateNameAction("create-copy"));
  el("duplicateRename").addEventListener("click", () => finishDuplicateNameAction("rename"));
  el("duplicateCancel").addEventListener("click", () => finishDuplicateNameAction("cancel"));
  el("duplicateRunDialog").addEventListener("cancel", (event) => {
    event.preventDefault?.();
    finishDuplicateNameAction("cancel");
  });
  el("openSettings").addEventListener("click", () => el("settingsDialog").showModal());
  el("openFiles").addEventListener("click", () => { el("filesDialog").showModal(); refreshExperimentFiles(); });
  el("refreshFiles").addEventListener("click", refreshExperimentFiles);
  el("closeFiles").addEventListener("click", () => el("filesDialog").close());
  el("openFilesDirectory").addEventListener("click", () => command("open_experiment_directory"));
  el("criticalError").addEventListener("click", () => {
    const dialog = el("settingsDialog");
    if (!dialog.open) dialog.showModal();
    const section = el("sectionDiagnostics");
    section.open = true;
    const filter = app.latestInvalidPoint && !app.state?.last_error ? "all" : "error";
    el("logFilter").value = filter;
    app.logFilter = filter;
    renderLogRows();
    section.scrollIntoView?.({ behavior: "smooth", block: "start" });
  });
  el("closeSettings").addEventListener("click", () => el("settingsDialog").close());
  el("emptyOpenSettings").addEventListener("click", () => el("settingsDialog").showModal());
  el("emptyConnectMock").addEventListener("click", connectMock);
  el("emptyStart").addEventListener("click", startContinuous);
  el("scanPorts").addEventListener("click", scanPorts);
  el("portList").addEventListener("change", () => {
    if (el("portList").value) el("port").value = el("portList").value;
    updatePortSelectionInfo();
  });
  el("port").addEventListener("input", () => updatePortSelectionInfo());
  el("connectReal").addEventListener("click", connectReal);
  el("reconnectReal").addEventListener("click", connectReal);
  el("connectMock").addEventListener("click", connectMock);
  el("disconnect").addEventListener("click", async () => {
    const result = await command("disconnect");
    if (result.success) {
      app.mockConnectPending = false;
      clearSafetyConfirmation();
      serialFormDisabled(false);
    }
  });
  el("applyControlMode").addEventListener("click", () => command("control_mode", { mode: el("controlMode").value }));
  el("outguardSoftware").addEventListener("change", saveManualSoftware);
  el("inguardSoftware").addEventListener("change", saveManualSoftware);
  el("clearManualSoftware").addEventListener("click", clearManualSoftware);
  el("resetSerialDefaults").addEventListener("click", () => {
    const defaults = { baudrate: "9600", bytesize: "8", parity: "N", stopbits: "1", eol: "CRLF", flow: "none", serialTimeout: "2.0" };
    for (const [id, value] of Object.entries(defaults)) el(id).value = value;
    saveSerialPreferences();
    toast("推荐串口设置已恢复：9600、8-N-1、CRLF、无流控制");
  });
  el("mode").addEventListener("change", () => {
    const spec = app.modes.get(el("mode").value);
    if (!spec) return;
    app.baselineByMode.set(app.selectedMode, app.baseline);
    populateModeOptions(spec);
    app.baseline = app.baselineByMode.get(spec.value) ?? null;
    configureBaselineUnits(spec.unit);
    setBaselineInputFromSI(app.baseline);
    updateBaseline(validPoints().at(-1)?.y, spec.unit);
    app.yView = null;
    drawChart();
    renderSettingsComparison(app.state);
  });
  el("applySettings").addEventListener("click", applySettings);
  el("applySpeed").addEventListener("click", applySpeed);
  el("zero").addEventListener("click", async () => {
    const active = app.state?.active_settings || {};
    const mode = app.modes.get(active.mode || app.selectedMode);
    const range = active.autorange ? "自动量程（当前实际量程由仪器动态决定）" : active.range_si !== null && active.range_si !== undefined && Number.isFinite(Number(active.range_si)) ? rangeLabel(active.range_si, mode?.unit) : "量程未回读";
    const timing = active.nplc !== null && active.nplc !== undefined ? `${active.nplc} NPLC` : active.ac_bandwidth_hz ? `${active.ac_bandwidth_hz} Hz 交流带宽` : "无适用积分/带宽设置";
    const filter = active.analog_filter ? "模拟滤波：开" : "模拟滤波：关";
    const autozero = active.autozero ? "自动零点：开" : "自动零点：关";
    if (!window.confirm(`ZERO:AUTO ONCE 将启动一次仪器内部零点测量，不会将被测读数置零，也不是校准调整。\n\n当前回读：${mode?.label || "模式未回读"} · ${range} · ${timing} · ${filter} · ${autozero}\n\n请核对输入和触发条件；软件发出命令后检查错误队列并回读配置。本软件没有零点值查询项，配置回读不能证明已取得 ZERO 测量结果。继续？`)) return;
    await command("zero", { zero_confirmed: true });
  });
  el("start").addEventListener("click", startContinuous);
  el("single").addEventListener("click", startSingle);
  el("pause").addEventListener("click", () => command("pause"));
  el("resume").addEventListener("click", () => command("resume"));
  el("stop").addEventListener("click", () => command("stop"));
  el("experimentName").addEventListener("input", updateExperimentNameValidation);
  el("outputRoot").addEventListener("input", clearPreflightSummary);
  el("safetyConfirm").addEventListener("change", clearPreflightSummary);
  el("interval").addEventListener("change", () => command("interval", { interval_s: Number(el("interval").value) }));
  el("markEvent").addEventListener("click", async () => {
    const input = el("eventText");
    if (!input.value.trim()) { toast("先输入事件内容", true); return; }
    const result = await command("event", { text: input.value.trim() });
    if (result.success) input.value = "";
  });
  el("eventText").addEventListener("keydown", (event) => { if (event.key === "Enter") el("markEvent").click(); });
  el("downloadCsv").addEventListener("click", downloadLatestCsv);
  el("downloadEventsSafe").addEventListener("click", downloadLatestEventsSafeCsv);
  el("savePlot").addEventListener("click", savePlot);
  el("historyFile").addEventListener("change", (event) => loadHistory(event.target.files[0]));
  el("historyDirectory").addEventListener("change", async (event) => {
    try { await loadHistoryDirectory(event.target.files); }
    finally { event.target.value = ""; }
  });
  el("eventsHistoryFile").addEventListener("change", (event) => loadHistoryEvents(event.target.files[0]));
  ui.events.addEventListener("click", (event) => {
    const button = event.target?.closest?.(".event-jump");
    if (button) focusEvent(Number(button.dataset.eventIndex));
  });
  el("copyPointTable").addEventListener("click", copyAccessiblePointTable);
  el("resetPreferences").addEventListener("click", clearPreferences);
  el("logFilter").addEventListener("change", () => { app.logFilter = el("logFilter").value; renderLogRows(); });
  el("exportLogs").addEventListener("click", exportFilteredLogs);
  el("openExperimentDir").addEventListener("click", () => command("open_experiment_directory"));
  el("copyExperimentPath").addEventListener("click", copyExperimentPath);
  el("restoreLive").addEventListener("click", () => {
    app.imported = null; app.xView = null; app.yView = null;
    app.importedEvents = null; app.importedExperimentId = null; app.importedFileName = ""; app.selectedEventIndex = null;
    el("autoFollow").checked = true;
    el("restoreLive").classList.add("hidden");
    const mode = activeMode();
    el("chartTitle").textContent = mode ? `${mode.label} · 随时间变化` : "测量曲线";
    el("legendName").textContent = mode ? `${mode.label} 原始读数` : "原始读数";
    renderEvents(app.state?.events || []);
    updateStats(); drawChart();
  });
  el("baselineCurrent").addEventListener("click", setBaselineFromCurrent);
  el("baselineMean").addEventListener("click", () => {
    const points = validPoints();
    if (!points.length) { toast("当前还没有有效测量值", true); return; }
    setActiveBaseline(points.reduce((sum, point) => sum + point.y, 0) / points.length);
    setBaselineInputFromSI(app.baseline);
    updateBaseline(app.baseline, points[0].unit || activeMode()?.unit || "");
  });
  el("baselineValue").addEventListener("input", () => {
    setActiveBaseline(el("baselineValue").value === "" ? null : baselineInputToSI(el("baselineValue").value));
    updateBaseline(validPoints().at(-1)?.y, activeMode()?.unit || "");
  });
  el("baselineScale").addEventListener("change", () => {
    setBaselineInputFromSI(app.baseline);
    updateBaseline(validPoints().at(-1)?.y, activeMode()?.unit || "");
  });
  el("baselineClear").addEventListener("click", () => { setActiveBaseline(null); el("baselineValue").value = ""; updateBaseline(null, activeMode()?.unit || ""); });
  el("shutdownService").addEventListener("click", async () => {
    if (!window.confirm("退出会先停止采集、保存并关闭当前实验，然后关闭 localhost 服务。关闭网页标签页不会停止采集或服务；它们会继续运行。确定退出服务？")) return;
    await command("shutdown");
  });
  ui.confirm.addEventListener("change", () => { if (ui.confirm.checked) toast("接线确认仅记录你的人工核对；软件无法检测插孔、保险丝或外部回路"); });
}

function restorePreferences() {
  try {
    const saved = JSON.parse(localStorage.getItem("8845a-web-preferences") || "{}");
    if (saved.port) el("port").value = saved.port;
    if (saved.baudrate) el("baudrate").value = saved.baudrate;
    if (saved.bytesize) el("bytesize").value = saved.bytesize;
    if (saved.parity) el("parity").value = saved.parity;
    if (saved.stopbits) el("stopbits").value = saved.stopbits;
    if (saved.eol) el("eol").value = saved.eol;
    if (saved.flow) el("flow").value = saved.flow;
    if (saved.serialTimeout) el("serialTimeout").value = saved.serialTimeout;
    if (saved.outputRoot) el("outputRoot").value = saved.outputRoot;
  } catch (_) { /* Use safe defaults when browser storage is unavailable. */ }
  el("experimentName").value = localExperimentName();
  el("outputRoot").value ||= app.meta?.default_root || "";
  for (const id of ["port", "baudrate", "bytesize", "parity", "stopbits", "eol", "flow", "serialTimeout", "outputRoot"]) {
    el(id).addEventListener("change", () => {
      const preferences = Object.fromEntries(["port", "baudrate", "bytesize", "parity", "stopbits", "eol", "flow", "serialTimeout", "outputRoot"].map((key) => [key, el(key).value]));
      try { localStorage.setItem("8845a-web-preferences", JSON.stringify(preferences)); } catch (_) { /* Optional convenience only. */ }
    });
  }
}

function localExperimentName(date = new Date()) {
  const pad = (value) => String(value).padStart(2, "0");
  return `实验_${date.getFullYear()}${pad(date.getMonth() + 1)}${pad(date.getDate())}${pad(date.getHours())}${pad(date.getMinutes())}`;
}

function clearPreferences() {
  localStorage.removeItem("8845a-web-preferences");
  const defaults = {
    port: "COM3", baudrate: "9600", bytesize: "8", parity: "N", stopbits: "1",
    eol: "CRLF", flow: "none", serialTimeout: "2.0", outputRoot: app.meta?.default_root || "",
  };
  for (const [id, value] of Object.entries(defaults)) el(id).value = value;
  el("experimentName").value = localExperimentName();
  toast("已恢复默认设置；串口与保存目录偏好保存在当前浏览器本机");
}

async function initialize() {
  bindControls();
  try {
    const response = await fetch("/api/meta", { cache: "no-store" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    app.meta = await response.json();
    setServiceStatus(true);
    app.modes = new Map(app.meta.modes.map((mode) => [mode.value, mode]));
    el("versionLabel").textContent = `v${app.meta.version}`;
    ui.mode.innerHTML = app.meta.modes.map((mode) => `<option value="${mode.value}">${htmlText(mode.label)}</option>`).join("");
    populateModeOptions(app.meta.modes[0]);
    restorePreferences();
    await scanPorts();
    await pollState();
    app.pollTimer = setInterval(pollState, 850);
  } catch (error) {
    setServiceStatus(false, error.message);
    el("connectionText").textContent = `界面初始化失败：${error.message}`;
    toast(`无法读取本地界面配置：${error.message}`, true);
  }
}

initialize();
