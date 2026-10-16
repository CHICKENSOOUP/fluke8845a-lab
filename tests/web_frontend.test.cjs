"use strict";

const assert = require("node:assert/strict");
const { spawnSync } = require("node:child_process");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const ROOT = path.resolve(__dirname, "..");

function expectedLocalDateTime(value) {
  const date = new Date(value);
  const pad = (part) => String(part).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;
}

test("desktop and web explain that elapsed time includes pauses and freezes while paused", () => {
  const html = fs.readFileSync(path.join(ROOT, "src/fluke8845a/web/index.html"), "utf8");
  const gui = fs.readFileSync(path.join(ROOT, "src/fluke8845a/gui.py"), "utf8");
  assert.match(html, /id="elapsed" title="[^"]*包含暂停时间；暂停时显示冻结，继续采集后计入暂停时长/);
  assert.match(gui, /elapsed_label\.setToolTip\("[^"]*包含暂停时间；暂停时显示冻结，继续采集后计入暂停时长/);
});

test("narrow-screen touch targets and small viewport rules stay accessible", () => {
  const css = fs.readFileSync(path.join(ROOT, "src/fluke8845a/web/app.css"), "utf8");
  const narrow = css.match(/@media \(max-width: 580px\)\s*\{([\s\S]*?)\n\}/);
  assert.ok(narrow, "mobile layout exists");
  assert.match(narrow[1], /\.option-check, \.confirm-row\s*\{[^}]*min-height:\s*44px/);
  assert.match(narrow[1], /\.chart-actions \.toggle-label\s*\{[^}]*min-width:\s*44px[^}]*min-height:\s*44px/);
  assert.match(css, /@media \(max-width: 350px\)/);
  assert.match(css, /body\s*\{[^}]*min-width:\s*280px/);
});

test("Web summary counts valid measurements separately from attempts", () => {
  const html = fs.readFileSync(path.join(ROOT, "src/fluke8845a/web/index.html"), "utf8");
  assert.match(html, /<span>有效样本<\/span><strong id="sampleCount">/);
  const { api, elements } = createFrontend();
  api.app.modes.set("DCV", dcv);
  api.app.selectedMode = "DCV";
  api.updateStateDisplay(connectedState({
    latest: { sample_index: 9, value_si: 1, unit_si: "V", mode: "DCV", status: "ok" },
    stats: { attempt_count: 9, valid_count: 7, abnormal_count: 2 },
  }));
  assert.equal(element(elements, "sampleCount").textContent, "7");
});

test("each Web summary statistic shows its sample count beside the value and unit", () => {
  const html = fs.readFileSync(path.join(ROOT, "src/fluke8845a/web/index.html"), "utf8");
  const { api, elements } = createFrontend();
  api.app.modes.set("DCV", dcv);
  api.app.selectedMode = "DCV";
  api.app.points = [
    { id: 1, x: 0, y: 1, status: "ok", unit: "V" },
    { id: 2, x: 1, y: 3, status: "ok", unit: "V" },
  ];
  api.updateStats();

  for (const id of ["statMean", "statStd", "statMin", "statMax"]) {
    assert.match(html, new RegExp(`id="${id}Count"`));
    assert.equal(element(elements, `${id}Count`).textContent, "n=2");
    assert.match(element(elements, id).textContent, /V$/);
  }
});

test("primary status metrics follow connection, acquisition, reading, samples, and interval order", () => {
  const html = fs.readFileSync(path.join(ROOT, "src/fluke8845a/web/index.html"), "utf8");
  const ids = ["connectionBadge", "runStatus", "readingMode", "readingValue", "sampleCount", "actualRate"];
  const positions = ids.map((id) => html.indexOf(`id=\"${id}\"`));
  assert.ok(positions.every((position) => position >= 0));
  assert.deepEqual(positions, [...positions].sort((a, b) => a - b));
});

test("mode settings bind only controls supported by the selected mode", () => {
  const { api, context, elements } = createFrontend();
  const getElementById = context.document.getElementById;
  const modeOptionIds = new Set(["autorange", "range", "nplc", "bandwidth", "analogFilter", "inputImpedanceAuto", "digitalFilter", "autozero"]);
  context.document.getElementById = (id) => {
    if (modeOptionIds.has(id) && !element(elements, "modeOptions").innerHTML.includes(`id="${id}"`)) return null;
    return getElementById(id);
  };

  api.app.modes.set("DCV", dcv);
  assert.doesNotThrow(() => api.populateModeOptions(dcv));
});

test("connection stage chips render each phase status using text content", () => {
  const { api, elements } = createFrontend();
  api.app.modes.set("DCV", dcv);
  api.updateStateDisplay(connectedState({
    connection_stages: [
      { stage: "device", status: "success", message: "设备 COM3 已选定" },
      { stage: "port", status: "running", message: "正在打开串口" },
      { stage: "identify", status: "failure", message: "识别失败 <bad>" },
    ],
  }));
  const chips = element(elements, "connectionStages").children;
  assert.deepEqual(chips.map((chip) => chip.textContent), [
    "设备已选: 成功", "串口: 进行中", "8845A识别: 失败",
  ]);
  assert.equal(chips[2].title, "识别失败 <bad>");
  assert.equal(chips[2].innerHTML, "");
});

test("Web chart and settings keep empty space compact and auxiliary copy readable", () => {
  const css = fs.readFileSync(path.join(ROOT, "src/fluke8845a/web/app.css"), "utf8");
  assert.match(css, /\.measurement-area\.chart-empty \.chart-panel\s*\{[^}]*height:\s*clamp\(220px,\s*34vh,\s*320px\)/);
  assert.match(css, /\.field-hint[^\n]*font-size:\s*12px/);
  assert.match(css, /\.telemetry-grid span[^\n]*font-size:\s*12px/);
  assert.match(css, /\.connection-stages li[^\n]*font-size:\s*12px/);
  assert.match(css, /\.plot-empty > span:not\(\.empty-icon\)[^\n]*font-size:\s*12px/);
  assert.match(css, /--muted[^;]*#(?:[5-7][0-9a-f]{5})/i);
});

test("Web disconnect exposes reconnect while preserving last serial settings", async () => {
  const html = fs.readFileSync(path.join(ROOT, "src/fluke8845a/web/index.html"), "utf8");
  assert.match(html, /id="reconnectReal"[^>]*>重新连接/);
  const { api, elements, context, storage } = createFrontend();
  api.app.modes.set("DCV", dcv);
  api.app.selectedMode = "DCV";
  element(elements, "port").value = "COM3";
  element(elements, "baudrate").value = "115200";
  context.fetch = async (_url, options = {}) => ({ ok: true, json: async () => ({ success: true, message: "已连接" }) });
  await api.connectReal();
  assert.equal(JSON.parse(storage.get("8845a-web-preferences")).baudrate, "115200");
  api.updateStateDisplay(connectedState({ connected: false, simulation: false, device: {} }));
  assert.equal(element(elements, "reconnectReal").classList.contains("hidden"), false);
});

test("selecting a scanned serial port shows its device description and connection target", async () => {
  const { api, elements, context } = createFrontend();
  api.bindControls();
  context.fetch = async (url) => ({
    ok: true,
    json: async () => url === "/api/ports" ? {
      ports: [{ device: "COM7", description: "USB Serial Device" }],
    } : { success: true },
  });

  await element(elements, "scanPorts").dispatch("click");
  element(elements, "portList").value = "COM7";
  await element(elements, "portList").dispatch("change");

  assert.equal(element(elements, "port").value, "COM7");
  assert.match(element(elements, "portSelectionInfo").textContent, /COM7.*USB Serial Device/);
});

test("serial status never describes a Mock connection as an open COM port", () => {
  const { api, elements } = createFrontend();
  element(elements, "port").value = "COM3";

  api.updateStateDisplay(connectedState({ connected: true, simulation: true }));
  assert.match(element(elements, "portSelectionInfo").textContent, /模拟器已连接.*未打开串口/);
  assert.doesNotMatch(element(elements, "portSelectionInfo").textContent, /已连接 COM3/);

  api.updateStateDisplay(connectedState({
    connected: true, simulation: false, device: { idn: "FLUKE,8845A,123,2.0" },
  }));
  assert.match(element(elements, "portSelectionInfo").textContent, /已连接 COM3.*FLUKE,8845A/);

  api.updateStateDisplay(connectedState({ connected: false, simulation: false }));
  assert.match(element(elements, "portSelectionInfo").textContent, /准备连接：COM3/);
});

test("Web advanced serial controls start collapsed and offer recommended settings reset", () => {
  const html = fs.readFileSync(path.join(ROOT, "src/fluke8845a/web/index.html"), "utf8");
  assert.match(html, /<details id="serialAdvanced"[^>]*>[\s\S]*?<summary>高级串口参数/);
  assert.match(html, /id="resetSerialDefaults"[^>]*>恢复推荐串口设置/);
  assert.match(html, /<option selected>9600<\/option>/);
  assert.doesNotMatch(html, /<option selected>115200<\/option>/);
});

test("manual panel software record shows its source, update time, and clear action", () => {
  const html = fs.readFileSync(path.join(ROOT, "src/fluke8845a/web/index.html"), "utf8");
  assert.match(html, /id="manualSoftwareInfo"/);
  assert.match(html, /id="clearManualSoftware"[^>]*>清除面板版本记录/);
  assert.match(html, /manualSoftwareInfo[\s\S]*来源：仪表面板人工核对/);
});

test("settings compare active readback with every currently selected setting", () => {
  const html = fs.readFileSync(path.join(ROOT, "src/fluke8845a/web/index.html"), "utf8");
  assert.match(html, /id="settingsComparison"/);
  const { api, elements } = createFrontend();
  api.app.modes.set("DCV", dcv);
  api.app.selectedMode = "DCV";
  element(elements, "mode").value = "DCV";
  element(elements, "autorange").checked = false;
  element(elements, "range").value = "1";
  element(elements, "nplc").value = "10";
  element(elements, "autozero").checked = false;
  api.updateStateDisplay(connectedState({ active_settings: {
    mode: "DCV", autorange: true, range_si: null, nplc: 1,
    analog_filter: false, autozero: true,
  } }));
  const comparison = element(elements, "settingsComparison").innerHTML;
  assert.match(comparison, /当前生效/);
  assert.match(comparison, /待应用/);
  assert.match(comparison, /量程/);
  assert.match(comparison, /NPLC/);
  assert.match(comparison, /数字滤波/);
  assert.match(comparison, /自动零点/);
  assert.match(comparison, /不同/);
});

test("DCV settings expose the documented high input impedance option", () => {
  const { api, elements } = createFrontend();
  api.app.modes.set("DCV", { ...dcv, input_impedance_auto: true });
  api.populateModeOptions({ ...dcv, input_impedance_auto: true });
  assert.match(element(elements, "modeOptions").innerHTML, /自动高输入阻抗/);
  const options = element(elements, "modeOptions").innerHTML;
  assert.match(options, /100 mV、1 V、10 V/);
  assert.match(options, /大于10 GΩ/);
  assert.doesNotMatch(options, /SENS:FUNC/);
});

test("Web keeps DCV high input impedance selection separate across mode changes", () => {
  const { api, elements } = createFrontend();
  const dcvMode = { ...dcv, input_impedance_auto: true };
  const acv = { ...dcv, value: "ACV", label: "交流电压", nplc: [], bandwidths: [3, 20, 200], input_impedance_auto: false };
  api.app.modes.set("DCV", dcvMode);
  api.app.modes.set("ACV", acv);
  api.populateModeOptions(dcvMode);
  element(elements, "inputImpedanceAuto").checked = true;
  api.populateModeOptions(acv);
  assert.doesNotMatch(element(elements, "modeOptions").innerHTML, /inputImpedanceAuto/);
  api.populateModeOptions(dcvMode);
  assert.equal(api.app.dcvInputImpedanceAuto, true);
  assert.match(element(elements, "modeOptions").innerHTML, /id="inputImpedanceAuto" type="checkbox" checked/);
});

test("Web NPLC controls use the backend's per-mode digital-filter policy", () => {
  const { api, elements } = createFrontend();
  api.app.modes.set("DCV", dcv);
  api.app.selectedMode = "DCV";
  const nplc = element(elements, "nplc");
  nplc.options = [
    { value: "1", disabled: false },
    { value: "10", disabled: false },
    { value: "100", disabled: false },
  ];
  const sharedReason = "尚未核实OutG SW；当前软件兼容策略门槛未获手册证实。请选择1 NPLC及以下。";

  api.updateStateDisplay(connectedState({
    simulation: false,
    device: {
      digital_filter_blocked_nplc_values: { DCV: [10, 100] },
      digital_filter_slow_read_block_reasons: { DCV: sharedReason },
    },
  }));

  assert.deepEqual(nplc.options.map((option) => option.disabled), [false, true, true]);
  assert.equal(nplc.title, sharedReason);
});

test("mode changes apply the NPLC guard to the newly rendered controls immediately", () => {
  const { api, elements } = createFrontend();
  api.app.state = connectedState({
    simulation: false,
    device: { digital_filter_blocked_nplc_values: { DCV: [10, 100] } },
  });
  const panel = element(elements, "modeOptions");
  let markup = "";
  Object.defineProperty(panel, "innerHTML", {
    configurable: true,
    get() { return markup; },
    set(value) {
      markup = String(value);
      if (markup.includes('id="nplc"')) {
        const select = new FakeElement("nplc");
        select.options = [0.02, 0.2, 1, 10, 100].map((value) => ({
          value: String(value), disabled: false,
        }));
        elements.set("nplc", select);
      } else {
        elements.delete("nplc");
      }
    },
  });

  api.populateModeOptions(dcv);

  assert.deepEqual(
    element(elements, "nplc").options.map((option) => option.disabled),
    [false, false, false, true, true],
  );
});

test("disabled digital filter reports no difference when both active and pending are off", () => {
  const { api, elements } = createFrontend();
  api.app.modes.set("DCV", dcv);
  api.app.selectedMode = "DCV";
  element(elements, "mode").value = "DCV";
  api.updateStateDisplay(connectedState({ active_settings: {
    mode: "DCV", autorange: true, range_si: null, nplc: 1,
    analog_filter: false, digital_filter: false, digital_filter_state: "readback_off", autozero: true,
  } }));
  const comparison = element(elements, "settingsComparison").innerHTML;
  const digitalRow = comparison.match(/<span>数字滤波<\/span>(.*?)<span>自动零点<\/span>/);
  assert.ok(digitalRow, "digital filter has a comparison row when the mode supports it");
  assert.match(digitalRow[1], /<span>关<\/span><span class="">关<\/span>/);
  assert.doesNotMatch(digitalRow[1], /不同/);
});

test("Web settings comparison marks an unprobed low-NPLC filter as inactive instead of off", () => {
  const { api, elements } = createFrontend();
  api.app.modes.set("DCV", dcv);
  api.app.selectedMode = "DCV";
  element(elements, "mode").value = "DCV";
  api.updateStateDisplay(connectedState({ simulation: false, active_settings: {
    mode: "DCV", autorange: true, range_si: null, nplc: 1,
    analog_filter: false, digital_filter: false,
    digital_filter_state: "unprobed_inactive", autozero: true,
  } }));

  const comparison = element(elements, "settingsComparison").innerHTML;
  const digitalRow = comparison.match(/<span>数字滤波<\/span>(.*?)<span>自动零点<\/span>/);
  assert.ok(digitalRow);
  assert.match(digitalRow[1], /未探测.*当前不活动/);
  assert.doesNotMatch(digitalRow[1], /<span>关<\/span>/);
});

test("Web settings comparison identifies a blocked slow filter state as unprobed", () => {
  const { api, elements } = createFrontend();
  api.app.modes.set("DCV", dcv);
  api.app.selectedMode = "DCV";
  element(elements, "mode").value = "DCV";
  api.updateStateDisplay(connectedState({ simulation: false, active_settings: {
    mode: "DCV", autorange: true, range_si: null, nplc: 10,
    analog_filter: false, digital_filter: false,
    digital_filter_state: "unprobed", autozero: true,
  } }));

  const comparison = element(elements, "settingsComparison").innerHTML;
  const digitalRow = comparison.match(/<span>数字滤波<\/span>(.*?)<span>自动零点<\/span>/);
  assert.ok(digitalRow);
  assert.match(digitalRow[1], /未探测/);
  assert.doesNotMatch(digitalRow[1], /<span>关<\/span>/);
});

test("autorange readout identifies the actual range returned with that sample", () => {
  const { api, elements } = createFrontend();
  api.app.modes.set("DCV", dcv);
  api.app.selectedMode = "DCV";
  api.updateStateDisplay(connectedState({
    active_settings: { mode: "DCV", autorange: true },
    latest: { sample_index: 2, value_si: 0.42, range_si: 1, unit_si: "V", mode: "DCV", status: "ok", raw_reading: "0.42" },
    stats: { attempt_count: 2, valid_count: 2, abnormal_count: 0 },
  }));
  assert.match(element(elements, "readingRaw").textContent, /实际量程\s*1 V/);
});

test("AC bandwidth and NPLC hints update for the selected measurement mode", () => {
  const { api, elements } = createFrontend();
  const acv = { ...dcv, value: "ACV", label: "交流电压", nplc: [], bandwidths: [3, 20, 200], quantity: "voltage" };
  api.populateModeOptions(acv);
  assert.match(element(elements, "modeOptions").innerHTML, /当前模式可调交流响应带宽/);
  assert.match(element(elements, "modeOptions").innerHTML, /交流响应带宽会影响稳定时间和噪声/);
  assert.match(element(elements, "modeOptions").innerHTML, /自动零点.*实测.*间隔/);
  assert.match(element(elements, "modeOptions").innerHTML, /软件目标间隔.*实测采样间隔/);
  assert.doesNotMatch(element(elements, "modeOptions").innerHTML, /NPLC 越高/);
  const dcvWithFilter = { ...dcv, analog_filter: true };
  api.populateModeOptions(dcvWithFilter);
  assert.match(element(elements, "modeOptions").innerHTML, /NPLC 越高/);
  assert.match(element(elements, "modeOptions").innerHTML, /模拟滤波.*实测.*间隔/);
  assert.doesNotMatch(element(elements, "modeOptions").innerHTML, /交流响应带宽会影响/);
});

test("Web exit explains that closing the page leaves service and acquisition running", async () => {
  const html = fs.readFileSync(path.join(ROOT, "src/fluke8845a/web/index.html"), "utf8");
  assert.match(html, /id="shutdownService"[^>]*title="[^"]*关闭浏览器标签[^\"]*采集仍会继续/);
  const { api, elements, context } = createFrontend();
  const confirmations = [];
  const requests = [];
  context.window.confirm = (message) => { confirmations.push(message); return false; };
  context.fetch = async (url, options) => {
    requests.push({ url, options });
    return { json: async () => ({ success: true, message: "服务正在退出" }) };
  };
  api.bindControls();

  await element(elements, "shutdownService").dispatch("click");
  assert.match(confirmations[0], /先停止采集.*保存并关闭当前实验.*关闭 localhost 服务/);
  assert.match(confirmations[0], /关闭网页标签页不会停止采集或服务/);
  assert.equal(requests.length, 0, "cancelling the confirmation must not send shutdown");

  context.window.confirm = (message) => { confirmations.push(message); return true; };
  await element(elements, "shutdownService").dispatch("click");
  assert.equal(requests.length, 1);
  assert.equal(requests[0].url, "/api/command");
  assert.equal(JSON.parse(requests[0].options.body).action, "shutdown");
});

test("mode terminal panel shows named 8845A jacks and backend metadata supplies terminal groups", () => {
  const { api, elements } = createFrontend();
  const current = { ...dcv, value: "DCI", label: "直流电流", quantity: "current", wiring_group: "current" };
  api.populateModeOptions(current);
  const diagram = element(elements, "terminalDiagram").innerHTML;
  assert.match(diagram, /terminal-socket terminal-hi/);
  assert.match(diagram, /terminal-socket terminal-lo/);
  assert.ok(diagram.includes("HI") && diagram.includes("LO"));
  assert.ok(diagram.includes("mA / 400 mA") && diagram.includes("10 A"));
});

test("resistance plot labels use the dynamically scaled vertical-axis unit", () => {
  const { api, elements } = createFrontend({ stubChart: false });
  api.app.modes.set("RES2", { ...dcv, value: "RES2", label: "两线电阻", unit: "Ohm", quantity: "resistance" });
  api.app.selectedMode = "RES2";
  api.app.points = [
    { id: 1, x: 0, y: 29_970_000, status: "ok", mode: "RES2", unit: "Ohm" },
    { id: 2, x: 1, y: 29_980_000, status: "ok", mode: "RES2", unit: "Ohm" },
  ];
  api.drawChart();
  const labels = element(elements, "plot").canvasContext.calls.map(([kind, label]) => kind === "text" ? label : "").join(" ");
  assert.match(labels, /电阻 \(MΩ\)/);
  assert.match(labels, /29\./);
});

test("manual panel firmware changes save source time and can be cleared", async () => {
  const { api, elements, context, storage } = createFrontend();
  api.app.activeDeviceSerial = "9383035";
  api.app.state = connectedState({ simulation: false, device: { serial_number: "9383035" } });
  element(elements, "outguardSoftware").value = "2.10.882.24";
  element(elements, "inguardSoftware").value = "ad300210";
  mockSuccessfulCommand(context);
  await api.saveManualSoftware();
  let saved = JSON.parse(storage.get("8845a-front-panel-software"))["9383035"];
  assert.equal(saved.source, "仪表面板人工核对");
  assert.ok(Number.isFinite(Date.parse(saved.updated_at)));
  assert.match(element(elements, "manualSoftwareInfo").textContent, /更新时间/);
  await api.clearManualSoftware();
  assert.equal(JSON.parse(storage.get("8845a-front-panel-software"))["9383035"], undefined);
  assert.match(element(elements, "manualSoftwareInfo").textContent, /尚无记录/);
});

test("automatic IDN firmware parsing is shown separately from manual OutG and InG records", () => {
  const html = fs.readFileSync(path.join(ROOT, "src/fluke8845a/web/index.html"), "utf8");
  assert.match(html, /id="idnFirmwareInfo"/);
  const { api, elements, storage } = createFrontend();
  storage.set("8845a-front-panel-software", JSON.stringify({ "9383035": {
    outguard_sw: "2.10.882.24", inguard_sw: "ad300210", source: "仪表面板人工核对",
  } }));
  api.updateStateDisplay(connectedState({ simulation: false, device: {
    idn: "FLUKE,8845A,9383035,2.0.1", serial_number: "9383035",
    firmware: "2.0.1", idn_revision: "2.0.1",
  } }));

  const automatic = element(elements, "idnFirmwareInfo").textContent;
  const manual = element(elements, "manualSoftwareInfo").textContent;
  assert.match(automatic, /\*IDN\?.*2\.0\.1.*解析/);
  assert.match(manual, /仪表面板人工核对/);
  assert.match(element(elements, "outguardSoftware").value, /2\.10\.882\.24/);
  assert.match(element(elements, "inguardSoftware").value, /ad300210/);
  assert.doesNotMatch(automatic, /OutG|InG|ad300210|2\.10\.882\.24/);

  api.updateStateDisplay(connectedState({ simulation: false, device: {
    idn: "FLUKE,8845A,9383035,02/14/08-08:54", serial_number: "9383035",
    firmware: null, idn_revision: "02/14/08-08:54",
  } }));
  assert.match(element(elements, "idnFirmwareInfo").textContent, /\*IDN\?.*无法解析.*02\/14\/08-08:54/);
  assert.match(element(elements, "manualSoftwareInfo").textContent, /仪表面板人工核对/);
});

test("clearing manual panel software asks for confirmation before sending or erasing", async () => {
  const { api, elements, context, storage } = createFrontend();
  const profile = { outguard_sw: "2.10.882.24", inguard_sw: "ad300210", source: "仪表面板人工核对", updated_at: new Date().toISOString() };
  storage.set("8845a-front-panel-software", JSON.stringify({ "9383035": profile }));
  api.app.activeDeviceSerial = "9383035";
  api.app.state = connectedState({ simulation: false, device: { serial_number: "9383035" } });
  element(elements, "outguardSoftware").value = "2.10.882.24";
  context.window.confirm = () => false;
  let commands = 0;
  context.fetch = async () => { commands += 1; return { ok: true, json: async () => ({ success: true }) }; };

  await api.clearManualSoftware();

  assert.equal(commands, 0);
  assert.equal(JSON.parse(storage.get("8845a-front-panel-software"))["9383035"].outguard_sw, "2.10.882.24");
  assert.equal(element(elements, "outguardSoftware").value, "2.10.882.24");
});

test("saving a new manual software profile preserves all existing device records", async () => {
  const { api, elements, context, storage } = createFrontend();
  const profiles = Object.fromEntries(Array.from({ length: 105 }, (_, index) => [`serial-${index}`, {
    outguard_sw: `2.${index}`, inguard_sw: "", source: "仪表面板人工核对", updated_at: new Date().toISOString(),
  }]));
  storage.set("8845a-front-panel-software", JSON.stringify(profiles));
  api.app.activeDeviceSerial = "9383035";
  api.app.state = connectedState({ simulation: false, device: { serial_number: "9383035" } });
  element(elements, "outguardSoftware").value = "2.10.882.24";
  mockSuccessfulCommand(context);

  await api.saveManualSoftware();

  const saved = JSON.parse(storage.get("8845a-front-panel-software"));
  assert.equal(Object.keys(saved).length, 106);
  assert.equal(saved["serial-104"].outguard_sw, "2.104");
  assert.equal(saved["9383035"].outguard_sw, "2.10.882.24");
});

test("sample rows retain and expose per-reading instrument range", () => {
  const { api, elements } = createFrontend();
  api.app.modes.set("DCV", dcv);
  api.app.selectedMode = "DCV";
  api.appendSampleRows([{
    sample_index: 1, elapsed_s: 0, value_si: 0.1, range_si: 0.1,
    status: "ok", raw_reading: "0.100", mode: "DCV", unit_si: "V", timestamp_local: "2026-10-16T12:00:00+08:00",
  }]);
  assert.equal(api.app.points[0].range, 0.1);
  assert.match(element(elements, "pointTableBody").innerHTML, /100 mV/);
});

test("recommended serial settings reset only advanced serial fields", async () => {
  const { api, elements } = createFrontend();
  api.bindControls();
  element(elements, "port").value = "COM7";
  element(elements, "baudrate").value = "9600";
  element(elements, "parity").value = "E";
  await element(elements, "resetSerialDefaults").dispatch("click");
  assert.equal(element(elements, "port").value, "COM7");
  assert.equal(element(elements, "baudrate").value, "9600");
  assert.equal(element(elements, "bytesize").value, "8");
  assert.equal(element(elements, "parity").value, "N");
  assert.equal(element(elements, "eol").value, "CRLF");
  assert.equal(element(elements, "flow").value, "none");
});

class FakeClassList {
  constructor() { this.names = new Set(); }
  add(name) { this.names.add(name); }
  remove(name) { this.names.delete(name); }
  contains(name) { return this.names.has(name); }
  toggle(name, force) {
    const enabled = force === undefined ? !this.names.has(name) : Boolean(force);
    if (enabled) this.names.add(name);
    else this.names.delete(name);
    return enabled;
  }
}

class FakeElement {
  constructor(id = "") {
    this.id = id;
    this.value = "";
    this.textContent = "";
    this.innerHTML = "";
    this.checked = false;
    this.disabled = false;
    this.className = "";
    this.classList = new FakeClassList();
    this.dataset = {};
    this.listeners = new Map();
    this.children = [];
    this.parentElement = { classList: new FakeClassList() };
    this.files = [];
    this.scrollHeight = 0;
    this.scrollTop = 0;
    this.width = 0;
    this.height = 0;
    this.canvasContext = {
      calls: [], strokes: [], currentPath: [], dash: [],
      setTransform() {}, clearRect() {}, fillRect() {}, strokeRect() {}, save() {}, restore() {}, scale() {},
      translate() {}, rotate() {}, fill() {}, clip() {}, arc() {}, rect() {}, drawImage() {},
      beginPath() { this.currentPath = []; },
      moveTo(x, y) { this.currentPath.push(["M", x, y]); },
      lineTo(x, y) { this.currentPath.push(["L", x, y]); },
      stroke() { this.strokes.push({ color: this.strokeStyle, dash: [...this.dash], path: [...this.currentPath] }); },
      setLineDash(values) { this.dash = [...values]; },
      fillText(text, x, y) { this.calls.push(["text", String(text), x, y, this.font]); },
      measureText(text) { return { width: String(text).length * 6 }; },
    };
  }
  addEventListener(type, listener) {
    const listeners = this.listeners.get(type) || [];
    listeners.push(listener);
    this.listeners.set(type, listeners);
  }
  dispatch(type, event = {}) {
    return Promise.all((this.listeners.get(type) || []).map((listener) => listener({ target: this, ...event })));
  }
  querySelector(selector) {
    if (selector === "span") {
      if (!this.span) this.span = new FakeElement(`${this.id}-span`);
      return this.span;
    }
    return new FakeElement(`${this.id}-${selector}`);
  }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children = [...children]; }
  remove() {}
  click() {}
  setPointerCapture() {}
  showModal() { this.showModalCalled = true; }
  close() { this.closeCalled = true; }
  getBoundingClientRect() { return { left: 0, top: 0, width: 800, height: 400 }; }
  focus() { this.focusCalled = true; }
  select() { this.selectCalled = true; }
  getContext() { return this.canvasContext; }
  toBlob(callback) { callback(null); }
}

function createFrontend({ preferences = null, stubChart = true } = {}) {
  const elements = new Map();
  const storage = new Map();
  const animationFrames = [];
  const historyWorkerSource = fs.readFileSync(path.join(ROOT, "src/fluke8845a/web/history_worker.js"), "utf8");
  class InlineHistoryWorker {
    constructor(url) {
      this.url = url;
      let handler;
      const scope = {
        addEventListener(type, callback) { if (type === "message") handler = callback; },
        postMessage: (data) => { if (!this.terminated) this.onmessage?.({ data }); },
      };
      vm.runInNewContext(historyWorkerSource, { self: scope });
      this.handleMessage = (event) => handler(event);
    }
    postMessage(data) { queueMicrotask(() => this.handleMessage({ data })); }
    terminate() { this.terminated = true; }
  }
  if (preferences !== null) storage.set("8845a-web-preferences", JSON.stringify(preferences));
  const clipboard = { text: "", async writeText(text) { this.text = text; } };
  const document = {
    getElementById(id) {
      if (!elements.has(id)) elements.set(id, new FakeElement(id));
      return elements.get(id);
    },
    querySelector() { return new FakeElement(); },
    createDocumentFragment() { return new FakeElement("fragment"); },
    createElement(tag) { return new FakeElement(tag); },
    addEventListener() {},
  };
  const context = vm.createContext({
    document,
    window: { confirm: () => true, location: { assign() {} }, addEventListener() {} },
    localStorage: {
      getItem: (key) => storage.get(key) || null,
      setItem: (key, value) => storage.set(key, String(value)),
      removeItem: (key) => storage.delete(key),
    },
    navigator: { clipboard },
    fetch: async () => { throw new Error("offline"); },
    URLSearchParams,
    setTimeout: () => 1,
    clearTimeout() {},
    setInterval: () => 1,
    clearInterval() {},
    requestAnimationFrame: (callback) => { animationFrames.push(callback); return animationFrames.length; },
    Worker: InlineHistoryWorker,
    console,
  });
  const source = fs.readFileSync(path.join(ROOT, "src/fluke8845a/web/app.js"), "utf8")
    .replace(/\ninitialize\(\);\s*$/, "");
  vm.runInContext(`${source}\n;globalThis.__frontend = { app, ui, command, updateStateDisplay, pollState, scanPorts, startPayload, startContinuous, startSingle, populateModeOptions, bindControls, chooseDuplicateNameAction, connectReal, connectMock, drawChart, buildPixelPointIndex, nearestPoint, appendSampleRows, saveManualSoftware, clearManualSoftware, renderSettingsComparison, refreshExperimentFiles: typeof refreshExperimentFiles === "function" ? refreshExperimentFiles : null, downloadLatestCsv: typeof downloadLatestCsv === "function" ? downloadLatestCsv : null, scheduleChartRedraw: typeof scheduleChartRedraw === "function" ? scheduleChartRedraw : null, downsampleForRender: typeof downsampleForRender === "function" ? downsampleForRender : null, classifyLog: typeof classifyLog === "function" ? classifyLog : null, filterLogRows: typeof filterLogRows === "function" ? filterLogRows : null, formatLogsText: typeof formatLogsText === "function" ? formatLogsText : null, loadHistory, loadHistoryEvents: typeof loadHistoryEvents === "function" ? loadHistoryEvents : null, restorePreferences, updateStats, renderEvents, focusEvent, updateAccessiblePointTable: typeof updateAccessiblePointTable === "function" ? updateAccessiblePointTable : null, clearPreferences: typeof clearPreferences === "function" ? clearPreferences : null, localExperimentName: typeof localExperimentName === "function" ? localExperimentName : null, copyAccessiblePointTable: typeof copyAccessiblePointTable === "function" ? copyAccessiblePointTable : null, updateEmptyPlotState: typeof updateEmptyPlotState === "function" ? updateEmptyPlotState : null, createPlotExportCanvas: typeof createPlotExportCanvas === "function" ? createPlotExportCanvas : null, copyExperimentPath: typeof copyExperimentPath === "function" ? copyExperimentPath : null, experimentNameError: typeof experimentNameError === "function" ? experimentNameError : null };`, context);
  if (stubChart) vm.runInContext("drawChart = () => {}; updateStats = () => {};", context);
  return { api: context.__frontend, elements, context, storage, clipboard, animationFrames };
}

function element(elements, id) {
  if (!elements.has(id)) elements.set(id, new FakeElement(id));
  return elements.get(id);
}

const dcv = {
  value: "DCV", label: "直流电压", unit: "V", ranges: [0.1, 1, 10],
  nplc: [0.02, 0.2, 1, 10], bandwidths: [], analog_filter: false,
  digital_filter: true, safety: "HI/LO 接线要求",
};

function connectedState(overrides = {}) {
  return {
    connected: true, simulation: true, control_mode: "computer", status: "等待连接",
    connection_text: "MOCK 模拟器已连接", active_settings: { mode: "DCV" },
    actual_hz: null, device: {}, ...overrides,
  };
}

test("state polling renders a mode without shadowing the activeMode lookup", () => {
  const { api, elements } = createFrontend();
  api.app.modes.set("DCV", dcv);
  api.app.selectedMode = "DCV";
  assert.doesNotThrow(() => api.updateStateDisplay(connectedState()));
  assert.equal(element(elements, "readingMode").textContent, "直流电压");
});

test("service polling failure is separate from the last instrument and acquisition status", async () => {
  const { api, elements } = createFrontend();
  api.app.state = connectedState({ status: "采集中" });
  element(elements, "runStatus").textContent = "采集中";
  element(elements, "connectionText").textContent = "MOCK 模拟器已连接";
  element(elements, "connectionBadge").classList.add("mock");

  await api.pollState();

  assert.equal(element(elements, "runStatus").textContent, "采集中");
  assert.equal(element(elements, "connectionText").textContent, "MOCK 模拟器已连接");
  assert.equal(element(elements, "connectionBadge").classList.contains("mock"), true);
  assert.match(element(elements, "serviceStatus").textContent, /服务.*(中断|不可用)/);
});

test("state polling skips a new request while the previous request is still in flight", async () => {
  const { api, context } = createFrontend();
  const pending = [];
  let requests = 0;
  context.fetch = () => {
    requests += 1;
    return new Promise((resolve) => pending.push(resolve));
  };

  const first = api.pollState();
  const second = api.pollState();

  assert.equal(requests, 1);
  assert.equal(pending.length, 1);
  pending[0]({
    ok: true,
    json: async () => connectedState({ generation: 1, sample_buffer_start: 1, samples: [], logs: [], log_seq: 0, events: [] }),
  });
  await Promise.all([first, second]);

  assert.equal(api.app.generation, 1);
  assert.equal(requests, 1);

  const afterCompletion = api.pollState();
  assert.equal(requests, 2);
  pending[1]({
    ok: true,
    json: async () => connectedState({ generation: 1, sample_buffer_start: 1, samples: [], logs: [], log_seq: 0, events: [] }),
  });
  await afterCompletion;
});

test("state polling can retry after a request throws", async () => {
  const { api, context } = createFrontend();
  let requests = 0;
  context.fetch = async () => {
    requests += 1;
    if (requests === 1) throw new Error("temporary connection failure");
    return {
      ok: true,
      json: async () => connectedState({ generation: 1, sample_buffer_start: 1, samples: [], logs: [], log_seq: 0, events: [] }),
    };
  };

  await api.pollState();
  assert.equal(api.app.pollInFlight, false);
  await api.pollState();

  assert.equal(requests, 2);
  assert.equal(api.app.pollInFlight, false);
});

test("Mock mode hides physical wiring confirmation and sends no physical confirmation field", () => {
  const { api, elements } = createFrontend();
  api.app.modes.set("DCV", dcv);
  api.app.selectedMode = "DCV";
  api.app.state = connectedState();
  api.updateStateDisplay(api.app.state);

  assert.equal(element(elements, "safetyControls").classList.contains("hidden"), true);
  assert.equal(Object.hasOwn(api.startPayload(), "safety_confirmed"), false);
});

test("real instrument mode keeps wiring confirmation visible and user controlled", () => {
  const { api, elements } = createFrontend();
  api.app.modes.set("DCV", dcv);
  api.app.selectedMode = "DCV";
  api.app.state = connectedState({ simulation: false, connection_text: "8845A 已连接" });
  api.updateStateDisplay(api.app.state);

  assert.equal(element(elements, "mockScenarioRow").classList.contains("hidden"), true);
  assert.equal(element(elements, "safetyControls").classList.contains("hidden"), false);
  assert.equal(element(elements, "mockSafetyNote").classList.contains("hidden"), true);
  element(elements, "safetyConfirm").checked = true;
  assert.equal(api.startPayload().safety_confirmed, true);
});

test("connected Mock scenario cannot be edited without reconnecting", () => {
  const { api, elements } = createFrontend();
  api.app.modes.set("DCV", dcv);
  api.app.selectedMode = "DCV";
  api.updateStateDisplay(connectedState());

  assert.equal(element(elements, "mockScenarioRow").classList.contains("hidden"), false);
  assert.equal(element(elements, "mockScenario").disabled, true);
});

test("Mock scenario is selectable before connection and is sent with the request", async () => {
  const { api, elements, context } = createFrontend();
  const html = fs.readFileSync(path.join(ROOT, "src/fluke8845a/web/index.html"), "utf8");
  assert.match(html, /id="mockScenarioRow" class="scenario-row"/);
  api.app.modes.set("DCV", dcv);
  api.app.selectedMode = "DCV";
  api.app.state = connectedState({ connected: false, simulation: false });
  api.updateStateDisplay(api.app.state);

  assert.equal(element(elements, "mockScenarioRow").classList.contains("hidden"), false);
  assert.equal(element(elements, "mockScenario").disabled, false);
  element(elements, "mockScenario").value = "长期漂移";

  const requests = mockSuccessfulCommand(context);
  await api.connectMock();

  const request = JSON.parse(requests[0].options.body);
  assert.equal(request.action, "connect_mock");
  assert.equal(request.payload.scenario, "长期漂移");
});

test("changing mode or range clears the previous physical wiring confirmation", () => {
  const { api, elements } = createFrontend();
  api.app.modes.set("DCV", dcv);
  api.app.selectedMode = "DCV";
  api.populateModeOptions(dcv);
  api.bindControls();

  element(elements, "mode").value = "DCV";
  element(elements, "safetyConfirm").checked = true;
  element(elements, "mode").dispatch("change");
  assert.equal(element(elements, "safetyConfirm").checked, false);

  element(elements, "safetyConfirm").checked = true;
  element(elements, "range").dispatch("change");
  assert.equal(element(elements, "safetyConfirm").checked, false);

  element(elements, "safetyConfirm").checked = true;
  element(elements, "autorange").checked = true;
  element(elements, "autorange").dispatch("change");
  assert.equal(element(elements, "safetyConfirm").checked, false);
});

test("physical mode, fixed-range, and terminal readback changes clear wiring confirmation", async () => {
  const { api, elements, context } = createFrontend();
  const dci = { ...dcv, value: "DCI", label: "直流电流", quantity: "current", unit: "A", ranges: [.01, .1, .4, 1, 3, 10] };
  const aci = { ...dci, value: "ACI", label: "交流电流" };
  api.app.modes.set("DCI", dci);
  api.app.modes.set("ACI", aci);
  api.app.selectedMode = "DCI";
  const stateFor = ({ mode = "DCI", range = .1, terminal = "FRONT" } = {}) => connectedState({
    simulation: false,
    control_mode: "computer",
    connection_text: "8845A 已连接",
    device: { serial_number: "9383035" },
    configuration: { input_terminals: terminal },
    active_settings: { mode, autorange: false, range_si: range },
    generation: 1,
    sample_buffer_start: 1,
    samples: [],
    logs: [],
    log_seq: 0,
    events: [],
  });
  const states = [
    stateFor(), stateFor(), stateFor({ terminal: "REAR" }),
    stateFor({ terminal: "REAR", range: .4 }), stateFor({ mode: "ACI", terminal: "REAR", range: .4 }),
  ];
  context.fetch = async () => ({ ok: true, json: async () => states.shift() });

  await api.pollState();
  element(elements, "safetyConfirm").checked = true;
  await api.pollState();
  assert.equal(element(elements, "safetyConfirm").checked, true, "stable readback keeps the user's confirmation");
  await api.pollState();
  assert.equal(element(elements, "safetyConfirm").checked, false, "terminal change invalidates confirmation");
  element(elements, "safetyConfirm").checked = true;
  await api.pollState();
  assert.equal(element(elements, "safetyConfirm").checked, false, "fixed-range change invalidates confirmation");
  element(elements, "safetyConfirm").checked = true;
  await api.pollState();

  assert.equal(element(elements, "safetyConfirm").checked, false, "mode change invalidates confirmation");
});

test("disabled pause and stop actions explain the acquisition state they require", () => {
  const { api, elements } = createFrontend();
  api.updateStateDisplay(connectedState({ status: "等待连接" }));

  assert.equal(element(elements, "pause").disabled, true);
  assert.match(element(elements, "pause").title, /开始连续采集后.*暂停/);
  assert.equal(element(elements, "stop").disabled, true);
  assert.match(element(elements, "stop").title, /没有进行中的采集/);

  api.updateStateDisplay(connectedState({ status: "连续采集中" }));
  assert.equal(element(elements, "pause").disabled, false);
  assert.match(element(elements, "pause").title, /暂停当前连续采集/);
  assert.equal(element(elements, "stop").disabled, false);
  assert.match(element(elements, "stop").title, /停止当前采集/);
});

test("single measurements remain stoppable but cannot be paused", () => {
  const { api, elements } = createFrontend();
  api.updateStateDisplay(connectedState({ status: "单次测量中" }));

  assert.equal(element(elements, "pause").disabled, true);
  assert.match(element(elements, "pause").title, /单次测量不支持暂停/);
  assert.equal(element(elements, "stop").disabled, false);
  assert.match(element(elements, "stop").title, /停止当前单次测量/);
});

function mockSuccessfulCommand(context) {
  const requests = [];
  context.fetch = async (url, options = {}) => {
    requests.push({ url, options });
    return { ok: true, json: async () => ({ success: true, message: "已连接" }) };
  };
  return requests;
}

test("starting a Mock connection clears any previous real wiring confirmation", async () => {
  const { api, elements, context } = createFrontend();
  api.app.modes.set("DCV", dcv);
  api.app.selectedMode = "DCV";
  mockSuccessfulCommand(context);
  element(elements, "safetyConfirm").checked = true;

  await api.connectMock();

  assert.equal(element(elements, "safetyConfirm").checked, false);
  assert.equal(Object.hasOwn(api.startPayload(), "safety_confirmed"), false);
});

test("starting a real connection clears stale wiring confirmation and requires a fresh check", async () => {
  const { api, elements, context } = createFrontend();
  api.app.modes.set("DCV", dcv);
  api.app.selectedMode = "DCV";
  api.app.state = { connected: false, simulation: false };
  mockSuccessfulCommand(context);
  element(elements, "safetyConfirm").checked = true;

  await api.connectReal();

  assert.equal(element(elements, "safetyConfirm").checked, false);
  assert.equal(api.startPayload().safety_confirmed, false);
  element(elements, "safetyConfirm").checked = true;
  assert.equal(api.startPayload().safety_confirmed, true);
});

test("successful disconnect clears wiring confirmation", async () => {
  const { api, elements, context } = createFrontend();
  api.bindControls();
  mockSuccessfulCommand(context);
  element(elements, "safetyConfirm").checked = true;

  await element(elements, "disconnect").dispatch("click");

  assert.equal(element(elements, "safetyConfirm").checked, false);
});

test("settings dialog has section navigation, a pinned action footer, and a balanced right stack", () => {
  const html = fs.readFileSync(path.join(ROOT, "src/fluke8845a/web/index.html"), "utf8");
  const css = fs.readFileSync(path.join(ROOT, "src/fluke8845a/web/app.css"), "utf8");
  for (const section of ["sectionSerial", "sectionMeasurement", "sectionExperiment", "sectionAnalysis", "sectionDiagnostics"]) {
    assert.match(html, new RegExp(`href="#${section}"`));
    assert.match(html, new RegExp(`id="${section}"`));
  }
  assert.match(html, /class="dialog-nav"[^>]*aria-label="设置章节"/);
  assert.match(html, /class="settings-side-stack"/);
  assert.match(html, /class="dialog-footer"[\s\S]*id="applySettings"[\s\S]*id="applySpeed"[\s\S]*id="closeSettings"/);
  assert.match(css, /\.dialog-footer\s*\{[^}]*flex:\s*0 0 auto/);
  assert.match(css, /\.settings-scroll\s*\{[^}]*flex:\s*1 1 auto/);
  assert.match(css, /\.settings-grid\s*\{[^}]*grid-template-columns:\s*minmax\(0,1\.1fr\)\s+minmax\(0,\.9fr\)/);
  const ids = [...html.matchAll(/\bid="([^"]+)"/g)].map((match) => match[1]);
  assert.equal(new Set(ids).size, ids.length, "page element IDs remain unique after moving footer actions");
});

test("empty plot gives the next useful action and uses a compact empty layout", () => {
  const { api, elements } = createFrontend();
  assert.equal(typeof api.updateEmptyPlotState, "function");
  api.app.state = { connected: false, status: "等待连接" };
  api.updateEmptyPlotState(false);
  assert.match(element(elements, "emptyDetail").textContent, /连接/);
  assert.equal(element(elements, "emptyConnectMock").classList.contains("hidden"), false);
  assert.equal(element(elements, "emptyStart").classList.contains("hidden"), true);
  assert.equal(element(elements, "measurementArea").classList.contains("chart-empty"), true);

  api.app.state = connectedState({ status: "等待开始" });
  api.updateEmptyPlotState(false);
  assert.equal(element(elements, "emptyConnectMock").classList.contains("hidden"), true);
  assert.equal(element(elements, "emptyStart").classList.contains("hidden"), false);
  api.updateEmptyPlotState(true);
  assert.equal(element(elements, "measurementArea").classList.contains("chart-empty"), false);
});

test("empty plot actions open settings, connect Mock, and start acquisition", async () => {
  const { api, elements, context } = createFrontend();
  api.app.modes.set("DCV", dcv);
  api.app.selectedMode = "DCV";
  api.populateModeOptions(dcv);
  element(elements, "experimentName").value = "短测";
  const requests = mockSuccessfulCommand(context);
  api.bindControls();

  await element(elements, "emptyOpenSettings").dispatch("click");
  assert.equal(element(elements, "settingsDialog").showModalCalled, true);
  await element(elements, "emptyConnectMock").dispatch("click");
  await element(elements, "emptyStart").dispatch("click");
  assert.deepEqual(requests.map(({ options }) => JSON.parse(options.body).action), ["connect_mock", "preflight", "start"]);
});

test("starting acquisition shows the completed preflight checklist before submitting start", async () => {
  const { api, elements, context } = createFrontend();
  api.app.modes.set("DCV", dcv);
  api.app.selectedMode = "DCV";
  api.populateModeOptions(dcv);
  element(elements, "experimentName").value = "预检短测";
  const requests = [];
  context.fetch = async (_url, options) => {
    const action = JSON.parse(options.body).action;
    requests.push(action);
    return { ok: true, json: async () => action === "preflight"
      ? { success: true, message: "预检通过", checks: ["仪器已连接", "配置已回读", "保存目录可写", "MOCK无需实体接线"] }
      : { success: true, message: "连续采集已启动" } };
  };

  await api.startContinuous();

  assert.deepEqual(requests, ["preflight", "start"]);
  assert.match(element(elements, "preflightSummary").textContent, /仪器已连接.*配置已回读.*保存目录可写.*MOCK无需实体接线/);
});

test("failed acquisition preflight prevents start submission", async () => {
  const { api, elements, context } = createFrontend();
  api.app.modes.set("DCV", dcv);
  api.app.selectedMode = "DCV";
  api.populateModeOptions(dcv);
  element(elements, "experimentName").value = "预检失败短测";
  const actions = [];
  context.fetch = async (_url, options) => {
    const action = JSON.parse(options.body).action;
    actions.push(action);
    return { ok: true, json: async () => ({ success: false, message: "保存目录不可写" }) };
  };

  await api.startSingle();

  assert.deepEqual(actions, ["preflight"]);
  assert.match(element(elements, "toast").textContent, /保存目录不可写/);
});

test("duplicate experiment dialog offers create-copy, rename, and cancel without overwriting", async () => {
  const { api, elements } = createFrontend();
  api.bindControls();
  const dialogHtml = fs.readFileSync(path.join(ROOT, "src/fluke8845a/web/index.html"), "utf8");
  assert.match(dialogHtml, /id="duplicateRunDialog"[\s\S]*已有数据不会被覆盖[\s\S]*id="duplicateCreateCopy"[\s\S]*id="duplicateRename"[\s\S]*id="duplicateCancel"/);
  const html = fs.readFileSync(path.join(ROOT, "src/fluke8845a/web/app.js"), "utf8");
  assert.match(html, /choice === "rename"[\s\S]*experimentName"\)\.focus\(\)[\s\S]*experimentName"\)\.select\(\)/);
  for (const [buttonId, expected] of [["duplicateCreateCopy", "create-copy"], ["duplicateRename", "rename"], ["duplicateCancel", "cancel"]]) {
    const choicePromise = api.chooseDuplicateNameAction();
    await element(elements, buttonId).dispatch("click");
    assert.equal(await choicePromise, expected);
  }
});

test("mode options explain only the measurement parameters supported by the active mode", () => {
  const { api, elements } = createFrontend();
  api.app.modes.set("DCV", dcv);
  api.populateModeOptions(dcv);
  assert.match(element(elements, "modeOptions").innerHTML, /id="nplc"/);
  assert.doesNotMatch(element(elements, "modeOptions").innerHTML, /id="bandwidth"/);
  assert.match(element(elements, "modeOptions").innerHTML, /modeParameterHint/);
  assert.match(element(elements, "modeOptions").innerHTML, /id="autozeroSpeedHint"[\s\S]*增加测量时间/);

  const acv = { ...dcv, value: "ACV", label: "交流电压", nplc: [], bandwidths: [3, 20] };
  api.app.modes.set("ACV", acv);
  api.populateModeOptions(acv);
  assert.doesNotMatch(element(elements, "modeOptions").innerHTML, /id="nplc"/);
  assert.match(element(elements, "modeOptions").innerHTML, /id="bandwidth"/);
  assert.match(element(elements, "modeOptions").innerHTML, /交流响应带宽/);
  assert.match(element(elements, "modeOptions").innerHTML, /交流响应带宽会影响稳定时间和噪声/);
});

test("mode setup shows terminal diagram and current-range restrictions", () => {
  const { api, elements } = createFrontend();
  const dci = { ...dcv, value: "DCI", label: "直流电流", quantity: "current", unit: "A", ranges: [.01, .1, .4, 1, 3, 10], nplc: [1], bandwidths: [], analog_filter: true };
  api.populateModeOptions(dci);
  assert.match(element(elements, "terminalDiagram").innerHTML, /mA \/ 400 mA/);
  assert.match(element(elements, "terminalDiagram").innerHTML, /10 A/);
  assert.match(element(elements, "rangeLimitHint").textContent, /OutG SW ≥ 2\.0/);
  assert.match(element(elements, "modeOptions").innerHTML, /NPLC 越高.*自动零点/);
});

test("current-range guidance follows selected range, cached firmware source, and terminal readback", () => {
  const { api, elements, context } = createFrontend();
  let requests = 0;
  context.fetch = async () => { requests++; return { ok: true, json: async () => ({}) }; };
  const dci = { ...dcv, value: "DCI", label: "直流电流", quantity: "current", unit: "A", ranges: [.01, .1, .4, 1, 3, 10] };
  api.app.modes.set("DCI", dci);
  api.populateModeOptions(dci);
  element(elements, "autorange").checked = false;
  element(elements, "range").value = "0.4";
  api.updateStateDisplay(connectedState({ simulation: false, device: {
    serial_number: "9383035", range_firmware: "1.9.0.0", range_firmware_source: "*IDN?",
  }, configuration: { input_terminals: "FRONT" }, active_settings: {
    mode: "DCI", autorange: false, range_si: .4,
  } }));
  const oldFirmware = element(elements, "rangeLimitHint").textContent;
  assert.match(oldFirmware, /400 mA.*1\.9\.0\.0.*\*IDN\?.*低于.*2\.0/);
  assert.match(oldFirmware, /FRONT.*回读/);
  assert.match(oldFirmware, /下一步.*核对.*仪表面板.*OutG SW/);

  element(elements, "range").value = "1";
  api.updateStateDisplay(connectedState({ simulation: false, device: {
    serial_number: "9383035", range_firmware: "2.10.882.24", range_firmware_source: "面板核对的 OutG SW",
  }, configuration: { input_terminals: "REAR" }, active_settings: {
    mode: "DCI", autorange: false, range_si: 1,
  } }));
  const rearTerminal = element(elements, "rangeLimitHint").textContent;
  assert.match(rearTerminal, /1 A.*REAR.*前面板/);
  assert.match(rearTerminal, /面板核对的 OutG SW.*2\.10\.882\.24/);
  assert.match(rearTerminal, /下一步.*面板.*接线/);

  element(elements, "autorange").checked = true;
  api.updateStateDisplay(connectedState({ simulation: false, device: {
    serial_number: "9383035", range_firmware: null, range_firmware_source: "未知",
  }, configuration: {}, active_settings: { mode: "DCI", autorange: true } }));
  const unknownReadback = element(elements, "rangeLimitHint").textContent;
  assert.match(unknownReadback, /端子.*未回读|未回读.*端子/);
  assert.match(unknownReadback, /版本.*未核实|未核实.*版本/);
  assert.doesNotMatch(unknownReadback, /已确认 FRONT|已确认支持/);
  assert.equal(requests, 0, "guidance is rendered from cached state without a device request");
});

test("NPLC and bandwidth can be prepared before acquisition and tuned during supported live acquisition", () => {
  const { api, elements } = createFrontend();
  api.app.modes.set("DCV", dcv);
  api.app.selectedMode = "DCV";
  api.populateModeOptions(dcv);
  api.updateStateDisplay(connectedState({ status: "等待开始" }));
  assert.equal(element(elements, "nplc").disabled, false);
  assert.equal(element(elements, "applySettings").disabled, false);
  assert.equal(element(elements, "applySpeed").disabled, true);
  assert.match(element(elements, "settingsApplyHint").textContent, /采集前/);

  api.updateStateDisplay(connectedState({ status: "采集中" }));
  assert.equal(element(elements, "nplc").disabled, false);
  assert.equal(element(elements, "applySettings").disabled, true);
  assert.equal(element(elements, "applySpeed").disabled, false);
  assert.match(element(elements, "settingsApplyHint").textContent, /采集中/);

  api.updateStateDisplay(connectedState({ status: "采集中", control_mode: "local", simulation: false }));
  assert.equal(element(elements, "nplc").disabled, true);
  assert.equal(element(elements, "applySpeed").disabled, true);

  const acv = { ...dcv, value: "ACV", label: "交流电压", nplc: [], bandwidths: [3, 20] };
  api.app.modes.set("ACV", acv);
  api.app.selectedMode = "ACV";
  api.populateModeOptions(acv);
  api.updateStateDisplay(connectedState({ status: "等待开始", active_settings: { mode: "ACV" } }));
  assert.equal(element(elements, "bandwidth").disabled, false);
  assert.equal(element(elements, "applySpeed").disabled, true);
  api.updateStateDisplay(connectedState({ status: "采集中", active_settings: { mode: "ACV" } }));
  assert.equal(element(elements, "bandwidth").disabled, false);
  assert.equal(element(elements, "applySpeed").disabled, false);
});

test("Computer and Local help is visible and Local status does not claim a device readback", () => {
  const html = fs.readFileSync(path.join(ROOT, "src/fluke8845a/web/index.html"), "utf8");
  assert.match(html, /id="controlModeHelp"/);
  assert.match(html, /id="controlModeStatus"/);
  const { api, elements } = createFrontend();
  api.app.modes.set("DCV", dcv);
  api.updateStateDisplay(connectedState({ simulation: false, control_mode: "local" }));

  assert.match(element(elements, "controlModeHelp").textContent, /Computer.*Local.*采集.*ZERO.*诊断/);
  assert.match(element(elements, "controlModeStatus").textContent, /Local.*SCPI.*(无法|不能).*回读.*面板/);
});

test("Local mode disables diagnostics with a concrete reason and Computer re-enables it", () => {
  const { api, elements } = createFrontend();
  api.app.modes.set("DCV", dcv);
  api.updateStateDisplay(connectedState({ simulation: false, control_mode: "local" }));
  assert.equal(element(elements, "diagnose").disabled, true);
  assert.match(element(elements, "diagnose").title, /Local.*诊断.*Computer/);

  api.updateStateDisplay(connectedState({ simulation: false, control_mode: "computer" }));
  assert.equal(element(elements, "diagnose").disabled, false);
  assert.match(element(elements, "controlModeStatus").textContent, /Computer.*(回读|读取)/);
});

test("settings labels remain readable and secondary text meets contrast target", () => {
  const css = fs.readFileSync(path.join(ROOT, "src/fluke8845a/web/app.css"), "utf8");
  const fontRule = css.match(/\.serial-grid label, \.mode-options label\s*\{([^}]*)\}/);
  assert.ok(fontRule, "form label rule exists");
  assert.match(fontRule[1], /font-size:\s*12px/);
  const buttonRule = css.match(/\.button\s*\{([^}]*)\}/);
  assert.ok(buttonRule, "primary and secondary buttons share a readable base size");
  assert.match(buttonRule[1], /font-size:\s*12px/);
  const statCountRule = css.match(/\.stat-card \.stat-count\s*\{([^}]*)\}/);
  assert.ok(statCountRule, "statistic sample counts have a readable rule");
  assert.match(statCountRule[1], /font-size:\s*12px/);
  const hintRule = css.match(/\.field-hint, \.rail-footnote\s*\{([^}]*)\}/);
  assert.ok(hintRule, "settings hint rule exists");
  const colorMatch = hintRule[1].match(/color:\s*(#[\da-fA-F]{6})/);
  assert.ok(colorMatch, "settings hint has an explicit color");
  const luminance = (hex) => {
    const channels = hex.slice(1).match(/../g).map((value) => parseInt(value, 16) / 255).map((value) => value <= 0.04045 ? value / 12.92 : ((value + 0.055) / 1.055) ** 2.4);
    return channels[0] * 0.2126 + channels[1] * 0.7152 + channels[2] * 0.0722;
  };
  const contrast = (foreground, background) => {
    const values = [luminance(foreground), luminance(background)].sort((a, b) => b - a);
    return (values[0] + 0.05) / (values[1] + 0.05);
  };
  assert.ok(contrast(colorMatch[1], "#f7f6ef") >= 4.5, `${colorMatch[1]} contrast must be at least 4.5:1`);
  const sectionKickerRule = css.match(/\.section-kicker\s*\{([^}]*)\}/);
  const sectionKickerColor = sectionKickerRule?.[1].match(/color:\s*(#[\da-fA-F]{6})/);
  assert.ok(sectionKickerColor);
  assert.ok(contrast(sectionKickerColor[1], "#f1efe6") >= 4.5, "section labels must meet 4.5:1 on the chart and dialog background");
  const stageRule = css.match(/\.connection-stages li\s*\{([^}]*)\}/);
  const stageColor = stageRule?.[1].match(/color:\s*(#[\da-fA-F]{6})/);
  assert.ok(stageColor);
  assert.ok(contrast(stageColor[1], "#f4f6f2") >= 4.5, "connection stage labels must meet 4.5:1 on their badge background");
  const eventValueRule = css.match(/\.event-list \.event-value\s*\{([^}]*)\}/);
  const eventValueColor = eventValueRule?.[1].match(/color:\s*(#[\da-fA-F]{6})/);
  assert.ok(eventValueColor);
  assert.ok(contrast(eventValueColor[1], "#fffefa") >= 4.5, "event-value labels must meet 4.5:1 on the event list background");
  const darkTextRule = css.match(/\.raw-value\s*\{([^}]*)\}/);
  const darkTextColor = darkTextRule?.[1].match(/color:\s*(#[\da-fA-F]{6})/);
  assert.ok(darkTextColor);
  assert.ok(contrast(darkTextColor[1], "#1d262a") >= 4.5, "dark readout auxiliary text contrast must be at least 4.5:1");
  const placeholderRule = css.match(/input::placeholder\s*\{([^}]*)\}/);
  const placeholderColor = placeholderRule?.[1].match(/color:\s*(#[\da-fA-F]{6})/);
  assert.ok(placeholderColor);
  assert.ok(contrast(placeholderColor[1], "#fffefa") >= 4.5, "input hint contrast must be at least 4.5:1");
});

test("release resource validator accepts current source and rejects stale sampling copy", () => {
  const html = fs.readFileSync(path.join(ROOT, "src/fluke8845a/web/index.html"), "utf8");
  const javascript = fs.readFileSync(path.join(ROOT, "src/fluke8845a/web/app.js"), "utf8");
  assert.match(html, /id="serviceStatus"/);
  assert.match(html, /id="safetyControls"/);
  assert.match(html, /id="mockSafetyNote"/);
  const validator = "import json,sys; from scripts.package_windows_release import validate_packaged_web_resources; data=json.load(sys.stdin); validate_packaged_web_resources(data['html'], data['javascript'])";
  const validate = (page) => spawnSync("python", ["-X", "utf8", "-c", validator], {
    cwd: ROOT, input: JSON.stringify({ html: page, javascript }), encoding: "utf8",
  });
  const current = validate(html);
  assert.equal(current.status, 0, current.stderr);
  const stale = validate(html.replace("实测采样间隔", "采样频率"));
  assert.notEqual(stale.status, 0);
  assert.match(stale.stderr, /实测采样间隔/);
});

test("the top bar identifies Mock data and keeps the selected scenario visible after state refresh", () => {
  const { api, elements } = createFrontend();
  api.app.modes.set("DCV", dcv);
  api.app.selectedMode = "DCV";
  api.updateStateDisplay(connectedState({ mock_scenario: "长期漂移" }));

  assert.equal(element(elements, "mockScenario").value, "长期漂移");
  assert.equal(element(elements, "scenarioBadge").textContent, "模拟 · 长期漂移");
  assert.equal(element(elements, "scenarioBadge").classList.contains("hidden"), false);

  api.updateStateDisplay(connectedState({ simulation: false, mock_scenario: null, connection_text: "真实 8845A 已连接" }));
  assert.equal(element(elements, "scenarioBadge").classList.contains("hidden"), true);
  assert.match(element(elements, "connectionBadge").querySelector("span").textContent, /8845A/);
});

test("connection phase text is taken from instrument-worker progress and remains separate from service status", () => {
  const { api, elements } = createFrontend();
  api.app.modes.set("DCV", dcv);
  api.updateStateDisplay(connectedState({
    connection_progress: "已识别 Fluke 8845A；正在读取当前测量功能和输入端子…",
  }));

  assert.match(element(elements, "connectionProgress").textContent, /已识别.*正在读取/);
  assert.equal(element(elements, "serviceStatus").textContent, "");
});

test("state polling clears physical wiring confirmation when the instrument disconnects", async () => {
  const { api, elements, context } = createFrontend();
  api.app.state = connectedState({ simulation: false });
  element(elements, "safetyConfirm").checked = true;
  context.fetch = async () => ({ ok: true, json: async () => ({ connected: false, simulation: false, status: "未连接", stats: {}, samples: [], logs: [], events: [] }) });

  await api.pollState();

  assert.equal(element(elements, "safetyConfirm").checked, false);
});

test("measurement telemetry distinguishes attempts, valid and abnormal counts from measured intervals", () => {
  const { api, elements } = createFrontend();
  api.app.modes.set("DCV", dcv);
  api.app.selectedMode = "DCV";
  api.updateStateDisplay(connectedState({
    actual_hz: 2,
    stats: { attempt_count: 7, valid_count: 5, abnormal_count: 2,
      interval_median_s: 0.48, interval_min_s: 0.4, interval_max_s: 0.6, interval_range_jitter_s: 0.2 },
  }));

  assert.equal(element(elements, "attemptCount").textContent, "7");
  assert.equal(element(elements, "validCount").textContent, "5");
  assert.equal(element(elements, "abnormalCount").textContent, "2");
  assert.equal(element(elements, "intervalMedian").textContent, "480 ms");
  assert.equal(element(elements, "intervalMin").textContent, "400 ms");
  assert.equal(element(elements, "intervalMax").textContent, "600 ms");
  assert.equal(element(elements, "intervalJitter").textContent, "200 ms");
  assert.equal(element(elements, "actualRate").textContent, "500 ms");
});

test("one valid reading says it is waiting for the second sample without changing actual_hz", () => {
  const { api, elements } = createFrontend();
  api.app.modes.set("DCV", dcv);
  api.updateStateDisplay(connectedState({ actual_hz: 0, stats: {
    attempt_count: 1, valid_count: 1, abnormal_count: 0,
    interval_median_s: null, interval_min_s: null, interval_max_s: null, interval_range_jitter_s: null,
  } }));

  assert.equal(element(elements, "actualRate").textContent, "等待第二个有效读数");
  assert.equal(element(elements, "intervalMedian").textContent, "等待第二个有效读数");
});

test("port scan has persistent no-port guidance while keeping the manual COM field available", async () => {
  const { api, elements, context } = createFrontend();
  context.fetch = async () => ({ ok: true, json: async () => ({ ports: [] }) });
  await api.scanPorts();

  assert.match(element(elements, "portScanStatus").textContent, /未发现.*手动输入/);
  assert.equal(element(elements, "port").disabled, false);
  assert.equal(element(elements, "scanPorts").disabled, false);
});

test("command requests show progress and suppress duplicate submissions", async () => {
  const { api, elements, context } = createFrontend();
  let resolveResponse;
  let callCount = 0;
  context.fetch = () => {
    callCount += 1;
    return new Promise((resolve) => { resolveResponse = resolve; });
  };
  const first = api.command("diagnose");
  assert.equal(element(elements, "commandStatus").classList.contains("hidden"), false);
  assert.match(element(elements, "commandStatus").textContent, /诊断.*进行中/);
  assert.equal(element(elements, "diagnose").disabled, true);
  const second = await api.command("diagnose");
  assert.equal(second.success, false);
  assert.equal(callCount, 1);
  resolveResponse({ json: async () => ({ success: true, message: "诊断完成" }) });
  await first;
  assert.equal(element(elements, "diagnose").disabled, false);
  assert.equal(element(elements, "commandStatus").classList.contains("hidden"), true);
});

test("long Web commands show the concrete work and expected result without inventing an ETA", async () => {
  const { api, elements, context } = createFrontend();
  let finish;
  context.fetch = () => new Promise((resolve) => { finish = resolve; });

  const pending = api.command("stop");

  assert.match(element(elements, "commandStatus").textContent, /等待采集线程完成写入并关闭 CSV/);
  assert.match(element(elements, "commandStatus").textContent, /样本数和保存目录/);
  assert.doesNotMatch(element(elements, "commandStatus").textContent, /预计.*秒|ETA/i);
  finish({ json: async () => ({ success: true, message: "采集已停止" }) });
  await pending;
});

test("critical acquisition errors stay visible independently of the latest reading", () => {
  const { api, elements } = createFrontend();
  api.app.modes.set("DCV", dcv);
  api.updateStateDisplay(connectedState({
    latest: { value_si: 1.23, unit_si: "V", raw_reading: "1.23", mode: "DCV", status: "ok" },
    last_error: "串口响应超时",
  }));

  assert.equal(element(elements, "criticalError").classList.contains("hidden"), false);
  assert.match(element(elements, "criticalError").textContent, /串口响应超时/);
});

test("acquisition feedback distinguishes retryable READ timeouts from fatal SCPI response timeouts", () => {
  const { api, elements } = createFrontend();
  api.app.modes.set("DCV", dcv);
  api.app.latestInvalidPoint = { id: 5, status: "timeout", error: "READ? 响应超时" };
  api.updateStateDisplay(connectedState({ status: "采集中", experiment_dir: "D:/runs/read-timeout", last_experiment_dir: "D:/runs/read-timeout", last_error: "READ? 响应超时", stats: {
    attempt_count: 5, valid_count: 3, abnormal_count: 2,
  } }));
  const readTimeout = element(elements, "acquisitionFeedback").textContent;
  assert.match(readTimeout, /普通 READ.*下一采样周期.*重试/);
  assert.match(readTimeout, /连续\s*3\s*次/);
  assert.match(readTimeout, /尝试\s*5.*有效\s*3.*异常\s*2/);

  api.app.latestInvalidPoint = null;
  api.updateStateDisplay(connectedState({
    connected: false, simulation: false,
    status: "SCPI响应超时，已停止采集并关闭连接；请重新连接",
    experiment_dir: "", last_experiment_dir: "D:/runs/scpi-timeout",
    last_error: "SCPI响应超时，连接已关闭",
    stats: { attempt_count: 7, valid_count: 4, abnormal_count: 3 },
  }));
  const scpiTimeout = element(elements, "acquisitionFeedback").textContent;
  assert.match(scpiTimeout, /SCPI 响应超时.*立即停止.*关闭/);
  assert.match(scpiTimeout, /已保存尝试\s*7.*有效\s*4.*异常\s*3/);
  assert.match(scpiTimeout, /COM.*波特率.*结束符.*Computer/);
  assert.match(scpiTimeout, /重新连接.*新建实验/);
});

test("completed READ timeout does not recommend reconnect after acquisition has recovered", () => {
  const { api, elements } = createFrontend();
  api.app.modes.set("DCV", dcv);
  api.app.latestInvalidPoint = { id: 5, status: "timeout", error: "READ? 响应超时" };
  api.updateStateDisplay(connectedState({
    connected: true, simulation: false, status: "实验已结束", experiment_dir: "",
    last_experiment_dir: "D:/runs/recovered-read", last_error: "READ? 响应超时",
    stats: { attempt_count: 6, valid_count: 5, abnormal_count: 1 },
  }));

  const feedback = element(elements, "acquisitionFeedback").textContent;
  assert.match(feedback, /实验已结束/);
  assert.doesNotMatch(feedback, /通讯恢复后重新连接/);
});

test("non-communication acquisition errors do not trigger reconnect instructions", () => {
  const { api, elements } = createFrontend();
  api.app.modes.set("DCV", dcv);
  api.updateStateDisplay(connectedState({
    connected: true, simulation: false, status: "实验已结束", experiment_dir: "",
    last_experiment_dir: "D:/runs/parameter-error", last_error: "参数校验失败",
    stats: { attempt_count: 2, valid_count: 1, abnormal_count: 1 },
  }));

  const feedback = element(elements, "acquisitionFeedback").textContent;
  assert.match(feedback, /实验已结束/);
  assert.doesNotMatch(feedback, /通讯恢复后重新连接/);
});

test("safe manual disconnect after a completed experiment is not reported as a connection failure", () => {
  const { api, elements } = createFrontend();
  api.app.modes.set("DCV", dcv);
  api.updateStateDisplay(connectedState({
    connected: false, simulation: false, status: "实验已结束", connection_text: "已安全退出",
    experiment_dir: "", last_experiment_dir: "D:/runs/safely-disconnected",
    last_error: "READ? 响应超时（旧记录）",
    stats: { attempt_count: 6, valid_count: 5, abnormal_count: 1 },
  }));

  const feedback = element(elements, "acquisitionFeedback").textContent;
  assert.match(feedback, /实验已结束/);
  assert.match(feedback, /异常详情见运行日志/);
  assert.doesNotMatch(feedback, /连接已中断|不会自动重连|重新连接/);
});

test("connection loss feedback says acquisition stopped and will not reconnect automatically", () => {
  const { api, elements } = createFrontend();
  api.app.modes.set("DCV", dcv);
  api.updateStateDisplay(connectedState({
    connected: false, simulation: false, status: "串口连接中断，采集已停止",
    experiment_dir: "", last_experiment_dir: "D:/runs/disconnected",
    last_error: "串口连接已关闭", stats: { attempt_count: 4, valid_count: 3, abnormal_count: 1 },
  }));

  const feedback = element(elements, "acquisitionFeedback").textContent;
  assert.match(feedback, /采集已停止/);
  assert.match(feedback, /不会自动重连/);
  assert.match(feedback, /已保存尝试\s*4.*有效\s*3.*异常\s*1/);
});

test("latest successful sample prevents stale last_error from being treated as a current timeout", () => {
  const { api, elements } = createFrontend();
  api.app.modes.set("DCV", dcv);
  api.app.latestInvalidPoint = { id: 5, status: "timeout", error: "READ? 响应超时" };
  api.updateStateDisplay(connectedState({
    connected: true, simulation: false, status: "采集中", experiment_dir: "D:/runs/recovered",
    last_experiment_dir: "D:/runs/recovered", last_error: "SCPI响应超时（旧记录）",
    latest: { sample_index: 6, status: "ok", value_si: 1.2, unit_si: "V", mode: "DCV", raw_reading: "1.2" },
    stats: { attempt_count: 6, valid_count: 5, abnormal_count: 1 },
  }));

  const feedback = element(elements, "acquisitionFeedback").textContent;
  assert.doesNotMatch(feedback, /SCPI 响应超时，立即停止/);
  assert.doesNotMatch(feedback, /普通 READ 超时会在下一采样周期重试/);
});

test("critical error banner clears when a newer successful sample recovers from the timeout", () => {
  const { api, elements } = createFrontend();
  api.app.modes.set("DCV", dcv);
  api.app.latestInvalidPoint = { id: 5, status: "timeout", error: "READ? 响应超时" };
  api.updateStateDisplay(connectedState({
    connected: true, simulation: false, status: "采集中", experiment_dir: "D:/runs/recovered",
    last_experiment_dir: "D:/runs/recovered", last_error: "READ? 响应超时",
    latest: { sample_index: 6, status: "ok", value_si: 1.2, unit_si: "V", mode: "DCV", raw_reading: "1.2" },
    stats: { attempt_count: 6, valid_count: 5, abnormal_count: 1 },
  }));

  assert.equal(element(elements, "criticalError").classList.contains("hidden"), true);
  assert.doesNotMatch(element(elements, "criticalError").textContent, /READ\? 响应超时/);
});

test("long acquisition recovery guidance wraps and remains visible in Chromium", (t) => {
  const candidates = [
    process.env.CHROME_BIN,
    process.env.EDGE_BIN,
    "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe",
    "C:/Program Files/Google/Chrome/Application/chrome.exe",
  ].filter(Boolean);
  const browser = candidates.find((candidate) => fs.existsSync(candidate));
  if (!browser) return t.skip("Chromium is not installed; recovery text wrapping cannot be measured here");

  const css = fs.readFileSync(path.join(ROOT, "src/fluke8845a/web/app.css"), "utf8");
  const childHtml = `<!doctype html><meta charset="utf-8"><style>${css}</style><section class="action-bar"><span id="acquisitionFeedback" class="acquisition-feedback"></span></section>`;
  const probe = `<!doctype html><meta charset="utf-8"><pre id="result">pending</pre><script>
    const source = ${JSON.stringify(childHtml)};
    const frame = document.createElement("iframe");
    frame.style.cssText = "display:block;border:0;width:280px;height:500px";
    frame.srcdoc = source;
    document.body.appendChild(frame);
    frame.addEventListener("load", () => {
      const doc = frame.contentDocument;
      const feedback = doc.getElementById("acquisitionFeedback");
      feedback.textContent = "串口连接中断后的安全恢复说明：检查 COM 端口、波特率、结束符和仪表 Computer 模式；手动重新连接并新建实验。".repeat(12);
      const style = getComputedStyle(feedback);
      document.getElementById("result").textContent = JSON.stringify({
        whiteSpace: style.whiteSpace, overflow: style.overflow, textOverflow: style.textOverflow,
        height: feedback.getBoundingClientRect().height, lineHeight: style.lineHeight,
        scrollHeight: feedback.scrollHeight, clientHeight: feedback.clientHeight,
      });
    });
  </script>`;
  const tempHtml = path.join(require("node:os").tmpdir(), `8845a-feedback-${process.pid}-${Date.now()}.html`);
  fs.writeFileSync(tempHtml, probe, "utf8");
  try {
    const result = spawnSync(browser, ["--headless", "--disable-gpu", "--no-first-run", "--no-default-browser-check", "--no-sandbox", "--dump-dom", tempHtml], { encoding: "utf8", timeout: 7000, windowsHide: true });
    assert.equal(result.error, undefined, result.error?.message);
    assert.equal(result.status, 0, result.stderr);
    const match = result.stdout.match(/<pre id="result">(\{[\s\S]*?\})<\/pre>/);
    assert.ok(match, "Chromium returns recovery-message layout metrics");
    const metrics = JSON.parse(match[1]);
    assert.equal(metrics.whiteSpace, "normal");
    assert.notEqual(metrics.overflow, "hidden");
    assert.notEqual(metrics.textOverflow, "ellipsis");
    assert.ok(metrics.height > (parseFloat(metrics.lineHeight) || 14) * 1.5);
    assert.equal(metrics.scrollHeight, metrics.clientHeight);
  } finally {
    fs.rmSync(tempHtml, { force: true });
  }
});

test("critical acquisition error opens the settings dialog at filtered error logs", async () => {
  const { api, elements } = createFrontend();
  api.bindControls();
  await element(elements, "criticalError").dispatch("click");
  assert.equal(element(elements, "settingsDialog").showModalCalled, true);
  assert.equal(element(elements, "sectionDiagnostics").open, true);
  assert.equal(element(elements, "logFilter").value, "error");
});

test("recent invalid sample details stay visible and open the log view", async () => {
  const { api, elements } = createFrontend();
  api.app.modes.set("DCV", dcv);
  api.bindControls();
  api.appendSampleRows([{
    sample_index: 12, elapsed_s: 4, value_si: null, status: "timeout",
    raw_reading: "", error: "SCPI 响应超时", mode: "DCV", unit_si: "V",
  }]);
  api.updateStateDisplay(connectedState({ latest: {
    sample_index: 11, value_si: 1, unit_si: "V", raw_reading: "1.0", mode: "DCV", status: "ok",
  } }));

  assert.match(element(elements, "criticalError").textContent, /最近无效样本 #12.*timeout.*SCPI 响应超时.*日志/);
  assert.equal(element(elements, "criticalError").classList.contains("hidden"), false);
  await element(elements, "criticalError").dispatch("click");
  assert.equal(element(elements, "settingsDialog").showModalCalled, true);
  assert.equal(element(elements, "sectionDiagnostics").open, true);
});

test("post-ZERO configuration readback remains visible and is not labeled a zero result", () => {
  const { api, elements } = createFrontend();
  api.app.modes.set("DCV", dcv);
  const readback = "ZERO:AUTO ONCE 命令已发送；配置回读：DCV · 1 NPLC；不代表零点测量结果";
  api.updateStateDisplay(connectedState({ zero_result: readback }));
  assert.equal(element(elements, "zeroResult").textContent, readback);
  assert.equal(element(elements, "zeroResult").classList.contains("hidden"), false);
});

test("the event action is disabled until an experiment is active and explains why", () => {
  const { api, elements } = createFrontend();
  api.app.modes.set("DCV", dcv);
  api.updateStateDisplay(connectedState({ experiment_dir: "", status: "等待开始" }));
  assert.equal(element(elements, "markEvent").disabled, true);
  assert.match(element(elements, "markEvent").title, /先启动实验/);

  api.updateStateDisplay(connectedState({ experiment_dir: "D:/runs/run-1", status: "采集中" }));
  assert.equal(element(elements, "markEvent").disabled, false);
});

test("event feedback is shown and text clears only after the event is saved", async () => {
  const { api, elements, context } = createFrontend();
  api.bindControls();
  api.updateStateDisplay(connectedState({ experiment_dir: "D:/runs/run-1", status: "采集中" }));
  const input = element(elements, "eventText");
  input.value = "负载切换";
  context.fetch = async () => ({ ok: true, json: async () => ({ success: true, message: "事件已记录 · 10:15:00 · #12 · 1.2 V" }) });

  await element(elements, "markEvent").dispatch("click");

  assert.match(element(elements, "toast").textContent, /事件已记录.*#12.*1\.2 V/);
  assert.equal(input.value, "");

  input.value = "恢复完成";
  context.fetch = async () => ({ ok: true, json: async () => ({ success: false, message: "保存失败：磁盘不可写" }) });
  await element(elements, "markEvent").dispatch("click");

  assert.match(element(elements, "toast").textContent, /保存失败：磁盘不可写/);
  assert.equal(input.value, "恢复完成");
});

test("chart gesture help is expandable and the live hint stays concise", () => {
  const html = fs.readFileSync(path.join(ROOT, "src/fluke8845a/web/index.html"), "utf8");
  assert.match(html, /<details id="plotHelp"[^>]*>/);
  assert.match(html, /<summary>[^<]*触屏[^<]*<\/summary>/);
  assert.match(html, /触屏单指平移、双指缩放/);
  assert.match(html, /id="pointCursor"[^>]*>[^<]*悬停读点/);
  assert.match(html, /id="chartGestureHint"[^>]*>[^<]*滚轮/);
  assert.match(html, /id="scenarioBadge"/);
  assert.match(html, /id="acquisitionFeedback"/);
  assert.match(html, /class="pause-slot"[\s\S]*id="pause"[\s\S]*id="resume"/);
});

test("the chart draws a horizontal reference line for the active baseline", () => {
  const { api, elements } = createFrontend({ stubChart: false });
  api.app.modes.set("DCV", dcv);
  api.app.points = [
    { id: 1, x: 0, y: 1, status: "ok", unit: "V", raw: "1.000" },
    { id: 2, x: 1, y: 2, status: "ok", unit: "V", raw: "2.000" },
  ];
  api.app.baseline = 1.5;
  element(elements, "baselineValue").value = "1.5";
  element(elements, "autoFollow").checked = false;
  api.drawChart();

  const strokes = element(elements, "plot").canvasContext.strokes;
  assert.ok(strokes.some((stroke) => stroke.color === "#c7842f" && stroke.dash.length > 0 && stroke.path.some(([kind], i, path) => kind === "M" && path[i + 1]?.[0] === "L")));
});

test("failed readings leave a visible gap instead of joining surrounding samples", () => {
  const { api, elements } = createFrontend({ stubChart: false });
  api.app.modes.set("DCV", dcv);
  api.app.points = [
    { id: 1, x: 0, y: 1, status: "ok", unit: "V" },
    { id: 2, x: 1, y: null, status: "timeout", unit: "V" },
    { id: 3, x: 2, y: 2, status: "ok", unit: "V" },
  ];
  element(elements, "autoFollow").checked = false;
  api.drawChart();

  const curve = element(elements, "plot").canvasContext.strokes.find((stroke) => stroke.color === "#138b92");
  assert.equal(curve.path.filter(([kind]) => kind === "M").length, 2);
});

test("history import rejects mixed modes and files with no valid readings", async () => {
  const { api, elements } = createFrontend();
  api.app.modes.set("DCV", dcv);
  api.app.modes.set("DCI", { ...dcv, value: "DCI", label: "直流电流", unit: "A" });
  const file = (text) => ({ name: "measurements.csv", size: text.length, text: async () => text });

  await api.loadHistory(file("elapsed_s,value_si,unit_si,mode,status\n0,1,V,DCV,ok\n1,2,A,DCI,ok\n"));
  assert.match(element(elements, "toast").textContent, /混合.*(模式|单位)|模式.*混合/);
  assert.equal(api.app.imported, null);

  await api.loadHistory(file("elapsed_s,value_si,unit_si,mode,status\n0,,V,DCV,timeout\n1,,V,DCV,timeout\n"));
  assert.match(element(elements, "toast").textContent, /没有有效读数/);
  assert.equal(api.app.imported, null);
});

test("history import hands the File to a background Worker without reading CSV on the UI thread", async () => {
  const { api, elements, context } = createFrontend();
  api.app.modes.set("DCV", dcv);
  const posted = [];
  class DeferredWorker {
    constructor(url) { this.url = url; DeferredWorker.instances.push(this); }
    postMessage(message) { posted.push(message); }
    terminate() { this.terminated = true; }
  }
  DeferredWorker.instances = [];
  context.Worker = DeferredWorker;
  const file = { name: "history.csv", size: 64, text: async () => { throw new Error("UI thread read file text"); } };

  const pending = api.loadHistory(file);

  assert.equal(posted.length, 1);
  assert.equal(posted[0].file, file);
  assert.match(String(DeferredWorker.instances[0].url), /history_worker\.js/);
  assert.match(element(elements, "toast").textContent, /后台解析/);
  DeferredWorker.instances[0].onmessage({ data: { ok: true, rows: [{ x: 0, y: 1, status: "ok", mode: "DCV", unit: "V", raw: "1", id: 1 }], modeValue: "DCV", unit: "V", experimentId: "run-1" } });
  await pending;

  assert.equal(api.app.imported.length, 1);
  assert.equal(api.app.importedExperimentId, "run-1");
  assert.equal(element(elements, "historyFile").value, "");
});

test("replacing a pending history import terminates it and ignores late worker results", async () => {
  const { api, elements, context } = createFrontend();
  api.app.modes.set("DCV", dcv);
  class DeferredWorker {
    constructor(url) { this.url = url; DeferredWorker.instances.push(this); }
    postMessage(message) { this.message = message; }
    terminate() { this.terminated = true; }
  }
  DeferredWorker.instances = [];
  context.Worker = DeferredWorker;
  const first = api.loadHistory({ name: "first.csv", size: 20 });
  const second = api.loadHistory({ name: "second.csv", size: 20 });
  const [firstWorker, secondWorker] = DeferredWorker.instances;

  await first;
  assert.equal(firstWorker.terminated, true);
  secondWorker.onmessage({ data: { ok: true, rows: [{ x: 0, y: 2, status: "ok", mode: "DCV", unit: "V", raw: "2", id: 2 }], modeValue: "DCV", unit: "V", experimentId: "second-run" } });
  await second;
  firstWorker.onmessage({ data: { ok: true, rows: [{ x: 0, y: 1, status: "ok", mode: "DCV", unit: "V", raw: "1", id: 1 }], modeValue: "DCV", unit: "V", experimentId: "first-run" } });

  assert.equal(api.app.importedFileName, "second.csv");
  assert.equal(api.app.importedExperimentId, "second-run");
  assert.equal(api.app.imported[0].y, 2);
  assert.equal(element(elements, "historyFile").value, "");
});

test("selecting an oversized history file cancels an older pending import", async () => {
  const { api, elements, context } = createFrontend();
  api.app.modes.set("DCV", dcv);
  class DeferredWorker {
    constructor(url) { this.url = url; DeferredWorker.instances.push(this); }
    postMessage(message) { this.message = message; }
    terminate() { this.terminated = true; }
  }
  DeferredWorker.instances = [];
  context.Worker = DeferredWorker;
  const first = api.loadHistory({ name: "first.csv", size: 20 });
  await api.loadHistory({ name: "too-large.csv", size: 51 * 1024 * 1024 });
  const firstWorker = DeferredWorker.instances[0];
  await first;

  assert.equal(firstWorker.terminated, true);
  assert.match(element(elements, "toast").textContent, /超过 50 MB/);
  firstWorker.onmessage({ data: { ok: true, rows: [{ x: 0, y: 1, status: "ok", mode: "DCV", unit: "V", raw: "1", id: 1 }], modeValue: "DCV", unit: "V", experimentId: "stale-run" } });
  assert.equal(api.app.imported, null);
});

test("history import explains its required columns and rejects unsupported or mismatched mode units", async () => {
  const { api, elements } = createFrontend();
  api.app.modes.set("DCV", dcv);
  const file = (text) => ({ name: "measurements.csv", size: text.length, text: async () => text });

  await api.loadHistory(file("value_si,status\n1,ok\n"));
  assert.match(element(elements, "toast").textContent, /缺少列.*elapsed_s.*可接受格式.*measurements\.csv/);

  await api.loadHistory(file("elapsed_s,value_si,unit_si,mode,status\n0,1,A,DCV,ok\n"));
  assert.match(element(elements, "toast").textContent, /单位 A.*DCV.*V.*不匹配/);

  await api.loadHistory(file("elapsed_s,value_si,unit_si,mode,status\n0,1,V,ALIEN,ok\n"));
  assert.match(element(elements, "toast").textContent, /模式 ALIEN 不受支持/);
  assert.equal(api.app.imported, null);
});

test("the accessible chart table shows escaped raw readings and local sample times", () => {
  const { api, elements } = createFrontend();
  api.app.points = [
    { id: 9, x: 4.5, y: 1.25, raw: "<reading>", status: "ok", unit: "V", timestamp: "2026-10-15T12:34:56+08:00" },
    { id: 10, x: 5, y: null, raw: "timeout", status: "timeout", unit: "V", timestamp: "2026-10-15T12:34:57+08:00" },
  ];

  api.updateAccessiblePointTable();

  const table = element(elements, "pointTableBody").innerHTML;
  assert.match(table, /#9/);
  assert.ok(table.includes(expectedLocalDateTime("2026-10-15T12:34:56+08:00")));
  assert.match(table, /&lt;reading&gt;/);
  assert.doesNotMatch(table, /<reading>/);
  assert.match(table, /1\.25 V/);
  assert.match(table, /timeout/);
});

test("chart accessibility summary updates with readings and statistics without live announcements", () => {
  const html = fs.readFileSync(path.join(ROOT, "src/fluke8845a/web/index.html"), "utf8");
  assert.match(html, /id="plotAccessibilitySummary"[^>]*aria-live="off"/);
  const { api, elements } = createFrontend({ stubChart: false });
  api.app.modes.set("DCV", dcv);
  api.app.selectedMode = "DCV";
  api.app.points = [
    { id: 4, x: 1, y: 1, raw: "1.000", status: "ok", unit: "V", timestamp: "2026-10-15T12:00:00+08:00" },
    { id: 5, x: 2, y: 3, raw: "3.000", status: "ok", unit: "V", timestamp: "2026-10-15T12:00:01+08:00" },
    { id: 6, x: 3, y: null, raw: "timeout", status: "timeout", unit: "V", timestamp: "2026-10-15T12:00:02+08:00" },
  ];
  api.app.chartScale = { xMin: 0, xMax: 3 };

  api.updateAccessiblePointTable();
  api.updateStats();

  const summary = element(elements, "plotAccessibilitySummary").textContent;
  assert.match(summary, /直流电压/);
  assert.match(summary, /2 个有效读数/);
  assert.match(summary, /3 次测量尝试/);
  assert.match(summary, /最新读数.*3 V/);
  assert.match(summary, /平均值.*2 V/);
  assert.match(summary, /标准差/);
  assert.match(summary, /最小值.*1 V/);
  assert.match(summary, /最大值.*3 V/);
  assert.match(summary, /时间范围.*0.*3/);
});

test("history events are imported only when the experiment identifier matches", async () => {
  const { api, elements } = createFrontend();
  api.app.imported = [{ id: 1, x: 0, y: 1, status: "ok", unit: "V" }];
  api.app.importedExperimentId = "run-a";
  const file = (text) => ({ name: "events.csv", size: text.length, text: async () => text });

  await api.loadHistoryEvents(file("experiment_id,elapsed_s,event\nrun-b,1,wrong run\n"));
  assert.match(element(elements, "toast").textContent, /实验标识不一致/);
  assert.equal(api.app.importedEvents, null);

  await api.loadHistoryEvents(file("experiment_id,elapsed_s,event\n,1,unlinked event\n"));
  assert.match(element(elements, "toast").textContent, /实验标识不一致/);
  assert.equal(api.app.importedEvents, null);

  await api.loadHistoryEvents(file("experiment_id,elapsed_s,event\nrun-a,1,load switched\n"));
  assert.equal(api.app.importedEvents.length, 1);
  assert.equal(api.app.importedEvents[0].text, "load switched");
});

test("history directory import pairs only a same-directory events.csv", async () => {
  const html = fs.readFileSync(path.join(ROOT, "src/fluke8845a/web/index.html"), "utf8");
  assert.match(html, /id="historyDirectory"[^>]*webkitdirectory/);
  assert.match(html, /普通文件选择.*不能.*同目录/);
  const { api, elements } = createFrontend();
  api.app.modes.set("DCV", dcv);
  const measurementText = "experiment_id,sample_index,elapsed_s,value_si,unit_si,mode,status,raw_reading\nrun-7,1,0,1,V,DCV,ok,1.0\n";
  const eventFile = (directory, text) => ({
    name: "events.csv", webkitRelativePath: `${directory}/events.csv`,
    size: text.length, text: async () => text,
  });
  const measurement = {
    name: "measurements.csv", webkitRelativePath: "run-7/measurements.csv",
    size: measurementText.length, text: async () => measurementText,
  };
  const unrelated = eventFile("other-run", "experiment_id,elapsed_s,event\nother,1,wrong directory\n");
  const events = eventFile("run-7", "experiment_id,elapsed_s,event\nrun-7,2,load switched\n");

  api.bindControls();
  const picker = element(elements, "historyDirectory");
  picker.files = [measurement, unrelated, events];
  await picker.dispatch("change");

  assert.equal(api.app.importedExperimentId, "run-7");
  assert.equal(api.app.importedEvents.length, 1);
  assert.equal(api.app.importedEvents[0].text, "load switched");
});

test("the point table can be copied as tab-separated text", async () => {
  const { api, clipboard, elements } = createFrontend();
  api.app.points = [{ id: 4, x: 3, y: 1.02, raw: "1.02", status: "ok", unit: "V", timestamp: "2026-10-15T12:00:00+08:00" }];

  await api.copyAccessiblePointTable();

  assert.match(clipboard.text, /样本\t本地时间\t原始响应 \/ 实际量程/);
  assert.ok(clipboard.text.includes(`#4\t${expectedLocalDateTime("2026-10-15T12:00:00+08:00")}\t1.02\t1.02 V\t有效`));
  assert.match(element(elements, "toast").textContent, /已复制/);
});

test("experiment names use local time and preferences have a reset-to-default action", () => {
  const { api, elements, storage } = createFrontend({ preferences: { port: "COM9", baudrate: "9600", outputRoot: "X:\\old" } });
  const date = new Date(2026, 9, 15, 3, 4, 5);
  assert.equal(api.localExperimentName(date), "实验_202610150304");

  api.app.meta = { default_root: "C:\\experiments" };
  api.restorePreferences();
  assert.equal(element(elements, "port").value, "COM9");
  api.clearPreferences();

  assert.equal(storage.has("8845a-web-preferences"), false);
  assert.equal(element(elements, "port").value, "COM3");
  assert.equal(element(elements, "baudrate").value, "9600");
  assert.equal(element(elements, "outputRoot").value, "C:\\experiments");
  assert.match(element(elements, "toast").textContent, /已恢复默认设置/);
});

test("experiment files workspace renders names as text and shows byte sizes", async () => {
  const { api, elements, context } = createFrontend();
  api.bindControls();
  context.fetch = async () => ({
    ok: true,
    json: async () => ({ success: true, directory: "C:\\experiments\\run", files: [
      { name: "measurements.csv", size_bytes: 1234 },
      { name: "<script>.csv", size_bytes: 2 },
    ] }),
  });

  await api.refreshExperimentFiles();

  const rows = element(elements, "filesList").children;
  assert.equal(element(elements, "filesDirectory").textContent, "C:\\experiments\\run");
  assert.equal(rows.length, 2);
  assert.equal(rows[0].children[0].textContent, "measurements.csv");
  assert.equal(rows[0].children[1].textContent, "1,234 B");
  assert.equal(rows[1].children[0].textContent, "<script>.csv");
  assert.equal(rows[1].innerHTML, "");
});

test("an oversized replacement events file cancels an older pending import", async () => {
  const { api, elements } = createFrontend();
  api.app.imported = [];
  let cancelled = false;
  api.app.cancelEventImport = () => { cancelled = true; };

  await api.loadHistoryEvents({ name: "oversized.csv", size: 10 * 1024 * 1024 + 1 });

  assert.equal(cancelled, true);
  assert.match(element(elements, "toast").textContent, /超过 10 MB/);
});

test("baseline editor shows its SI unit and stores the entered value in SI", () => {
  const { api, elements } = createFrontend();
  api.app.modes.set("DCV", dcv);
  api.app.selectedMode = "DCV";
  api.app.points = [{ id: 1, x: 0, y: 1.25, status: "ok", unit: "V" }];
  api.updateStats();
  assert.equal(element(elements, "baselineUnit").textContent, "V（SI 基本单位）");

  api.bindControls();
  element(elements, "baselineValue").value = "1.5";
  element(elements, "baselineValue").dispatch("input");
  assert.equal(api.app.baseline, 1.5);
});

test("baseline input converts the selected engineering prefix back to SI", () => {
  const { api, elements } = createFrontend();
  api.app.modes.set("DCV", dcv);
  api.app.selectedMode = "DCV";
  api.app.points = [{ id: 1, x: 0, y: 1.25, status: "ok", unit: "V" }];
  api.updateStats();
  api.bindControls();

  element(elements, "baselineScale").value = "mV";
  element(elements, "baselineValue").value = "1500";
  element(elements, "baselineValue").dispatch("input");

  assert.equal(api.app.baseline, 1.5);
});

test("changing baseline units does not turn an unset baseline into zero", () => {
  const { api, elements } = createFrontend();
  api.app.modes.set("DCV", dcv);
  api.app.selectedMode = "DCV";
  api.app.baseline = null;
  api.bindControls();

  element(elements, "baselineScale").value = "mV";
  element(elements, "baselineScale").dispatch("change");

  assert.equal(element(elements, "baselineValue").value, "");
  assert.equal(api.app.baseline, null);
});

test("baseline values are restored per live measurement mode", () => {
  const { api, elements } = createFrontend();
  const dci = { ...dcv, value: "DCI", label: "直流电流", unit: "A" };
  api.app.modes.set("DCV", dcv);
  api.app.modes.set("DCI", dci);
  api.app.selectedMode = "DCV";
  api.bindControls();
  element(elements, "baselineValue").value = "1.5";
  element(elements, "baselineValue").dispatch("input");

  element(elements, "mode").value = "DCI";
  element(elements, "mode").dispatch("change");
  assert.equal(api.app.baseline, null);
  element(elements, "baselineValue").value = "0.005";
  element(elements, "baselineValue").dispatch("input");

  element(elements, "mode").value = "DCV";
  element(elements, "mode").dispatch("change");
  assert.equal(api.app.baseline, 1.5);
  assert.equal(element(elements, "baselineValue").value, "1.5");
});

test("resistance baseline input supports megaohms and stores ohms internally", () => {
  const { api, elements } = createFrontend();
  api.app.modes.set("RES2", { ...dcv, value: "RES2", unit: "Ohm" });
  api.app.selectedMode = "RES2";
  api.app.points = [{ id: 1, x: 0, y: 30e6, status: "ok", unit: "Ohm" }];
  api.updateStats();
  api.bindControls();

  element(elements, "baselineScale").value = "MΩ";
  element(elements, "baselineScale").dispatch("change");
  element(elements, "baselineValue").value = "30";
  element(elements, "baselineValue").dispatch("input");

  assert.equal(api.app.baseline, 30e6);
});

test("event entries identify their chart location and selecting one centers the timeline", () => {
  const { api, elements } = createFrontend();
  api.app.imported = [{ id: 1, x: 0, y: 1, status: "ok", unit: "V" }];
  api.app.importedEvents = [{ elapsed_s: 300, text: "load switched", timestamp_local: "2026-10-15T12:00:00+08:00" }];
  api.renderEvents(api.app.importedEvents);
  assert.match(element(elements, "eventList").innerHTML, /data-event-index="0"/);
  assert.match(element(elements, "eventList").innerHTML, /load switched/);

  api.focusEvent(0);
  assert.deepEqual(Array.from(api.app.xView), [240, 360]);
  assert.match(element(elements, "pointCursor").textContent, /事件.*load switched/);
});

test("chart points can be selected and locked as baseline with the keyboard", async () => {
  const { api, elements } = createFrontend({ stubChart: false });
  api.app.modes.set("DCV", dcv);
  element(elements, "chartGestureHint").textContent = "滚轮读数轴 · Shift 时间轴 · Ctrl 双轴 · Alt 零点锚";
  api.app.points = [
    { id: 1, x: 0, y: 1, status: "ok", unit: "V", raw: "1.0" },
    { id: 2, x: 1, y: 2, status: "ok", unit: "V", raw: "2.0" },
  ];
  element(elements, "autoFollow").checked = false;
  api.drawChart();

  await element(elements, "plot").dispatch("keydown", { key: "End", preventDefault() {} });
  assert.match(element(elements, "pointCursor").textContent, /已选样本 #2/);
  assert.equal(element(elements, "chartGestureHint").textContent, "滚轮读数轴 · Shift 时间轴 · Ctrl 双轴 · Alt 零点锚");
  await element(elements, "plot").dispatch("keydown", { key: "Enter", preventDefault() {} });

  assert.equal(api.app.baseline, 2);
  assert.equal(element(elements, "baselineValue").value, "2");
});

test("chart supports keyboard zoom and pan while keeping the view near measured data", async () => {
  const { api, elements } = createFrontend({ stubChart: false });
  api.app.modes.set("DCV", dcv);
  api.app.points = Array.from({ length: 21 }, (_, index) => ({ id: index + 1, x: index, y: index + 1, status: "ok", unit: "V" }));
  element(elements, "autoFollow").checked = false;
  api.drawChart();
  const canvas = element(elements, "plot");
  const initialSpan = api.app.chartScale.xMax - api.app.chartScale.xMin;
  await canvas.dispatch("keydown", { key: "+", preventDefault() {} });
  assert.ok(api.app.xView[1] - api.app.xView[0] < initialSpan);
  const before = [...api.app.xView];
  await canvas.dispatch("keydown", { key: "ArrowRight", altKey: true, preventDefault() {} });
  assert.notDeepEqual(Array.from(api.app.xView), before);
});

test("chart axis title includes the measurement quantity and SI unit", () => {
  const { api, elements } = createFrontend({ stubChart: false });
  api.app.modes.set("RES2", { value: "RES2", label: "两线电阻", quantity: "resistance", unit: "Ohm", ranges: [], nplc: [], bandwidths: [] });
  api.app.selectedMode = "RES2";
  api.app.points = [{ id: 1, x: 0, y: 30e6, status: "ok", unit: "Ohm", mode: "RES2" }];
  element(elements, "autoFollow").checked = false;

  api.drawChart();

  assert.ok(element(elements, "plot").canvasContext.calls.some(([, text]) => text === "电阻 (MΩ)"));
  assert.ok(element(elements, "plot").canvasContext.calls.some(([, text, , , font]) => text !== "电阻 (MΩ)" && /11px/.test(font || "")), "axis labels use a readable 11px canvas font");
});

test("large history is downsampled for drawing while preserving an isolated peak", () => {
  const { api, elements } = createFrontend({ stubChart: false });
  api.app.modes.set("DCV", dcv);
  const peakIndex = 100_123;
  api.app.points = Array.from({ length: 200_000 }, (_, index) => ({
    id: index + 1, x: index, y: index === peakIndex ? 10 : 1,
    status: "ok", unit: "V", raw: String(index === peakIndex ? 10 : 1),
  }));
  element(elements, "autoFollow").checked = false;

  assert.doesNotThrow(() => api.drawChart());
  const curve = element(elements, "plot").canvasContext.strokes.find((stroke) => stroke.color === "#138b92");
  assert.ok(curve.path.length <= 8_000);
  const peakY = api.app.chartScale.yToPixel(10);
  assert.ok(curve.path.some(([, , y]) => Math.abs(y - peakY) < 0.01));
  assert.ok(api.app.chartScale.pointIndex?.root);
  assert.equal(api.nearestPoint(api.app.chartScale.xToPixel(peakIndex), api.app.chartScale.yToPixel(10)).point.id, peakIndex + 1);
});

test("nearest-point pixel index is reused across cursor redraws", () => {
  const { api, elements } = createFrontend({ stubChart: false });
  api.app.modes.set("DCV", dcv);
  api.app.points = Array.from({ length: 101 }, (_, index) => ({ id: index + 1, x: index, y: index, status: "ok", unit: "V" }));
  element(elements, "autoFollow").checked = false;

  api.drawChart();
  const firstIndex = api.app.chartScale.pointIndex;
  assert.ok(firstIndex?.root);
  api.app.cursor = { x: 300, y: 160 };
  api.drawChart();

  assert.equal(api.app.chartScale.pointIndex, firstIndex);
});

test("sparse indexed data still exposes the nearest point between distant samples", async () => {
  const { api, elements, animationFrames } = createFrontend({ stubChart: false });
  api.app.modes.set("DCV", dcv);
  api.app.points = [
    { id: 1, x: 0, y: 1, status: "ok", unit: "V", raw: "1.000" },
    { id: 2, x: 1, y: 2, status: "ok", unit: "V", raw: "2.000" },
  ];
  element(elements, "autoFollow").checked = false;
  api.drawChart();

  await element(elements, "plot").dispatch("pointermove", { clientX: 400, clientY: 200 });
  while (animationFrames.length) animationFrames.shift()();

  assert.equal(api.app.cursor?.point?.id, 1);
  assert.match(element(elements, "pointCursor").textContent, /样本 #1.*原始 1\.000/);
});

test("nearest-point lookup expands past a close-x bucket when a farther bucket is much closer in y", () => {
  const { api } = createFrontend();
  const nearXFarY = { id: 1, x: 400, y: 100 };
  const fartherXNearY = { id: 2, x: 403, y: 200 };
  makeIndexedNearestFixture(api, [nearXFarY, fartherXNearY], { xMin: 0, xMax: 800, yMin: 0, yMax: 400, left: 0, right: 800, top: 0, bottom: 400 });

  assert.equal(api.nearestPoint(400, 200).point.id, 2);
});

function makeIndexedNearestFixture(api, rows, { xMin = 0, xMax = 100, yMin = 0, yMax = 100, left = 0, right = 100, top = 0, bottom = 100 } = {}) {
  const xToPixel = (value) => left + (value - xMin) / (xMax - xMin) * (right - left);
  const yToPixel = (value) => bottom - (value - yMin) / (yMax - yMin) * (bottom - top);
  const pointIndex = api.buildPixelPointIndex(rows, xToPixel, yToPixel, xMin, xMax);
  api.app.chartScale = { xMin, xMax, yMin, yMax, left, right, top, bottom, rows, pointIndex, xToPixel, yToPixel };
  return api.app.chartScale;
}

function legacyLinearNearest(rows, scale, canvasX, canvasY) {
  let nearest = null, distance = Infinity;
  for (const point of rows) {
    if (point.x < scale.xMin || point.x > scale.xMax) continue;
    const dx = scale.xToPixel(point.x) - canvasX;
    const dy = scale.yToPixel(point.y) - canvasY;
    const score = dx * dx + dy * dy * .6;
    if (score < distance) { nearest = point; distance = score; }
  }
  return nearest;
}

function countIndexCandidateCoordinateReads(index) {
  const candidates = [];
  if (typeof index?.values === "function") {
    for (const bucket of index.values()) candidates.push(...bucket);
  } else {
    const pending = [index?.root];
    while (pending.length) {
      const node = pending.pop();
      if (!node) continue;
      if (node.entries) candidates.push(...node.entries);
      else pending.push(node.left, node.right);
    }
  }
  let reads = 0;
  for (const candidate of candidates) {
    for (const key of ["x", "y"]) {
      const coordinate = candidate[key];
      Object.defineProperty(candidate, key, { configurable: true, get() { reads++; return coordinate; } });
    }
  }
  return () => reads;
}

test("indexed nearest point matches legacy linear selection for y-clipped, tie, sparse, and weighted-distance cases", () => {
  const cases = [
    {
      name: "y-clipped point remains eligible while x-outside point stays excluded",
      rows: [{ id: 11, x: 50, y: 101 }, { id: 12, x: 52, y: 90 }, { id: 13, x: 101, y: 100 }],
      x: 50, y: 0, expectedId: 11,
    },
    {
      name: "equal distances retain original row order",
      rows: [
        { id: 21, x: 60, y: 50 }, { id: 22, x: 40, y: 50 },
        ...Array.from({ length: 16 }, (_, index) => ({ id: 100 + index, x: index, y: 0 })),
        ...Array.from({ length: 16 }, (_, index) => ({ id: 200 + index, x: 85 + index, y: 0 })),
      ],
      x: 50, y: 50, expectedId: 21,
    },
    {
      name: "sparse points use the same nearest distance as a linear scan",
      rows: [{ id: 31, x: 0, y: 0 }, { id: 32, x: 100, y: 100 }],
      x: 49, y: 49, expectedId: 31,
    },
    {
      name: "slightly farther x wins when the closer x is much farther in y",
      rows: [{ id: 41, x: 50, y: 10 }, { id: 42, x: 53, y: 100 }],
      x: 50, y: 0, expectedId: 42,
    },
    {
      name: "the .6 vertical weight affects both ranking and pruning across branches",
      rows: [
        { id: 51, x: 55, y: 50 }, { id: 52, x: 50, y: 44 },
        ...Array.from({ length: 16 }, (_, index) => ({ id: 300 + index, x: index, y: 0 })),
        ...Array.from({ length: 16 }, (_, index) => ({ id: 400 + index, x: 85 + index, y: 0 })),
      ],
      x: 50, y: 50, expectedId: 52,
    },
  ];
  const actualIds = [];
  const expectedIds = [];
  for (const scenario of cases) {
    const { api } = createFrontend();
    const scale = makeIndexedNearestFixture(api, scenario.rows);
    const expected = legacyLinearNearest(scenario.rows, scale, scenario.x, scenario.y);
    assert.equal(expected?.id, scenario.expectedId, `${scenario.name}: hand-checked legacy result`);
    actualIds.push(api.nearestPoint(scenario.x, scenario.y).point.id);
    expectedIds.push(expected.id);
  }
  assert.deepEqual(actualIds, expectedIds, cases.map(({ name }) => name).join("; "));
});

test("nearest-point index prunes candidate reads for 20k dense flat samples", () => {
  const { api } = createFrontend();
  let sourceReads = 0;
  const rows = Array.from({ length: 20_000 }, (_, index) => {
    const x = .499 + index * .0000001;
    const point = { id: index + 1 };
    Object.defineProperties(point, {
      x: { get() { sourceReads++; return x; } },
      y: { get() { sourceReads++; return 20; } },
    });
    return point;
  });
  const scale = makeIndexedNearestFixture(api, rows, { xMin: 0, xMax: 1, left: 0, right: 1, yMin: 0, yMax: 40, top: 0, bottom: 40 });
  const readCandidateCoordinates = countIndexCandidateCoordinateReads(scale.pointIndex);
  sourceReads = 0;

  const result = api.nearestPoint(.5, 20);

  assert.equal(result.point.id, 10_001);
  assert.ok(readCandidateCoordinates() < 1_000, `indexed search read ${readCandidateCoordinates()} candidate coordinates`);
  assert.equal(sourceReads, 2, "only the selected row is re-read to produce the returned pixel position");
});

test("pointer hover paints only the cursor overlay instead of redrawing the data plot", async () => {
  const html = fs.readFileSync(path.join(ROOT, "src/fluke8845a/web/index.html"), "utf8");
  assert.match(html, /id="plotOverlay"[^>]*aria-hidden="true"/);
  const { api, elements, animationFrames } = createFrontend({ stubChart: false });
  api.app.modes.set("DCV", dcv);
  api.app.points = Array.from({ length: 20_000 }, (_, index) => ({
    id: index + 1, x: index, y: 1 + index / 20_000, status: "ok", unit: "V", raw: String(index),
  }));
  element(elements, "autoFollow").checked = false;
  api.drawChart();
  const plotStrokes = element(elements, "plot").canvasContext.strokes.length;
  const overlay = element(elements, "plotOverlay").canvasContext;

  await element(elements, "plot").dispatch("pointermove", { clientX: 400, clientY: 180 });
  while (animationFrames.length) animationFrames.shift()();

  assert.equal(element(elements, "plot").canvasContext.strokes.length, plotStrokes);
  assert.ok(overlay.strokes.length > 0, "the pointer crosshair is rendered on the lightweight overlay");
});

test("nearest-point lookup retains a correct linear fallback without a pixel index", () => {
  const { api } = createFrontend();
  const rows = [{ id: 1, x: 0, y: 1 }, { id: 2, x: 1, y: 2 }, { id: 3, x: 2, y: 3 }];
  api.app.chartScale = {
    xMin: 0, xMax: 2, yMin: 0, yMax: 4, left: 0, right: 200, top: 0, bottom: 400,
    rows, pointIndex: null, xToPixel: (value) => value * 100, yToPixel: (value) => 400 - value * 100,
  };

  assert.equal(api.nearestPoint(101, 201).point.id, 2);
});

test("multiple pointer redraw requests collapse into one animation frame", () => {
  const { api, animationFrames } = createFrontend({ stubChart: false });
  api.app.points = [{ id: 1, x: 0, y: 1, status: "ok", unit: "V" }];
  api.scheduleChartRedraw();
  api.scheduleChartRedraw();
  assert.equal(animationFrames.length, 1);

  animationFrames.shift()();
  assert.ok(api.app.chartScale);
});

test("two touch pointers pinch-zoom both chart axes while one pointer remains available for pan", async () => {
  const { api, elements } = createFrontend({ stubChart: false });
  api.app.modes.set("DCV", dcv);
  api.app.points = Array.from({ length: 101 }, (_, index) => ({ id: index + 1, x: index, y: index, status: "ok", unit: "V" }));
  element(elements, "autoFollow").checked = false;
  api.drawChart();
  const canvas = element(elements, "plot");
  const initialXSpan = api.app.chartScale.xMax - api.app.chartScale.xMin;
  const initialYSpan = api.app.chartScale.yMax - api.app.chartScale.yMin;

  await canvas.dispatch("pointerdown", { pointerId: 1, pointerType: "touch", clientX: 200, clientY: 180 });
  await canvas.dispatch("pointerdown", { pointerId: 2, pointerType: "touch", clientX: 600, clientY: 280 });
  await canvas.dispatch("pointermove", { pointerId: 2, pointerType: "touch", clientX: 700, clientY: 320, preventDefault() {} });

  assert.ok(api.app.xView[1] - api.app.xView[0] < initialXSpan);
  assert.ok(api.app.yView[1] - api.app.yView[0] < initialYSpan);
});

test("manual panning cannot move the viewport completely away from measured data", async () => {
  const { api, elements } = createFrontend({ stubChart: false });
  api.app.modes.set("DCV", dcv);
  api.app.points = Array.from({ length: 101 }, (_, index) => ({ id: index + 1, x: index, y: index, status: "ok", unit: "V" }));
  element(elements, "autoFollow").checked = false;
  api.drawChart();
  const canvas = element(elements, "plot");

  await canvas.dispatch("pointerdown", { pointerId: 1, clientX: 400, clientY: 180 });
  await canvas.dispatch("pointermove", { pointerId: 1, clientX: -100_000, clientY: 180 });

  const [xMin, xMax] = api.app.xView;
  const overlap = Math.max(0, Math.min(xMax, 100) - Math.max(xMin, 0));
  assert.ok(overlap >= 25);
});

test("web wheel modifiers follow desktop axis mapping and Alt anchor precedence", async () => {
  const localPython = path.join(ROOT, ".venv-build", "Scripts", "python.exe");
  const python = process.env.PYTHON || (fs.existsSync(localPython) ? localPython : "python");
  const desktopMapping = spawnSync(python, ["-c", [
    "import json",
    "from PySide6.QtCore import Qt",
    "from fluke8845a.gui import _wheel_zoom_axes",
    "mods = [Qt.KeyboardModifier.NoModifier, Qt.KeyboardModifier.ShiftModifier, Qt.KeyboardModifier.ControlModifier, Qt.KeyboardModifier.AltModifier, Qt.KeyboardModifier.AltModifier | Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier]",
    "print(json.dumps([_wheel_zoom_axes(mod) for mod in mods]))",
  ].join("; ")], {
    cwd: ROOT,
    encoding: "utf8",
    env: { ...process.env, PYTHONPATH: path.join(ROOT, "src") },
  });
  assert.equal(desktopMapping.status, 0, desktopMapping.stderr);
  const expectedMapping = [[false, true], [true, false], [true, true], [false, true], [false, true]];
  assert.deepEqual(JSON.parse(desktopMapping.stdout), expectedMapping, "this is the desktop _wheel_zoom_axes mapping");

  const { api, elements } = createFrontend({ stubChart: false });
  api.app.points = Array.from({ length: 11 }, (_, index) => ({ id: index + 1, x: index, y: index + 10, status: "ok", unit: "V" }));
  element(elements, "autoFollow").checked = false;
  api.drawChart();
  const canvas = element(elements, "plot");
  const bounds = () => ({
    x: [...(api.app.xView || [api.app.chartScale.xMin, api.app.chartScale.xMax])],
    y: [...(api.app.yView || [api.app.chartScale.yMin, api.app.chartScale.yMax])],
  });
  const zoom = async (modifiers) => {
    const before = bounds();
    await canvas.dispatch("wheel", { clientX: 400, clientY: 200, deltaY: -120, preventDefault() {}, ...modifiers });
    const after = bounds();
    return { before, after };
  };

  const plain = await zoom({});
  assert.deepEqual(plain.after.x, plain.before.x);
  assert.ok(plain.after.y[1] - plain.after.y[0] < plain.before.y[1] - plain.before.y[0]);
  const gui = fs.readFileSync(path.join(ROOT, "src/fluke8845a/gui.py"), "utf8");
  assert.match(gui, /factor = math\.exp\(-float\(delta\) \/ 120\.0 \* 0\.18\)/);
  const webFactor = (plain.after.y[1] - plain.after.y[0]) / (plain.before.y[1] - plain.before.y[0]);
  assert.ok(Math.abs(webFactor - Math.exp(-0.18)) < 1e-12, "one upward wheel notch uses the desktop zoom factor");

  const shift = await zoom({ shiftKey: true });
  assert.ok(shift.after.x[1] - shift.after.x[0] < shift.before.x[1] - shift.before.x[0]);
  assert.deepEqual(shift.after.y, shift.before.y);

  const ctrl = await zoom({ ctrlKey: true });
  assert.ok(ctrl.after.x[1] - ctrl.after.x[0] < ctrl.before.x[1] - ctrl.before.x[0]);
  assert.ok(ctrl.after.y[1] - ctrl.after.y[0] < ctrl.before.y[1] - ctrl.before.y[0]);

  const altOverrides = await zoom({ altKey: true, ctrlKey: true, shiftKey: true });
  assert.deepEqual(altOverrides.after.x, altOverrides.before.x);
  assert.ok(altOverrides.after.y[1] - altOverrides.after.y[0] < altOverrides.before.y[1] - altOverrides.before.y[0]);
  const zeroBefore = (0 - altOverrides.before.y[0]) / (altOverrides.before.y[1] - altOverrides.before.y[0]);
  const zeroAfter = (0 - altOverrides.after.y[0]) / (altOverrides.after.y[1] - altOverrides.after.y[0]);
  assert.ok(Math.abs(zeroAfter - zeroBefore) < 1e-12, "Alt keeps zero at the same vertical screen position while zooming");
});

test("manual zoom of either axis turns off auto-follow and updates the visible status", async () => {
  const { api, elements } = createFrontend({ stubChart: false });
  api.app.points = Array.from({ length: 21 }, (_, index) => ({ id: index + 1, x: index, y: index + 1, status: "ok", unit: "V" }));
  element(elements, "autoFollow").checked = true;
  api.drawChart();

  await element(elements, "plot").dispatch("wheel", { clientX: 400, clientY: 200, deltaY: -120, preventDefault() {} });

  assert.equal(element(elements, "autoFollow").checked, false);
  assert.match(element(elements, "viewModeStatus").textContent, /自动跟随已关闭/);
  const html = fs.readFileSync(path.join(ROOT, "src/fluke8845a/web/index.html"), "utf8");
  assert.match(html, /手动缩放或平移任一坐标轴.*自动跟随关闭/);
});

test("Shift-wheel time-axis zoom turns off auto-follow and preserves the reading axis", async () => {
  const { api, elements } = createFrontend({ stubChart: false });
  api.app.points = Array.from({ length: 101 }, (_, index) => ({ id: index + 1, x: index, y: index + 1, status: "ok", unit: "V" }));
  element(elements, "autoFollow").checked = true;
  api.drawChart();
  const initialXSpan = api.app.chartScale.xMax - api.app.chartScale.xMin;
  const initialYRange = [api.app.chartScale.yMin, api.app.chartScale.yMax];

  await element(elements, "plot").dispatch("wheel", {
    clientX: 400, clientY: 200, deltaY: -120, shiftKey: true, preventDefault() {},
  });

  assert.ok(api.app.xView[1] - api.app.xView[0] < initialXSpan);
  assert.equal(api.app.yView, null);
  assert.deepEqual([api.app.chartScale.yMin, api.app.chartScale.yMax], initialYRange);
  assert.equal(element(elements, "autoFollow").checked, false);
  assert.match(element(elements, "viewModeStatus").textContent, /自动跟随已关闭/);
});

test("a horizontal wheel event with no vertical delta does not leave auto-follow", async () => {
  const { api, elements } = createFrontend({ stubChart: false });
  api.app.points = Array.from({ length: 101 }, (_, index) => ({ id: index + 1, x: index, y: index + 1, status: "ok", unit: "V" }));
  element(elements, "autoFollow").checked = true;
  api.drawChart();

  await element(elements, "plot").dispatch("wheel", {
    clientX: 400, clientY: 200, deltaX: 120, deltaY: 0, preventDefault() {},
  });

  assert.equal(element(elements, "autoFollow").checked, true);
  assert.match(element(elements, "viewModeStatus").textContent, /自动跟随最新数据/);
  assert.equal(api.app.xView, null);
  assert.equal(api.app.yView, null);
});

test("manual pointer panning turns off auto-follow and updates the visible status", async () => {
  const { api, elements, animationFrames } = createFrontend({ stubChart: false });
  api.app.points = Array.from({ length: 101 }, (_, index) => ({ id: index + 1, x: index, y: index + 1, status: "ok", unit: "V" }));
  element(elements, "autoFollow").checked = true;
  api.drawChart();
  const initialXRange = [api.app.chartScale.xMin, api.app.chartScale.xMax];
  const canvas = element(elements, "plot");

  await canvas.dispatch("pointerdown", { pointerId: 1, clientX: 400, clientY: 180 });
  await canvas.dispatch("pointermove", { pointerId: 1, clientX: 320, clientY: 180 });
  while (animationFrames.length) animationFrames.shift()();

  assert.equal(element(elements, "autoFollow").checked, false);
  assert.notDeepEqual(api.app.xView, initialXRange);
  assert.match(element(elements, "viewModeStatus").textContent, /自动跟随已关闭/);
});

test("full Web layout fits 280–351 px viewports and honors the 350 px boundary in Chromium", (t) => {
  const candidates = [
    process.env.CHROME_BIN,
    process.env.EDGE_BIN,
    "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe",
    "C:/Program Files/Google/Chrome/Application/chrome.exe",
  ].filter(Boolean);
  const browser = candidates.find((candidate) => fs.existsSync(candidate));
  if (!browser) return t.skip("Chromium is not installed; browser geometry cannot be measured here");

  const css = fs.readFileSync(path.join(ROOT, "src/fluke8845a/web/app.css"), "utf8");
  const html = fs.readFileSync(path.join(ROOT, "src/fluke8845a/web/index.html"), "utf8")
    .replace('<link rel="stylesheet" href="/app.css">', `<style>${css}</style>`)
    .replace(/\s*<script[^>]*>[\s\S]*?<\/script>|\s*<script[^>]*><\/script>|\s*<script[^>]*><\/script>/g, "")
    .replace(/\s*<script[^>]*src="[^"]*"[^>]*><\/script>/g, "");
  const childDocument = html.replace("</body>", "</body>").replace(/<script[\s\S]*?<\/script>/g, "");
  const probe = `<!doctype html><meta charset="utf-8"><pre id="result">pending</pre><script>
    const source = ${JSON.stringify(childDocument)};
    const widths = [280, 349, 350, 351];
    const frames = widths.map((width) => {
      const frame = document.createElement("iframe");
      frame.style.cssText = "display:block;border:0;width:" + width + "px;height:900px";
      frame.srcdoc = source;
      document.body.appendChild(frame);
      return { width, frame };
    });
    Promise.all(frames.map(({frame}) => new Promise((resolve) => frame.addEventListener("load", () => {
      const doc = frame.contentDocument;
      const viewport = frame.contentWindow.innerWidth;
      const measured = [".topbar", ".workspace", ".measurement-area", ".readout-panel", ".chart-panel", ".plot-wrap", ".chart-context", ".chart-context-actions", "#pointCursor", "#chartGestureHint", ".chart-actions .toggle-label"]
        .map((selector) => { const node = doc.querySelector(selector); const rect = node.getBoundingClientRect(); return [selector, rect.left, rect.right]; });
      const overflowing = [...doc.body.querySelectorAll("*")].map((node) => {
        const rect = node.getBoundingClientRect();
        return [node.id || node.className || node.tagName, rect.left, rect.right, node.scrollWidth, node.clientWidth];
      }).filter(([, left, right]) => left < -1 || right > doc.documentElement.clientWidth + 1).slice(0, 12);
      const targetRect = doc.querySelector(".chart-actions .toggle-label").getBoundingClientRect();
      resolve({ viewport, bodyMinWidth: getComputedStyle(doc.body).minWidth, bodyClient: doc.body.clientWidth, bodyScroll: doc.body.scrollWidth,
        documentClient: doc.documentElement.clientWidth, documentScroll: doc.documentElement.scrollWidth,
        touchTarget: [targetRect.width, targetRect.height], measured, overflowing });
    })))).then((metrics) => { document.getElementById("result").textContent = JSON.stringify(metrics); });
  </script>`;
  const tempHtml = path.join(require("node:os").tmpdir(), `8845a-layout-${process.pid}-${Date.now()}.html`);
  fs.writeFileSync(tempHtml, probe, "utf8");
  try {
    const result = spawnSync(browser, ["--headless", "--disable-gpu", "--no-first-run", "--no-default-browser-check", "--no-sandbox", "--dump-dom", tempHtml], { encoding: "utf8", timeout: 7000, windowsHide: true });
    assert.equal(result.error, undefined, result.error?.message);
    assert.equal(result.status, 0, result.stderr);
    const match = result.stdout.match(/<pre id="result">(\[[\s\S]*?\])<\/pre>/);
    assert.ok(match, "Chromium returns measured layout geometry");
    const metrics = JSON.parse(match[1]);
    assert.equal(metrics.length, 4);
    for (const layout of metrics) {
      assert.equal(layout.bodyScroll, layout.bodyClient, `${layout.viewport}px body has no horizontal overflow: ${JSON.stringify(layout)}`);
      assert.equal(layout.documentScroll, layout.documentClient, `${layout.viewport}px document has no horizontal overflow`);
      assert.ok(layout.documentClient <= layout.viewport, `${layout.viewport}px document client area stays within the viewport`);
      assert.equal(layout.bodyMinWidth, layout.viewport <= 350 ? "0px" : "280px", `${layout.viewport}px min-width breakpoint is applied at the intended boundary`);
      assert.ok(layout.touchTarget[0] >= 44, `${layout.viewport}px chart toggle target is at least 44 CSS px wide`);
      assert.ok(layout.touchTarget[1] >= 44, `${layout.viewport}px chart toggle target is at least 44 CSS px tall`);
      for (const [selector, left, right] of layout.measured) {
        assert.ok(left >= -1 && right <= layout.documentClient + 1, `${selector} stays within ${layout.viewport}px viewport: ${left}..${right}`);
      }
    }
  } finally {
    fs.rmSync(tempHtml, { force: true });
  }
});

test("automatic all-data zoom and return-to-live-window have distinct states", async () => {
  const { api, elements } = createFrontend({ stubChart: false });
  api.app.points = [{ id: 1, x: 1, y: 1, status: "ok", unit: "V" }];
  element(elements, "autoFollow").checked = true;
  api.drawChart();

  await element(elements, "autoScale").dispatch("click");
  assert.equal(element(elements, "autoFollow").checked, false);
  assert.match(element(elements, "viewModeStatus").textContent, /手动视图.*关闭/);

  await element(elements, "resetRealtimeView").dispatch("click");
  assert.equal(element(elements, "autoFollow").checked, true);
  assert.match(element(elements, "viewModeStatus").textContent, /自动跟随最新数据/);
});

test("web chart guidance explains axis follow behavior and highlights return to live", () => {
  const html = fs.readFileSync(path.join(ROOT, "src/fluke8845a/web/index.html"), "utf8");
  assert.match(html, /手动缩放或平移任一坐标轴后，自动跟随关闭/);
  assert.match(html, /id="resetRealtimeView" class="button button-primary"/);
  assert.match(html, /回到实时窗口/);
});

test("diagnostic logs can be filtered by source and exported as plain text", () => {
  const { api } = createFrontend();
  const rows = [
    { time: "2026-10-15T12:00:00+08:00", text: "! SCPI response timeout" },
    { time: "2026-10-15T12:00:01+08:00", text: "> SENS:FUNC?" },
    { time: "2026-10-15T12:00:02+08:00", text: "COM3 设备已连接" },
    { time: "2026-10-15T12:00:03+08:00", text: "开始实验采集" },
  ];

  assert.deepEqual(Array.from(api.filterLogRows(rows, "error")).map((row) => row.text), ["! SCPI response timeout"]);
  assert.deepEqual(Array.from(api.filterLogRows(rows, "scpi")).map((row) => row.text), ["> SENS:FUNC?"]);
  assert.deepEqual(Array.from(api.filterLogRows(rows, "connection")).map((row) => row.text), ["COM3 设备已连接"]);
  assert.deepEqual(Array.from(api.filterLogRows(rows, "acquisition")).map((row) => row.text), ["开始实验采集"]);
  const output = api.formatLogsText(api.filterLogRows(rows, "scpi"));
  assert.match(output, /\[SCPI\] > SENS:FUNC\?/);
  assert.doesNotMatch(output, /timeout|COM3|开始实验/);
});

test("CSV download state distinguishes a live snapshot from a completed experiment", () => {
  const { api, elements } = createFrontend();

  api.updateStateDisplay(connectedState({
    status: "连续采集中", experiment_dir: "D:\\experiments\\active", last_experiment_dir: "D:\\experiments\\active",
  }));
  assert.equal(element(elements, "downloadCsv").textContent, "下载进行中快照 CSV");
  assert.match(element(elements, "downloadCsv").title, /快照时间|完整记录/);

  api.updateStateDisplay(connectedState({ status: "等待开始", experiment_dir: "", last_experiment_dir: "D:\\experiments\\done" }));
  assert.equal(element(elements, "downloadCsv").textContent, "下载最近已保存 CSV");
  assert.match(element(elements, "downloadCsv").title, /已完成并保存/);
});

test("events export is a separate safety copy and stays disabled during acquisition", () => {
  const html = fs.readFileSync(path.join(ROOT, "src/fluke8845a/web/index.html"), "utf8");
  const javascript = fs.readFileSync(path.join(ROOT, "src/fluke8845a/web/app.js"), "utf8");
  assert.match(html, /id="downloadEventsSafe"[^>]*disabled[^>]*安全处理；原始 events\.csv 不会更改/);
  assert.match(javascript, /downloadEventsSafe"\)\.disabled = !state\.last_experiment_dir \|\| !!state\.experiment_dir/);
  assert.match(javascript, /\/api\/latest-events-safe\.csv/);
  assert.match(javascript, /已导出事件安全副本；原始 events\.csv 未修改/);
});

test("measurement export is a separate safety copy and stays disabled during acquisition", () => {
  const html = fs.readFileSync(path.join(ROOT, "src/fluke8845a/web/index.html"), "utf8");
  const javascript = fs.readFileSync(path.join(ROOT, "src/fluke8845a/web/app.js"), "utf8");
  assert.match(html, /id="downloadMeasurementsSafe"[^>]*disabled[^>]*原始 measurements\.csv 不会更改/);
  assert.match(javascript, /downloadMeasurementsSafe"\)\.disabled = !state\.last_experiment_dir \|\| !!state\.experiment_dir/);
  assert.match(javascript, /\/api\/latest-measurements-safe\.csv/);
  assert.match(javascript, /已导出测量安全副本；原始 measurements\.csv 未修改/);
  const { api, elements } = createFrontend();
  api.updateStateDisplay(connectedState({ experiment_dir: "D:\\experiments\\active", last_experiment_dir: "D:\\experiments\\active" }));
  assert.equal(element(elements, "downloadMeasurementsSafe").disabled, true);
  api.updateStateDisplay(connectedState({ experiment_dir: "", last_experiment_dir: "D:\\experiments\\done" }));
  assert.equal(element(elements, "downloadMeasurementsSafe").disabled, false);
});

test("PNG export includes experiment, mode, time range, local export time, and baseline context", () => {
  const { api, elements } = createFrontend();
  api.app.modes.set("DCV", dcv);
  api.app.selectedMode = "DCV";
  api.app.points = [
    { id: 1, x: 1, y: 1.25, status: "ok", unit: "V", timestamp: "2026-10-15T12:00:00+08:00" },
    { id: 2, x: 5, y: 1.5, status: "ok", unit: "V", timestamp: "2026-10-15T12:00:04+08:00" },
  ];
  api.app.baseline = 1.25;
  api.app.chartScale = { xMin: 1, xMax: 5 };
  element(elements, "experimentName").value = "阶跃响应 A";

  const exported = api.createPlotExportCanvas();

  assert.equal(exported.width, element(elements, "plot").width);
  assert.ok(exported.height > element(elements, "plot").height);
  const labels = exported.canvasContext.calls.map((call) => call[1]).join(" ");
  assert.match(labels, /阶跃响应 A/);
  assert.match(labels, /直流电压/);
  assert.match(labels, /时间范围.*1.*5/);
  assert.match(labels, /基准线.*1\.25/);
  assert.match(labels, /导出时间/);
});

test("experiment path can be copied locally and folder actions stay disabled until a run exists", async () => {
  const { api, elements, clipboard } = createFrontend();
  const html = fs.readFileSync(path.join(ROOT, "src/fluke8845a/web/index.html"), "utf8");
  assert.match(html, /id="experimentFiles"/);
  api.updateStateDisplay(connectedState({ experiment_dir: "", last_experiment_dir: "" }));
  assert.equal(element(elements, "openExperimentDir").disabled, true);
  assert.equal(element(elements, "copyExperimentPath").disabled, true);

  api.app.state = connectedState({ experiment_dir: "", last_experiment_dir: "D:\\experiments\\run-1" });
  api.updateStateDisplay(api.app.state);
  assert.match(element(elements, "experimentFiles").textContent, /measurements\.csv.*events\.csv.*config\.json.*run\.log/);
  assert.equal(element(elements, "openExperimentDir").disabled, false);
  assert.equal(element(elements, "copyExperimentPath").disabled, false);
  await api.copyExperimentPath();
  assert.equal(clipboard.text, "D:\\experiments\\run-1");
  assert.match(element(elements, "toast").textContent, /路径已复制/);
});

test("Web experiment names reject Windows path characters and overlong names before command submission", () => {
  const { api, elements } = createFrontend();
  assert.match(api.experimentNameError(""), /请填写/);
  assert.match(api.experimentNameError("bad/name"), /非法字符/);
  assert.match(api.experimentNameError("x".repeat(101)), /最多100/);
  assert.equal(api.experimentNameError("合法实验名称"), "");
  assert.ok(element(elements, "experimentNameError"));
});

test("CSV download reports the server snapshot time from response headers", async () => {
  const { api, context, elements } = createFrontend();
  const savedUrls = [];
  context.fetch = async (url, options) => {
    assert.equal(url, "/api/latest.csv");
    assert.equal(options.cache, "no-store");
    return {
      ok: true,
      headers: { get: (name) => ({
        "X-8845A-Export-State": "in-progress-snapshot",
        "X-8845A-Snapshot-Time": "2026-10-15T12:34:56+08:00",
        "Content-Disposition": "attachment; filename*=UTF-8''run_%E8%BF%9B%E8%A1%8C%E4%B8%AD%E5%BF%AB%E7%85%A7.csv",
      }[name]) },
      blob: async () => new Blob(["raw_reading\n1\n"]),
    };
  };
  context.URL = { createObjectURL: (blob) => { savedUrls.push(blob); return "blob:mock"; }, revokeObjectURL() {} };

  await api.downloadLatestCsv();

  assert.equal(savedUrls.length, 1);
  assert.match(element(elements, "toast").textContent, /进行中快照.*2026-10-15T12:34:56\+08:00/);
});
