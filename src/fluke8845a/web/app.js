"use strict";

const $ = (selector) => document.querySelector(selector);
const el = (id) => document.getElementById(id);
const ui = {
  mode: el("mode"), options: el("modeOptions"), safety: el("safetyText"), confirm: el("safetyConfirm"),
  canvas: el("plot"), empty: el("plotEmpty"), toast: el("toast"), logs: el("logList"), events: el("eventList"),
};
const app = {
  meta: null, state: null, points: [], imported: null, generation: null, lastSample: 0, lastLog: 0,
  modes: new Map(), selectedMode: "DCV", baseline: null, toastTimer: 0, pollTimer: 0,
  xView: null, yView: null, dragging: null, cursor: null, bufferTruncated: false,
  awaitInitialSettings: false, awaitSettingsReadback: false, awaitSpeedReadback: false,
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

function activeMode() { return app.modes.get(app.selectedMode) || null; }
function settingsPayload() {
  const mode = app.selectedMode;
  const settings = { mode, autorange: el("autorange").checked, autozero: el("autozero").checked };
  if (!settings.autorange) settings.range_si = Number(el("range").value);
  const spec = app.modes.get(mode);
  if (spec.nplc.length) settings.nplc = Number(el("nplc").value);
  if (spec.bandwidths.length) settings.ac_bandwidth_hz = Number(el("bandwidth").value);
  if (spec.analog_filter) settings.analog_filter = el("analogFilter").checked;
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
  app.selectedMode = spec.value;
  const rangeItems = spec.ranges.map((range) => `<option value="${range}">${htmlText(rangeLabel(range, spec.unit))}</option>`).join("");
  let html = `<div class="option-row"><label class="option-check"><input id="autorange" type="checkbox" checked> 自动量程</label><label>固定量程<select id="range" disabled>${rangeItems}</select></label></div>`;
  if (spec.nplc.length) {
    html += `<label>积分时间 / NPLC<select id="nplc">${spec.nplc.map((value) => `<option value="${value}" ${value === 1 ? "selected" : ""}>${value} NPLC</option>`).join("")}</select></label>`;
  }
  if (spec.bandwidths.length) {
    html += `<label>交流响应带宽<select id="bandwidth">${spec.bandwidths.map((value) => `<option value="${value}" ${value === 20 ? "selected" : ""}>${value} Hz</option>`).join("")}</select></label>`;
  }
  if (spec.analog_filter) html += `<label class="option-check"><input id="analogFilter" type="checkbox"> 启用直流模拟滤波</label>`;
  html += `<label class="option-check" title="仪表自动测量内部零点并补偿偏移，可提升稳定性但会增加测量时间；不会把被测读数强制改成0。"><input id="autozero" type="checkbox" checked> 自动零点校正</label>`;
  ui.options.innerHTML = html;
  el("autorange").addEventListener("change", () => { el("range").disabled = el("autorange").checked; });
  ui.safety.textContent = spec.safety;
  ui.confirm.checked = false;
  el("readingMode").textContent = spec.label;
  el("chartTitle").textContent = `${spec.label} · 随时间变化`;
  el("legendName").textContent = `${spec.label} 原始读数`;
  drawChart();
}

function rangeLabel(value, unit) {
  const n = Number(value);
  if (unit === "V") return n < 1 ? `${n * 1000} mV` : `${n} V`;
  if (unit === "A") return n < 1 ? `${n * 1000} mA` : `${n} A`;
  if (unit === "Ohm") return n >= 1e6 ? `${n / 1e6} MΩ` : n >= 1e3 ? `${n / 1e3} kΩ` : `${n} Ω`;
  return String(value);
}

function toast(message, isError = false) {
  clearTimeout(app.toastTimer);
  ui.toast.textContent = message;
  ui.toast.classList.toggle("error", isError);
  ui.toast.classList.add("visible");
  app.toastTimer = setTimeout(() => ui.toast.classList.remove("visible"), 3300);
}

async function command(action, payload = {}) {
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
  }
}

function serialFormDisabled(disabled) {
  for (const id of ["port", "portList", "baudrate", "bytesize", "parity", "stopbits", "eol", "flow", "serialTimeout", "scanPorts", "connectReal", "connectMock"]) el(id).disabled = disabled;
}

function safetyPayload() { return { safety_confirmed: ui.confirm.checked }; }

async function connectReal() {
  const result = await command("connect_real", {
    port: el("port").value.trim(), serial: serialPayload(), settings: settingsPayload(),
  });
  if (result.success) { serialFormDisabled(true); app.awaitInitialSettings = true; }
}

async function connectMock() {
  const result = await command("connect_mock", {
    settings: settingsPayload(), scenario: el("mockScenario").value,
  });
  if (result.success) { serialFormDisabled(true); app.awaitInitialSettings = true; }
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

async function startContinuous() { await command("start", startPayload()); }
async function startSingle() { await command("single", startPayload()); }

async function scanPorts() {
  const button = el("scanPorts");
  button.disabled = true;
  try {
    const response = await fetch("/api/ports", { cache: "no-store" });
    const result = await response.json();
    const list = el("portList");
    list.innerHTML = `<option value="">选择已发现端口</option>` + result.ports.map((port) => `<option value="${htmlText(port.device)}">${htmlText(port.device)} · ${htmlText(port.description || "串口")}</option>`).join("");
    if (!result.ports.length) toast(result.error || "没有发现串口；仍可在左侧手动输入 COM 端口");
    else toast(`发现 ${result.ports.length} 个串口`);
  } catch (error) { toast(`扫描失败：${error.message}`, true); }
  finally { button.disabled = false; }
}

function appendLogRows(rows) {
  if (!rows?.length) return;
  const list = ui.logs;
  if (list.querySelector(".empty-log")) list.innerHTML = "";
  const fragment = document.createDocumentFragment();
  for (const row of rows) {
    const item = document.createElement("div");
    item.className = `log-entry${String(row.text).startsWith("! ") ? " error" : ""}`;
    const time = document.createElement("time");
    time.textContent = new Date(row.time).toLocaleTimeString("zh-CN", { hour12: false });
    const text = document.createElement("span");
    text.textContent = row.text;
    item.append(time, text);
    fragment.append(item);
  }
  list.append(fragment);
  while (list.children.length > 300) list.firstElementChild.remove();
  list.scrollTop = list.scrollHeight;
}

function appendSampleRows(rows) {
  if (!rows?.length) return;
  for (const row of rows) {
    const id = Number(row.sample_index);
    if (Number.isFinite(id) && id <= app.lastSample) continue;
    const value = Number(row.value_si);
    app.points.push({
      id: Number.isFinite(id) ? id : app.points.length + 1,
      x: Number(row.elapsed_s), y: Number.isFinite(value) ? value : null,
      status: row.status, raw: row.raw_reading, mode: row.mode, unit: row.unit_si,
      timestamp: row.timestamp_local, error: row.error,
    });
    app.lastSample = Math.max(app.lastSample, Number.isFinite(id) ? id : app.lastSample);
  }
  if (app.points.length > 5000) app.points.splice(0, app.points.length - 5000);
}

async function pollState() {
  const parameters = new URLSearchParams({ since_sample: String(app.lastSample), since_log: String(app.lastLog) });
  if (app.generation !== null) parameters.set("generation", String(app.generation));
  try {
    const response = await fetch(`/api/state?${parameters}`, { cache: "no-store" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const state = await response.json();
    if (app.generation !== null && state.generation !== app.generation) {
      app.points = [];
      app.lastSample = 0;
      app.imported = null;
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
    updateStateDisplay(state);
    if (state.events) renderEvents(state.events);
    if (state.logs?.some((line) => String(line.text).startsWith("! "))) {
      // The actual error remains in the status panel; avoid repeated popups during polling.
    }
    drawChart();
    updateStats();
  } catch (error) {
    el("runStatus").textContent = "本地服务连接中断";
    el("runDot").className = "run-dot";
    el("connectionText").textContent = `无法读取服务状态：${error.message}`;
  }
}

function updateStateDisplay(state) {
  syncSettingsReadback(state);
  const badge = el("connectionBadge");
  const badgeText = badge.querySelector("span");
  badge.classList.toggle("connected", state.connected && !state.simulation);
  badge.classList.toggle("mock", state.connected && state.simulation);
  badgeText.textContent = !state.connected ? "未连接" : state.simulation ? "MOCK 模拟" : `已连接 · 8845A / ${state.control_mode === "local" ? "LOCAL" : "COMPUTER"}`;
  el("connectionText").textContent = state.connection_text || "串口尚未打开";
  el("connectionText").parentElement.classList.toggle("is-connected", !!state.connected);
  el("disconnect").classList.toggle("hidden", !state.connected);
  el("connectReal").classList.toggle("hidden", !!state.connected);
  el("connectMock").classList.toggle("hidden", !!state.connected);
  el("mockScenarioRow").classList.toggle("hidden", !state.simulation);
  serialFormDisabled(!!state.connected);
  el("controlMode").value = state.control_mode || "disconnected";
  const activeMode = state.active_settings?.mode;
  const modeApplied = !state.connected || !activeMode || activeMode === app.selectedMode;
  const badgeName = state.configuration?.function || activeMode || "未应用";
  el("modeFunction").textContent = state.connected
    ? (state.simulation ? (modeApplied ? "模拟配置" : `当前 ${activeMode}`) : modeApplied ? `回读 ${badgeName}` : `待应用 · 当前 ${activeMode}`)
    : "待连接";
  el("modeFunction").classList.toggle("is-active", !!state.connected);
  el("runStatus").textContent = state.status || "等待连接";
  el("runDot").className = `run-dot${String(state.status).includes("暂停") ? " paused" : /(采集中|测量中)/.test(state.status || "") ? " running" : ""}`;
  const latest = state.latest;
  const mode = latest?.mode ? app.modes.get(latest.mode) : activeMode();
  if (latest) {
    const scaled = scaledValue(latest.value_si, latest.unit_si || mode?.unit);
    el("readingValue").textContent = scaled.value;
    el("readingUnit").textContent = scaled.unit;
    el("readingMode").textContent = mode?.label || latest.mode || "测量读数";
    el("readingRaw").textContent = `原始响应 ${latest.raw_reading || "—"}  ·  ${latest.timestamp_local || ""}`;
    el("sampleCount").textContent = latest.sample_index ?? app.points.length;
    const elapsedSeconds = Number(latest.elapsed_s);
    el("elapsed").textContent = Number.isFinite(elapsedSeconds) ? durationText(elapsedSeconds) : "—";
  } else {
    el("readingValue").textContent = "—";
    el("readingUnit").textContent = "";
    el("readingMode").textContent = mode?.label || "等待仪器读数";
    el("readingRaw").textContent = state.last_error ? `最近异常：${state.last_error}` : "原始响应将保存在实验 CSV";
    el("sampleCount").textContent = "0";
    el("elapsed").textContent = "—";
  }
  el("actualRate").innerHTML = `${numberText(state.actual_hz || 0, 4)} <small>Hz</small>`;
  const directory = state.experiment_dir || state.last_experiment_dir;
  el("experimentPath").textContent = directory || "实验目录尚未创建";
  el("experimentPathDetail").textContent = directory || "实验目录将在开始测量后创建";
  el("downloadCsv").disabled = !state.last_experiment_dir;
  const running = /采集中|测量中/.test(state.status || "");
  const paused = String(state.status || "").includes("暂停");
  const acquiring = running || paused;
  const computerControl = !state.connected || state.simulation || state.control_mode === "computer";
  el("controlMode").disabled = !state.connected || state.simulation || acquiring;
  el("applyControlMode").disabled = !state.connected || state.simulation || acquiring;
  el("start").disabled = !state.connected || !computerControl || acquiring;
  el("single").disabled = !state.connected || !computerControl || acquiring;
  el("pause").disabled = !running;
  el("pause").classList.toggle("hidden", paused);
  el("resume").classList.toggle("hidden", !paused);
  el("stop").disabled = !running && !paused;
  el("applySettings").disabled = !state.connected || !computerControl || acquiring;
  el("disconnect").disabled = acquiring;
  const settingsDisabled = acquiring || !computerControl;
  el("mode").disabled = settingsDisabled;
  el("autorange").disabled = settingsDisabled;
  el("range").disabled = settingsDisabled || el("autorange").checked;
  if (el("analogFilter")) el("analogFilter").disabled = settingsDisabled;
  el("autozero").disabled = settingsDisabled;
  if (el("nplc")) el("nplc").disabled = !acquiring || !computerControl;
  if (el("bandwidth")) el("bandwidth").disabled = !acquiring || !computerControl;
  const active = activeMode();
  const canTune = active && (active.nplc.length || active.bandwidths.length);
  el("applySpeed").disabled = !state.connected || !computerControl || !canTune || !acquiring;
  el("zero").disabled = !state.connected || !computerControl || acquiring;
  if (state.last_error && state.last_error !== app.lastShownError) {
    app.lastShownError = state.last_error;
    // Errors stay visible inline and in the log, without interrupting long sessions.
  }
  if (state.simulation) document.title = "Fluke 8845A · MOCK 模拟 · 实验测量台";
  else document.title = "Fluke 8845A · 实验测量台";
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
    el("autozero").checked = !!active.autozero;
    app.awaitInitialSettings = false;
    app.awaitSettingsReadback = false;
  } else if (app.awaitSpeedReadback && app.selectedMode === active.mode) {
    if (spec.nplc.length && active.nplc !== null && active.nplc !== undefined) el("nplc").value = String(active.nplc);
    if (spec.bandwidths.length && active.ac_bandwidth_hz !== null && active.ac_bandwidth_hz !== undefined) el("bandwidth").value = String(active.ac_bandwidth_hz);
  }
  app.awaitSpeedReadback = false;
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
  if (!points.length) {
    for (const id of ["statMean", "statStd", "statMin", "statMax"]) el(id).textContent = "—";
    updateBaseline(null, spec?.unit || "");
    return;
  }
  let sum = 0, min = Infinity, max = -Infinity;
  for (const point of points) { sum += point.y; min = Math.min(min, point.y); max = Math.max(max, point.y); }
  const mean = sum / points.length;
  let squares = 0;
  for (const point of points) squares += (point.y - mean) ** 2;
  const std = points.length > 1 ? Math.sqrt(squares / (points.length - 1)) : 0;
  const unit = points[0].unit || spec?.unit || "";
  el("statMean").textContent = statWithUnit(mean, unit);
  el("statStd").textContent = statWithUnit(std, unit);
  el("statMin").textContent = statWithUnit(min, unit);
  el("statMax").textContent = statWithUnit(max, unit);
  if (!el("baselineValue").value && app.baseline === null && app.state?.connected && points.length === 1) updateBaseline(null, unit);
  updateBaseline(mean, unit);
}

function statWithUnit(value, unit) {
  const scaled = scaledValue(value, unit);
  return `${scaled.value} ${scaled.unit}`.trim();
}

function updateBaseline(mean, unit) {
  if (el("baselineValue").value !== "") app.baseline = Number(el("baselineValue").value);
  if (!Number.isFinite(app.baseline)) {
    el("baselineDelta").textContent = "尚未设置基准值";
    el("baselineDelta").classList.remove("is-set");
    return;
  }
  const latest = validPoints().at(-1);
  if (!latest) return;
  const delta = latest.y - app.baseline;
  const relative = app.baseline === 0 ? null : delta / Math.abs(app.baseline) * 100;
  el("baselineDelta").textContent = `Δ ${statWithUnit(delta, unit)}${relative === null ? " · 基准为 0" : ` · ${numberText(relative, 5)}%`}`;
  el("baselineDelta").classList.add("is-set");
}

function renderEvents(events) {
  el("eventCount").textContent = `${events.length} 条`;
  if (!events.length) {
    ui.events.innerHTML = `<li class="empty-event">实验事件会按实际记录时间显示在这里。</li>`;
    return;
  }
  ui.events.innerHTML = events.slice().reverse().map((event) => {
    const elapsed = Number(event.elapsed_s);
    const value = event.value_si !== null && event.value_si !== undefined ? scaledValue(event.value_si, event.unit_si) : null;
    const sample = event.sample_index ? `#${event.sample_index}` : "";
    return `<li><time>t=${Number.isFinite(elapsed) ? durationText(elapsed) : "—"}</time><span>${htmlText(event.text)}</span><span class="event-value">${sample}${value ? ` · ${value.value} ${value.unit}` : ""}</span></li>`;
  }).join("");
}

function plotContext() {
  const rect = ui.canvas.getBoundingClientRect();
  const dpr = Math.max(1, window.devicePixelRatio || 1);
  const width = Math.max(1, rect.width);
  const height = Math.max(1, rect.height);
  if (ui.canvas.width !== Math.round(width * dpr) || ui.canvas.height !== Math.round(height * dpr)) {
    ui.canvas.width = Math.round(width * dpr);
    ui.canvas.height = Math.round(height * dpr);
  }
  const context = ui.canvas.getContext("2d");
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

function drawChart() {
  if (!ui.canvas) return;
  const { context: ctx, width, height, plot } = plotContext();
  ctx.clearRect(0, 0, width, height);
  const rows = validPoints();
  ui.empty.classList.toggle("hidden", rows.length > 0);
  if (!rows.length) {
    el("plotRange").textContent = "等待数据";
    el("pointCursor").textContent = "滚轮缩放纵轴 · Shift 时间轴 · Ctrl 双轴 · Alt 锚定零点 · 拖动平移";
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
  const dataMin = Math.min(...values);
  const dataMax = Math.max(...values);
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
  ctx.font = "10px Segoe UI, Microsoft YaHei UI, sans-serif";
  ctx.textBaseline = "middle";
  ctx.strokeStyle = COLORS.grid;
  ctx.lineWidth = 1;
  const yStep = niceStep(yMax - yMin, 5);
  for (let value = Math.ceil(yMin / yStep) * yStep; value <= yMax + yStep * 0.01; value += yStep) {
    const y = yToPixel(value);
    ctx.beginPath(); ctx.moveTo(left, y); ctx.lineTo(right, y); ctx.stroke();
    const label = scaledValue(value, unit);
    ctx.fillStyle = COLORS.ink;
    ctx.textAlign = "right";
    ctx.fillText(`${label.value} ${label.unit}`.trim(), left - 9, y);
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
  ctx.fillText(`读数 (${unit === "Ohm" ? "Ω" : unit})`, 0, 0);
  ctx.restore();
  ctx.fillStyle = "#697a80";
  ctx.textAlign = "center";
  ctx.fillText("实验经过时间", left + plotWidth / 2, height - 7);

  ctx.save();
  ctx.beginPath(); ctx.rect(left, top, plotWidth, plotHeight); ctx.clip();
  ctx.beginPath();
  let started = false;
  for (const point of rows) {
    if (point.x < xMin || point.x > xMax) continue;
    const x = xToPixel(point.x), y = yToPixel(point.y);
    if (!started) { ctx.moveTo(x, y); started = true; } else ctx.lineTo(x, y);
  }
  if (started) {
    ctx.strokeStyle = COLORS.line;
    ctx.lineWidth = 1.8;
    ctx.lineJoin = "round";
    ctx.lineCap = "round";
    ctx.stroke();
  }
  const events = app.state?.events || [];
  for (const event of events) {
    const xValue = Number(event.elapsed_s);
    if (xValue < xMin || xValue > xMax) continue;
    const x = xToPixel(xValue);
    ctx.strokeStyle = COLORS.event;
    ctx.setLineDash([3, 4]);
    ctx.beginPath(); ctx.moveTo(x, top); ctx.lineTo(x, bottom); ctx.stroke();
    ctx.setLineDash([]);
  }
  ctx.restore();
  app.chartScale = { xMin, xMax, yMin, yMax, left, top, right, bottom, xToPixel, yToPixel, rows };
  const visibleCount = rows.reduce((count, point) => count + (point.x >= xMin && point.x <= xMax ? 1 : 0), 0);
  const source = app.imported ? "历史 CSV" : "实时缓存";
  el("plotRange").textContent = `${source} · ${visibleCount.toLocaleString("en-US")} 个有效点 · t=${timeLabel(xMin)} — ${timeLabel(xMax)}`;
  el("bufferNote").textContent = app.imported ? "历史文件只在本浏览器读取；不会上传或改写文件" : app.bufferTruncated ? "图表缓存已滚动；CSV 文件仍保存完整原始记录" : "显示缓存最多保留 5000 点；完整数据持续写入 CSV";
  drawCursor();
}

function drawCursor() {
  const scale = app.chartScale;
  if (!scale || !app.cursor || app.dragging) return;
  const ctx = ui.canvas.getContext("2d");
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
  const label = scaledValue(value, validPoints()[0]?.unit || activeMode()?.unit || "");
  ctx.font = "10px Segoe UI, sans-serif";
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

function nearestPoint(canvasX, canvasY) {
  const scale = app.chartScale;
  if (!scale) return null;
  const xTarget = scale.xMin + (canvasX - scale.left) / (scale.right - scale.left) * (scale.xMax - scale.xMin);
  let nearest = null;
  let distance = Infinity;
  for (const point of scale.rows) {
    if (point.x < scale.xMin || point.x > scale.xMax) continue;
    const dx = scale.xToPixel(point.x) - canvasX;
    const dy = scale.yToPixel(point.y) - canvasY;
    const score = dx * dx + dy * dy * .6;
    if (score < distance) { nearest = point; distance = score; }
  }
  if (!nearest) return { x: canvasX, y: canvasY, time: xTarget, value: null };
  return { x: scale.xToPixel(nearest.x), y: scale.yToPixel(nearest.y), point: nearest };
}

ui.canvas.addEventListener("pointermove", (event) => {
  const rect = ui.canvas.getBoundingClientRect();
  const x = event.clientX - rect.left, y = event.clientY - rect.top;
  if (app.dragging) {
    const scale = app.chartScale;
    const deltaX = -(x - app.dragging.x) / Math.max(1, scale.right - scale.left) * (app.dragging.xRange[1] - app.dragging.xRange[0]);
    const deltaY = (y - app.dragging.y) / Math.max(1, scale.bottom - scale.top) * (app.dragging.yRange[1] - app.dragging.yRange[0]);
    app.xView = [app.dragging.xRange[0] + deltaX, app.dragging.xRange[1] + deltaX];
    app.yView = [app.dragging.yRange[0] + deltaY, app.dragging.yRange[1] + deltaY];
    el("autoFollow").checked = false;
    app.cursor = null;
    drawChart();
    return;
  }
  app.cursor = nearestPoint(x, y);
  if (app.cursor?.point) {
    const point = app.cursor.point;
    const value = scaledValue(point.y, point.unit || "");
    el("pointCursor").textContent = `t=${timeLabel(point.x)} · ${value.value} ${value.unit} · 原始 ${point.raw || numberText(point.y, 14)} · 点 #${point.id}`;
  } else el("pointCursor").textContent = "滚轮缩放纵轴 · Shift 时间轴 · Ctrl 双轴 · Alt 锚定零点 · 拖动平移";
  drawChart();
});
ui.canvas.addEventListener("pointerdown", (event) => {
  if (!app.chartScale) return;
  const rect = ui.canvas.getBoundingClientRect();
  const x = event.clientX - rect.left, y = event.clientY - rect.top;
  if (x < app.chartScale.left || x > app.chartScale.right || y < app.chartScale.top || y > app.chartScale.bottom) return;
  ui.canvas.setPointerCapture(event.pointerId);
  app.dragging = {
    x, y,
    xRange: (app.xView || [app.chartScale.xMin, app.chartScale.xMax]).slice(),
    yRange: (app.yView || [app.chartScale.yMin, app.chartScale.yMax]).slice(),
  };
});
ui.canvas.addEventListener("pointerup", () => { app.dragging = null; drawChart(); });
ui.canvas.addEventListener("pointercancel", () => { app.dragging = null; drawChart(); });
ui.canvas.addEventListener("pointerleave", () => { if (!app.dragging) { app.cursor = null; drawChart(); } });
ui.canvas.addEventListener("wheel", (event) => {
  if (!app.chartScale) return;
  event.preventDefault();
  const scale = app.chartScale;
  const rect = ui.canvas.getBoundingClientRect();
  const x = event.clientX - rect.left;
  const y = event.clientY - rect.top;
  const xFraction = Math.max(0, Math.min(1, (x - scale.left) / (scale.right - scale.left)));
  const yFraction = Math.max(0, Math.min(1, (scale.bottom - y) / (scale.bottom - scale.top)));
  const xAnchor = scale.xMin + xFraction * (scale.xMax - scale.xMin);
  const yAnchor = event.altKey ? 0 : scale.yMin + yFraction * (scale.yMax - scale.yMin);
  const factor = event.deltaY < 0 ? .8 : 1.25;
  const zoomX = !event.altKey && (event.ctrlKey || event.shiftKey);
  const zoomY = event.altKey || event.ctrlKey || !event.shiftKey;
  const zoomRange = (min, max, anchor, minimumSpan, maximumSpan) => {
    const span = Math.max(minimumSpan, Math.min(maximumSpan, (max - min) * factor));
    const relative = (anchor - min) / (max - min || 1);
    return [anchor - span * relative, anchor + span * (1 - relative)];
  };
  if (zoomX) {
    app.xView = zoomRange(scale.xMin, scale.xMax, xAnchor, .05, 1e8);
    el("autoFollow").checked = false;
  }
  if (zoomY) {
    const span = scale.yMax - scale.yMin;
    const minSpan = Math.max(Math.abs(yAnchor) * 1e-12, span * 1e-9, 1e-15);
    app.yView = zoomRange(scale.yMin, scale.yMax, yAnchor, minSpan, Math.max(span, 1e-15) * 1e6);
  }
  drawChart();
}, { passive: false });
el("autoFollow").addEventListener("change", () => { if (el("autoFollow").checked) app.xView = null; drawChart(); });
el("autoScale").addEventListener("click", () => {
  app.xView = null;
  app.yView = null;
  el("autoFollow").checked = false;
  drawChart();
});
window.addEventListener("resize", drawChart);

function parseCsv(text) {
  const rows = [];
  let row = [], cell = "", quoted = false;
  for (let index = 0; index < text.length; index++) {
    const char = text[index];
    if (quoted) {
      if (char === '"' && text[index + 1] === '"') { cell += '"'; index++; }
      else if (char === '"') quoted = false;
      else cell += char;
    } else if (char === '"') quoted = true;
    else if (char === ",") { row.push(cell); cell = ""; }
    else if (char === "\n" || char === "\r") {
      if (char === "\r" && text[index + 1] === "\n") index++;
      row.push(cell); cell = "";
      if (row.some((value) => value !== "")) rows.push(row);
      row = [];
    } else cell += char;
  }
  if (cell || row.length) { row.push(cell); if (row.some((value) => value !== "")) rows.push(row); }
  return rows;
}

async function loadHistory(file) {
  if (!file) return;
  if (file.size > 50 * 1024 * 1024) { toast("历史 CSV 超过 50 MB，请先按实验区间拆分文件", true); el("historyFile").value = ""; return; }
  try {
    const text = (await file.text()).replace(/^\uFEFF/, "");
    const table = parseCsv(text);
    if (table.length < 2) throw new Error("CSV 没有测量数据行");
    const headers = table[0].map((value) => value.trim());
    const column = (name) => headers.indexOf(name);
    const elapsedColumn = column("elapsed_s");
    const valueColumn = column("value_si");
    const statusColumn = column("status");
    const modeColumn = column("mode");
    const unitColumn = column("unit_si");
    if (elapsedColumn < 0 || valueColumn < 0 || statusColumn < 0) throw new Error("CSV 缺少 elapsed_s、value_si 或 status 列");
    const modeValue = modeColumn >= 0 ? table.slice(1).map((row) => row[modeColumn]).find(Boolean) : "";
    const mode = app.modes.get(modeValue);
    const unit = unitColumn >= 0 ? table.slice(1).map((row) => row[unitColumn]).find(Boolean) || mode?.unit : mode?.unit;
    const rows = [];
    for (const row of table.slice(1)) {
      const x = Number(row[elapsedColumn]);
      const y = row[valueColumn] === "" ? NaN : Number(row[valueColumn]);
      const rawColumn = column("raw_reading");
      const idColumn = column("sample_index");
      rows.push({ x, y, status: row[statusColumn], mode: modeValue, unit, raw: rawColumn >= 0 ? row[rawColumn] : "", id: idColumn >= 0 ? (Number(row[idColumn]) || rows.length + 1) : rows.length + 1 });
    }
    app.imported = rows;
    app.xView = null;
    app.yView = null;
    el("autoFollow").checked = false;
    el("restoreLive").classList.remove("hidden");
    el("chartTitle").textContent = `${file.name} · 历史曲线`;
    el("legendName").textContent = "CSV 历史原始读数";
    el("readingMode").textContent = mode?.label || `历史数据 ${modeValue}`;
    toast(`已在浏览器加载 ${rows.length.toLocaleString("en-US")} 行；原文件未修改`);
    updateStats(); drawChart();
  } catch (error) { toast(`CSV 加载失败：${error.message}`, true); }
  finally { el("historyFile").value = ""; }
}

function setBaselineFromCurrent() {
  const point = validPoints().at(-1);
  if (!point) { toast("当前还没有有效测量值", true); return; }
  app.baseline = point.y;
  el("baselineValue").value = String(point.y);
  updateBaseline(point.y, point.unit || activeMode()?.unit || "");
}

async function savePlot() {
  try {
    const blob = await new Promise((resolve) => ui.canvas.toBlob(resolve, "image/png"));
    if (!blob) throw new Error("浏览器未能生成 PNG");
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url; link.download = `8845A_plot_${new Date().toISOString().replaceAll(":", "-")}.png`;
    link.click(); URL.revokeObjectURL(url);
  } catch (error) { toast(`截图失败：${error.message}`, true); }
}

function bindControls() {
  el("openSettings").addEventListener("click", () => el("settingsDialog").showModal());
  el("closeSettings").addEventListener("click", () => el("settingsDialog").close());
  el("scanPorts").addEventListener("click", scanPorts);
  el("portList").addEventListener("change", () => { if (el("portList").value) el("port").value = el("portList").value; });
  el("connectReal").addEventListener("click", connectReal);
  el("connectMock").addEventListener("click", connectMock);
  el("disconnect").addEventListener("click", async () => { const result = await command("disconnect"); if (result.success) serialFormDisabled(false); });
  el("applyControlMode").addEventListener("click", () => command("control_mode", { mode: el("controlMode").value }));
  el("mode").addEventListener("change", () => { const spec = app.modes.get(el("mode").value); if (spec) { populateModeOptions(spec); app.yView = null; drawChart(); } });
  el("applySettings").addEventListener("click", applySettings);
  el("applySpeed").addEventListener("click", applySpeed);
  el("zero").addEventListener("click", async () => {
    if (!window.confirm("ZERO 会对仪器执行一次零点校正。请确认输入条件、量程、积分时间与触发条件适合校正。继续？")) return;
    await command("zero", { zero_confirmed: true });
  });
  el("start").addEventListener("click", startContinuous);
  el("single").addEventListener("click", startSingle);
  el("pause").addEventListener("click", () => command("pause"));
  el("resume").addEventListener("click", () => command("resume"));
  el("stop").addEventListener("click", () => command("stop"));
  el("interval").addEventListener("change", () => command("interval", { interval_s: Number(el("interval").value) }));
  el("markEvent").addEventListener("click", async () => {
    const input = el("eventText");
    if (!input.value.trim()) { toast("先输入事件内容", true); return; }
    const result = await command("event", { text: input.value.trim() });
    if (result.success) input.value = "";
  });
  el("eventText").addEventListener("keydown", (event) => { if (event.key === "Enter") el("markEvent").click(); });
  el("downloadCsv").addEventListener("click", () => { window.location.assign("/api/latest.csv"); });
  el("savePlot").addEventListener("click", savePlot);
  el("historyFile").addEventListener("change", (event) => loadHistory(event.target.files[0]));
  el("restoreLive").addEventListener("click", () => {
    app.imported = null; app.xView = null; app.yView = null;
    el("autoFollow").checked = true;
    el("restoreLive").classList.add("hidden");
    const mode = activeMode();
    el("chartTitle").textContent = mode ? `${mode.label} · 随时间变化` : "测量曲线";
    el("legendName").textContent = mode ? `${mode.label} 原始读数` : "原始读数";
    updateStats(); drawChart();
  });
  el("baselineCurrent").addEventListener("click", setBaselineFromCurrent);
  el("baselineMean").addEventListener("click", () => {
    const points = validPoints();
    if (!points.length) { toast("当前还没有有效测量值", true); return; }
    app.baseline = points.reduce((sum, point) => sum + point.y, 0) / points.length;
    el("baselineValue").value = String(app.baseline);
    updateBaseline(app.baseline, points[0].unit || activeMode()?.unit || "");
  });
  el("baselineValue").addEventListener("input", () => {
    app.baseline = el("baselineValue").value === "" ? null : Number(el("baselineValue").value);
    updateBaseline(validPoints().at(-1)?.y, activeMode()?.unit || "");
  });
  el("baselineClear").addEventListener("click", () => { app.baseline = null; el("baselineValue").value = ""; updateBaseline(null, activeMode()?.unit || ""); });
  el("shutdownService").addEventListener("click", async () => {
    if (!window.confirm("退出会先停止采集、保存并关闭当前实验，然后关闭 localhost 服务。继续？")) return;
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
  el("experimentName").value = `实验_${new Date().toISOString().slice(0, 16).replace(/[T:-]/g, "")}`;
  el("outputRoot").value ||= app.meta?.default_root || "";
  for (const id of ["port", "baudrate", "bytesize", "parity", "stopbits", "eol", "flow", "serialTimeout", "outputRoot"]) {
    el(id).addEventListener("change", () => {
      const preferences = Object.fromEntries(["port", "baudrate", "bytesize", "parity", "stopbits", "eol", "flow", "serialTimeout", "outputRoot"].map((key) => [key, el(key).value]));
      try { localStorage.setItem("8845a-web-preferences", JSON.stringify(preferences)); } catch (_) { /* Optional convenience only. */ }
    });
  }
}

async function initialize() {
  bindControls();
  try {
    const response = await fetch("/api/meta", { cache: "no-store" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    app.meta = await response.json();
    app.modes = new Map(app.meta.modes.map((mode) => [mode.value, mode]));
    el("versionLabel").textContent = `v${app.meta.version}`;
    ui.mode.innerHTML = app.meta.modes.map((mode) => `<option value="${mode.value}">${htmlText(mode.label)}</option>`).join("");
    populateModeOptions(app.meta.modes[0]);
    restorePreferences();
    await scanPorts();
    await pollState();
    app.pollTimer = setInterval(pollState, 850);
  } catch (error) {
    el("connectionText").textContent = `界面初始化失败：${error.message}`;
    toast(`无法读取本地界面配置：${error.message}`, true);
  }
}

initialize();
