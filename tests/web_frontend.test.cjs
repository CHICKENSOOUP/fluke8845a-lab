"use strict";

const assert = require("node:assert/strict");
const { spawn, spawnSync } = require("node:child_process");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const ROOT = path.resolve(__dirname, "..");

function expectedLocalDateTime(value) {
  const date = new Date(value);
  const pad = (part) => String(part).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;
}

const wait = (milliseconds) => new Promise((resolve) => setTimeout(resolve, milliseconds));

async function inspectChromiumDom(browser, html) {
  assert.equal(typeof WebSocket, "function", "Node provides WebSocket for the local Chromium DevTools connection");
  const profile = fs.mkdtempSync(path.join(os.tmpdir(), "fluke8845a-layout-"));
  let child;
  let childError;
  let childClosed = false;
  let socket;
  let result;
  let primaryError;

  try {
    child = spawn(browser, [
      "--headless=new",
      "--disable-gpu",
      "--no-first-run",
      "--no-default-browser-check",
      "--no-sandbox",
      "--remote-debugging-port=0",
      `--user-data-dir=${profile}`,
      "--remote-allow-origins=*",
      "about:blank",
    ], { detached: process.platform !== "win32", stdio: "ignore", windowsHide: true });
    child.on("error", (error) => { childError = error; });
    child.on("close", () => { childClosed = true; });

    const activePortFile = path.join(profile, "DevToolsActivePort");
    let activePort = "";
    const startupDeadline = Date.now() + 15000;
    while (Date.now() < startupDeadline && !activePort) {
      if (childError) throw childError;
      try { activePort = fs.readFileSync(activePortFile, "utf8"); } catch {}
      if (!activePort) await wait(100);
    }
    assert.ok(activePort, `Chromium starts its isolated DevTools endpoint (launcher exit code: ${child.exitCode})`);

    const port = Number(activePort.split(/\r?\n/)[0]);
    let page;
    const targetDeadline = Date.now() + 5000;
    while (Date.now() < targetDeadline && !page) {
      try {
        const response = await fetch(`http://127.0.0.1:${port}/json/list`, { signal: AbortSignal.timeout(1000) });
        const targets = await response.json();
        page = targets.find((target) => target.type === "page");
      } catch {}
      if (!page) await wait(100);
    }
    assert.ok(page, "Chromium exposes a page target through DevTools");

    socket = new WebSocket(page.webSocketDebuggerUrl);
    const socketOpen = new Promise((resolve, reject) => {
      socket.addEventListener("open", resolve, { once: true });
      socket.addEventListener("error", reject, { once: true });
    });
    const browserLaunchFailure = new Promise((_, reject) => {
      if (childError) reject(childError);
      else child.once("error", reject);
    });
    await Promise.race([
      socketOpen,
      browserLaunchFailure,
      wait(5000).then(() => { throw new Error("Chromium DevTools WebSocket did not open within five seconds"); }),
    ]);

    let nextId = 0;
    const pending = new Map();
    socket.addEventListener("message", (event) => {
      const message = JSON.parse(event.data);
      if (!pending.has(message.id)) return;
      const request = pending.get(message.id);
      pending.delete(message.id);
      if (message.error) request.reject(new Error(JSON.stringify(message.error)));
      else request.resolve(message.result);
    });
    const command = (method, params = {}) => new Promise((resolve, reject) => {
      const id = ++nextId;
      let timer;
      pending.set(id, {
        resolve: (value) => { clearTimeout(timer); resolve(value); },
        reject: (error) => { clearTimeout(timer); reject(error); },
      });
      socket.send(JSON.stringify({ id, method, params }));
      timer = setTimeout(() => {
        if (!pending.has(id)) return;
        pending.delete(id);
        reject(new Error(`Chromium DevTools timed out during ${method}`));
      }, 5000);
    });
    const evaluate = async (expression) => {
      const response = await command("Runtime.evaluate", { expression, awaitPromise: true, returnByValue: true });
      if (response.exceptionDetails) throw new Error(response.exceptionDetails.text || "Chromium evaluation failed");
      return response.result.value;
    };

    await command("Runtime.enable");
    await evaluate(`document.open();document.write(${JSON.stringify(html)});document.close();`);
    let resultText = "pending";
    const resultDeadline = Date.now() + 10000;
    while (Date.now() < resultDeadline && resultText === "pending") {
      await wait(100);
      resultText = await evaluate('document.querySelector("#result")?.textContent || ""');
    }
    assert.notEqual(resultText, "pending", "Chromium finishes measuring the requested viewport geometry");
    result = JSON.parse(resultText);
  } catch (error) {
    primaryError = error;
  }

  const cleanupErrors = [];
  if (socket && socket.readyState < WebSocket.CLOSING) {
    try {
      socket.close();
      await Promise.race([new Promise((resolve) => socket.addEventListener("close", resolve, { once: true })), wait(500)]);
    } catch (error) {
      cleanupErrors.push(error);
    }
  }

  if (child) {
    if (process.platform === "win32") {
      const escapedBrowserPath = path.resolve(browser).replace(/'/g, "''");
      const escapedProfile = profile.replace(/'/g, "''");
      const escapedBrowser = path.basename(browser).replace(/'/g, "''");

      const profileRootPids = () => {
        const query = `$ErrorActionPreference = 'Stop'; $profile = '${escapedProfile}'; $exe = '${escapedBrowserPath}'; $name = '${escapedBrowser}'; $quotedArg = '--user-data-dir="' + $profile + '"'; $plainArg = '--user-data-dir=' + $profile; Get-CimInstance Win32_Process | Where-Object { $_.Name -eq $name -and $_.ExecutablePath -ieq $exe -and $_.CommandLine -and ($_.CommandLine.Contains($quotedArg) -or $_.CommandLine.Contains($plainArg + ' ') -or $_.CommandLine.EndsWith($plainArg)) -and $_.CommandLine -notmatch '--type=' } | ForEach-Object { $_.ProcessId }`;
        const encodedQuery = Buffer.from(query, "utf16le").toString("base64");
        const queried = spawnSync("powershell.exe", ["-NoProfile", "-NonInteractive", "-EncodedCommand", encodedQuery], { encoding: "utf8", timeout: 5000, windowsHide: true });
        if (queried.error) throw queried.error;
        if (queried.status !== 0) throw new Error(`Could not enumerate isolated Chromium processes: ${queried.stderr || queried.status}`);
        return (queried.stdout || "").split(/\r?\n/).map((line) => Number(line.trim())).filter((pid) => Number.isInteger(pid) && pid > 0);
      };

      try {
        for (const pid of profileRootPids()) {
          const killed = spawnSync("taskkill.exe", ["/PID", String(pid), "/T", "/F"], { encoding: "utf8", timeout: 5000, windowsHide: true });
          if (killed.error && killed.error.code !== "ETIMEDOUT") throw killed.error;
        }
        const remainingPids = profileRootPids();
        if (remainingPids.length) throw new Error(`Isolated Chromium process cleanup left PIDs: ${remainingPids.join(", ")}`);
        if (!childClosed) child.kill();
      } catch (error) {
        cleanupErrors.push(error);
      }
    } else {
      if (Number.isInteger(child.pid)) {
        try { process.kill(-child.pid, "SIGTERM"); } catch (error) { if (error.code !== "ESRCH") cleanupErrors.push(error); }
        await Promise.race([new Promise((resolve) => child.once("close", resolve)), wait(1500)]);
        try { process.kill(-child.pid, "SIGKILL"); } catch (error) { if (error.code !== "ESRCH") cleanupErrors.push(error); }
      }
    }
    if (!childClosed) await Promise.race([new Promise((resolve) => child.once("close", resolve)), wait(2000)]);
    if (!childClosed) {
      try { child.kill("SIGKILL"); } catch (error) { cleanupErrors.push(error); }
      await Promise.race([new Promise((resolve) => child.once("close", resolve)), wait(1000)]);
      if (!childClosed) cleanupErrors.push(new Error("Chromium launcher did not close after forced termination"));
    }
  }

  try {
    const tempRoot = path.resolve(os.tmpdir());
    const resolvedProfile = path.resolve(profile);
    const relativeProfile = path.relative(tempRoot, resolvedProfile);
    assert.ok(relativeProfile.startsWith("fluke8845a-layout-") && !relativeProfile.includes(path.sep), "Chromium profile is a direct child of the OS temp directory");
    for (let attempt = 0; fs.existsSync(resolvedProfile) && attempt < 25; attempt += 1) {
      try { fs.rmSync(resolvedProfile, { recursive: true, force: true }); } catch {}
      if (fs.existsSync(resolvedProfile)) await wait(100);
    }
    assert.equal(fs.existsSync(resolvedProfile), false, "isolated Chromium profile is removed after the geometry check");
  } catch (error) {
    cleanupErrors.push(error);
  }

  if (primaryError) {
    if (cleanupErrors.length) primaryError.message += ` (cleanup also failed: ${cleanupErrors.map((error) => error.message).join("; ")})`;
    throw primaryError;
  }
  if (cleanupErrors.length) throw new AggregateError(cleanupErrors, "Chromium geometry probe cleanup failed");
  return result;
}

test("Chromium geometry probe reports launch failures and removes its isolated profile", async () => {
  const profileNamesBefore = new Set(fs.readdirSync(os.tmpdir()).filter((name) => name.startsWith("fluke8845a-layout-")));
  const missingBrowser = path.join(os.tmpdir(), `missing-fluke-browser-${process.pid}-${Date.now()}.exe`);
  await assert.rejects(inspectChromiumDom(missingBrowser, ""), /ENOENT/);
  const leakedProfiles = fs.readdirSync(os.tmpdir()).filter((name) => name.startsWith("fluke8845a-layout-") && !profileNamesBefore.has(name));
  assert.deepEqual(leakedProfiles, []);
});

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
  assert.match(narrow[1], /button\s*\{[^}]*min-width:\s*44px[^}]*min-height:\s*44px/);
  assert.match(narrow[1], /\.text-button[^,{]*,\s*\.critical-error[^,{]*,\s*\.file-button[^,{]*[\s\S]*?min-height:\s*44px/);
  assert.match(narrow[1], /\.text-button[^}]*min-width:\s*44px/);
  assert.match(narrow[1], /\.accessible-data-table summary[^}]*min-height:\s*44px/);
  assert.match(narrow[1], /\.serial-advanced summary[^}]*min-height:\s*44px/);
  assert.match(narrow[1], /\.log-panel summary[^}]*min-height:\s*44px/);
  assert.match(css, /\.chart-toolbar\s*\{[^}]*flex:\s*0 0 auto/);
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
  const ids = ["connectionBadge", "connectionSummary", "runStatus", "readingMode", "readingValue", "sampleCount", "actualRate"];
  const positions = ids.map((id) => html.indexOf(`id=\"${id}\"`));
  assert.ok(positions.every((position) => position >= 0));
  assert.deepEqual(positions, [...positions].sort((a, b) => a - b));
});

test("Web chart keeps the loaded history filename visible and labels the live source", () => {
  const { api, elements } = createFrontend({ stubChart: false });
  api.app.modes.set("DCV", dcv);
  api.app.imported = [{ id: 1, x: 0, y: 1.25, status: "ok", unit: "V", mode: "DCV" }];
  api.app.importedFileName = "step-response.csv";
  api.drawChart();

  assert.match(element(elements, "dataSourceStatus").textContent, /step-response\.csv/);
  assert.match(element(elements, "plotRange").textContent, /step-response\.csv/);

  api.app.imported = null;
  api.app.importedFileName = "";
  api.drawChart();
  assert.equal(element(elements, "dataSourceStatus").textContent, "实时数据");
});

test("disabled Web controls explain Local mode and active acquisition locks", () => {
  const { api, elements } = createFrontend();
  api.app.modes.set("DCV", dcv);
  api.app.selectedMode = "DCV";
  api.updateStateDisplay(connectedState({
    simulation: false, control_mode: "local", status: "LOCAL本地操作",
    active_settings: { mode: "DCV", nplc: 1 },
  }));

  for (const id of ["mode", "nplc", "bandwidth", "applySpeed", "applySettings", "zero", "diagnose"]) {
    assert.equal(element(elements, id).disabled, true, `${id} is disabled in Local`);
    assert.match(element(elements, id).title, /Local/);
  }

  api.updateStateDisplay(connectedState({
    status: "连续采集中", active_settings: { mode: "DCV", nplc: 1 },
  }));
  assert.equal(element(elements, "disconnect").disabled, true);
  assert.match(element(elements, "disconnect").title, /采集中/);
});

test("Web serial controls explain that disconnecting unlocks connection settings", () => {
  const { api, elements } = createFrontend();
  api.updateStateDisplay(connectedState({ simulation: false, control_mode: "computer" }));

  for (const id of ["port", "portList", "baudrate", "bytesize", "parity", "stopbits", "eol", "flow", "serialTimeout", "scanPorts"]) {
    assert.equal(element(elements, id).disabled, true, `${id} is locked while connected`);
    assert.match(element(elements, id).title, /断开.*修改/);
  }

  api.updateStateDisplay(connectedState({ connected: false, simulation: false, status: "未连接" }));
  assert.equal(element(elements, "baudrate").disabled, false);
  assert.doesNotMatch(element(elements, "baudrate").title, /已连接.*断开/);
});

test("Web firmware fields and clear action explain disconnected, Mock, and acquisition locks", () => {
  const { api, elements } = createFrontend();
  api.updateStateDisplay(connectedState({ connected: false, simulation: false, status: "未连接" }));
  assert.equal(element(elements, "outguardSoftware").disabled, true);
  assert.match(element(elements, "outguardSoftware").title, /连接真实仪器/);

  api.updateStateDisplay(connectedState({ simulation: true, status: "MOCK 模拟器已连接" }));
  assert.match(element(elements, "outguardSoftware").title, /MOCK.*不使用/);

  api.updateStateDisplay(connectedState({
    simulation: false, status: "连续采集中", device: { serial_number: "TEST8845A001" },
  }));
  assert.match(element(elements, "outguardSoftware").title, /采集中.*停止/);
  assert.match(element(elements, "clearManualSoftware").title, /采集中.*停止/);

  api.updateStateDisplay(connectedState({
    simulation: false, status: "就绪", device: { serial_number: "TEST8845A001" },
  }));
  assert.equal(element(elements, "outguardSoftware").disabled, false);
  assert.match(element(elements, "clearManualSoftware").title, /没有.*记录/);
});

test("Web safe exports explain when a saved run is required and when acquisition must stop", () => {
  const { api, elements } = createFrontend();
  api.updateStateDisplay(connectedState({ connected: false, simulation: false, status: "未连接" }));
  for (const id of ["downloadEventsSafe", "downloadMeasurementsSafe", "downloadSharePackage"]) {
    assert.equal(element(elements, id).disabled, true);
    assert.match(element(elements, id).title, /完成.*实验/);
  }

  api.updateStateDisplay(connectedState({
    experiment_dir: "C:/experiments/run-1", last_experiment_dir: "C:/experiments/run-1",
    status: "连续采集中",
  }));
  for (const id of ["downloadEventsSafe", "downloadMeasurementsSafe", "downloadSharePackage"]) {
    assert.equal(element(elements, id).disabled, true);
    assert.match(element(elements, id).title, /停止.*采集/);
  }

  api.updateStateDisplay(connectedState({ last_experiment_dir: "C:/experiments/run-1", status: "就绪" }));
  assert.equal(element(elements, "downloadEventsSafe").disabled, false);
  assert.match(element(elements, "downloadEventsSafe").title, /原始 events\.csv 不会更改/);
});

test("fixed range tooltip updates on auto toggle and combines Local acquisition locks", async () => {
  const { api, elements } = createFrontend();
  api.app.modes.set("DCV", dcv);
  api.app.selectedMode = "DCV";
  api.populateModeOptions(dcv);
  element(elements, "autorange").checked = true;

  api.updateStateDisplay(connectedState({
    active_settings: { mode: "DCV", autorange: true, range_si: null },
  }));

  assert.equal(element(elements, "range").disabled, true);
  assert.match(element(elements, "range").title, /自动量程.*关闭.*固定量程/);

  api.updateStateDisplay(connectedState({
    connected: false, simulation: false, status: "未连接",
    active_settings: { mode: "DCV", autorange: true, range_si: null },
  }));
  assert.doesNotMatch(element(elements, "range").title, /请先连接/);

  api.app.state = connectedState({
    simulation: false, control_mode: "local", status: "连续采集中",
    active_settings: { mode: "DCV", autorange: true, range_si: null },
  });
  api.updateStateDisplay(api.app.state);
  assert.match(element(elements, "range").title, /Local/);
  assert.match(element(elements, "range").title, /采集中/);

  api.updateStateDisplay(connectedState({
    simulation: false, control_mode: "computer", status: "就绪",
    active_settings: { mode: "DCV", autorange: true, range_si: null },
  }));
  assert.equal(element(elements, "mode").disabled, false);
  assert.doesNotMatch(element(elements, "mode").title, /Local|采集中/);

  api.app.state = connectedState({ active_settings: { mode: "DCV", autorange: true, range_si: null } });
  api.updateStateDisplay(api.app.state);
  await element(elements, "autorange").dispatch("change");
  assert.match(element(elements, "range").title, /自动量程.*关闭.*固定量程/);

  element(elements, "autorange").checked = false;
  await element(elements, "autorange").dispatch("change");

  assert.equal(element(elements, "range").disabled, false);
  assert.doesNotMatch(element(elements, "range").title, /自动量程/);
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

test("wide Web empty plot shortens the chart and keeps its actions visible", async (t) => {
  const candidates = [
    process.env.CHROME_BIN,
    process.env.EDGE_BIN,
    "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe",
    "C:/Program Files/Google/Chrome/Application/chrome.exe",
  ].filter(Boolean);
  const browser = candidates.find((candidate) => fs.existsSync(candidate));
  if (!browser) return t.skip("Chromium is not installed; wide empty-plot geometry cannot be measured here");

  const css = fs.readFileSync(path.join(ROOT, "src/fluke8845a/web/app.css"), "utf8");
  const source = fs.readFileSync(path.join(ROOT, "src/fluke8845a/web/index.html"), "utf8")
    .replace('<link rel="stylesheet" href="/app.css">', `<style>${css}</style>`)
    .replace(/\s*<script[^>]*>[\s\S]*?<\/script>|\s*<script[^>]*><\/script>/g, "")
    .replace(/\s*<script[^>]*src="[^"]*"[^>]*><\/script>/g, "");
  const appScript = fs.readFileSync(path.join(ROOT, "src/fluke8845a/web/app.js"), "utf8");
  const stateTransitionStart = appScript.indexOf("function updateEmptyPlotState(");
  const stateTransitionEnd = appScript.indexOf("\nfunction drawChart(", stateTransitionStart);
  assert.ok(stateTransitionStart >= 0 && stateTransitionEnd > stateTransitionStart, "extract the actual empty-state transition from app.js");
  const stateTransition = [
    "const app = { state: null, chartCleared: false };",
    "const el = (id) => document.getElementById(id);",
    "const validPoints = () => [];",
    appScript.slice(stateTransitionStart, stateTransitionEnd),
  ].join("\n");
  const page = source.replace(
    "</body>",
    `<script>${stateTransition}</script></body>`,
  );
  const serializedPage = JSON.stringify(page).replace(/<\/script/gi, "<\\/script");
  const probe = `<!doctype html><meta charset="utf-8"><pre id="result">pending</pre><script>
    const frame = document.createElement("iframe");
    frame.style.cssText = "display:block;border:0;width:1440px;height:900px";
    const report = (value) => { document.getElementById("result").textContent = JSON.stringify(value); };
    frame.addEventListener("load", () => requestAnimationFrame(() => {
      try {
        const doc = frame.contentDocument;
        const updateEmptyPlotState = frame.contentWindow.updateEmptyPlotState;
        if (typeof updateEmptyPlotState !== "function") throw new Error("frontend empty-state transition is unavailable");
        const chart = doc.querySelector(".chart-panel");
        const fullHeight = chart.getBoundingClientRect().height;
        updateEmptyPlotState(false);
        requestAnimationFrame(() => {
          try {
            const actions = ["emptyOpenSettings", "emptyConnectMock"].map((id) => {
              const node = doc.getElementById(id);
              const rect = node.getBoundingClientRect();
              return getComputedStyle(node).display !== "none" && rect.width > 0 && rect.height > 0;
            });
            report({
              fullHeight,
              emptyHeight: chart.getBoundingClientRect().height,
              actions,
              emptyTitle: doc.getElementById("emptyTitle").textContent,
            });
          } catch (error) {
            report({ error: String(error), stack: error?.stack || "" });
          }
        });
      } catch (error) {
        report({ error: String(error), stack: error?.stack || "" });
      }
    }));
    frame.srcdoc = ${serializedPage};
    document.body.appendChild(frame);
  </script>`;
  const metrics = await inspectChromiumDom(browser, probe);
  assert.equal(metrics.error, undefined, `frontend empty-state geometry probe runs: ${JSON.stringify(metrics)}`);
  assert.ok(metrics.emptyHeight >= 220 && metrics.emptyHeight <= 320, `wide empty chart is compact: ${JSON.stringify(metrics)}`);
  assert.ok(metrics.emptyHeight < metrics.fullHeight * 0.6, `wide empty chart is shorter than the data layout: ${JSON.stringify(metrics)}`);
  assert.equal(metrics.emptyTitle, "图表还没有数据", "geometry is measured after the real frontend empty-state transition");
  assert.deepEqual(metrics.actions, [true, true], "settings and Mock connection remain available in the empty chart");
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

test("ACV and ACI settings comparison marks analog filtering not applicable", async (t) => {
  for (const [value, label, unit] of [["ACV", "交流电压", "V"], ["ACI", "交流电流", "A"]]) {
    await t.test(value, () => {
      const { api, elements } = createFrontend();
      const mode = {
        ...dcv, value, label, unit, nplc: [], bandwidths: [3, 20, 200],
        analog_filter: false, digital_filter: false,
      };
      api.app.modes.set(value, mode);
      api.app.selectedMode = value;
      element(elements, "mode").value = value;
      api.updateStateDisplay(connectedState({ active_settings: {
        mode: value, autorange: true, range_si: null, ac_bandwidth_hz: 20,
        analog_filter: false, autozero: true,
      } }));

      const comparison = element(elements, "settingsComparison").innerHTML;
      const analogRow = comparison.match(/<span>模拟滤波<\/span>(.*?)<span>自动零点<\/span>/);
      assert.ok(analogRow, "analog filter comparison row is present");
      assert.match(analogRow[1], /<span>不适用<\/span><span class="">不适用<\/span>/);
      assert.doesNotMatch(analogRow[1], /<span>关<\/span>/);
    });
  }
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

test("Web does not block NPLC choices from obsolete digital-filter guard metadata", () => {
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

  assert.deepEqual(nplc.options.map((option) => option.disabled), [false, false, false]);
  assert.ok(!nplc.title);
});

test("mode changes keep NPLC choices enabled without firmware guard metadata", () => {
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
    [false, false, false, false, false],
  );
});

test("digital filter is available for slow DC settings and clears when NPLC becomes fast", async () => {
  const { api, elements } = createFrontend();
  api.app.modes.set("DCV", dcv);
  api.app.selectedMode = "DCV";
  api.populateModeOptions(dcv);
  element(elements, "nplc").value = "10";
  await element(elements, "nplc").dispatch("change");

  const filter = element(elements, "digitalFilter");
  assert.equal(filter.disabled, false);
  assert.match(filter.title, /OutG SW.*2\.0/);
  filter.checked = true;
  element(elements, "nplc").value = "1";
  await element(elements, "nplc").dispatch("change");

  assert.equal(filter.checked, false, "NPLC at or below one cannot retain the filter selection");
  assert.equal(filter.disabled, true);
  assert.match(filter.title, /NPLC.*10.*100/);
});

test("known pre-2.0 OutG firmware disables digital filter selection", () => {
  const { api, elements } = createFrontend();
  api.app.modes.set("DCV", dcv);
  api.app.selectedMode = "DCV";
  api.populateModeOptions(dcv);
  element(elements, "nplc").value = "10";
  api.updateStateDisplay(connectedState({ simulation: false, device: {
    range_firmware: "1.9.0.0", range_firmware_source: "面板核对的 OutG SW",
  } }));

  assert.equal(element(elements, "digitalFilter").disabled, true);
  assert.match(element(elements, "digitalFilter").title, /OutG SW.*早于 2\.0/);
});

test("empty settings comparison stays readable in Chinese before a mode is available", () => {
  const { api, elements } = createFrontend();
  api.app.modes.clear();
  api.app.selectedMode = "";
  element(elements, "mode").value = "";

  api.renderSettingsComparison(connectedState());

  const message = element(elements, "settingsComparison").textContent;
  assert.match(message, /请先选择测量模式/);
  assert.doesNotMatch(message, /[\uac00-\ud7af]/);
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

test("Web reports digital filtering as unsupported on verified pre-2.0 OutG SW", () => {
  const { api, elements } = createFrontend();
  api.app.modes.set("DCV", dcv);
  api.app.selectedMode = "DCV";
  element(elements, "mode").value = "DCV";
  api.updateStateDisplay(connectedState({ simulation: false, active_settings: {
    mode: "DCV", autorange: true, range_si: null, nplc: 10,
    analog_filter: false, digital_filter: false, digital_filter_state: "unsupported", autozero: true,
  } }));

  const comparison = element(elements, "settingsComparison").innerHTML;
  const digitalRow = comparison.match(/<span>数字滤波<\/span>(.*?)<span>自动零点<\/span>/);
  assert.ok(digitalRow);
  assert.match(digitalRow[1], /<span>OutG SW 版本不支持<\/span><span class="">OutG SW 版本不支持<\/span>/);
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
  assert.match(labels, /电阻 \(kΩ\)/);
  assert.match(labels, /2997/);
});

test("manual panel firmware changes save source time and can be cleared", async () => {
  const { api, elements, context, storage } = createFrontend();
  api.app.activeDeviceSerial = "TEST8845A001";
  api.app.state = connectedState({ simulation: false, device: { serial_number: "TEST8845A001" } });
  element(elements, "outguardSoftware").value = "2.10.882.24";
  element(elements, "inguardSoftware").value = "ad300210";
  mockSuccessfulCommand(context);
  await api.saveManualSoftware();
  let saved = JSON.parse(storage.get("8845a-front-panel-software"))["TEST8845A001"];
  assert.equal(saved.source, "仪表面板人工核对");
  assert.ok(Number.isFinite(Date.parse(saved.updated_at)));
  assert.match(element(elements, "manualSoftwareInfo").textContent, /更新时间/);
  await api.clearManualSoftware();
  assert.equal(JSON.parse(storage.get("8845a-front-panel-software"))["TEST8845A001"], undefined);
  assert.match(element(elements, "manualSoftwareInfo").textContent, /尚无记录/);
});

test("IDN version candidates are explicitly unverified and separate from manual OutG and InG records", () => {
  const html = fs.readFileSync(path.join(ROOT, "src/fluke8845a/web/index.html"), "utf8");
  assert.match(html, /id="idnFirmwareInfo"/);
  const { api, elements, storage } = createFrontend();
  storage.set("8845a-front-panel-software", JSON.stringify({ "TEST8845A001": {
    outguard_sw: "2.10.882.24", inguard_sw: "ad300210", source: "仪表面板人工核对",
  } }));
  api.updateStateDisplay(connectedState({ simulation: false, device: {
    idn: "FLUKE,8845A,TEST8845A001,2.0.1", serial_number: "TEST8845A001",
    idn_version_candidate: "2.0.1", idn_revision: "2.0.1",
  } }));

  const automatic = element(elements, "idnFirmwareInfo").textContent;
  const manual = element(elements, "manualSoftwareInfo").textContent;
  assert.match(automatic, /IDN\?.*候选.*2\.0\.1.*未验证.*OutG SW/);
  assert.match(automatic, /DCI.*OutG SW.*ACI.*未明确.*OutG SW.*InG SW.*不根据.*自动拒绝/);
  assert.match(manual, /仪表面板人工核对/);
  assert.match(element(elements, "outguardSoftware").value, /2\.10\.882\.24/);
  assert.match(element(elements, "inguardSoftware").value, /ad300210/);
  assert.doesNotMatch(automatic, /ad300210|2\.10\.882\.24/);

  api.updateStateDisplay(connectedState({ simulation: false, device: {
    idn: "FLUKE,8845A,TEST8845A001,02/14/08-08:54", serial_number: "TEST8845A001",
    idn_version_candidate: null, idn_revision: "02/14/08-08:54",
  } }));
  const unversionedRevision = element(elements, "idnFirmwareInfo").textContent;
  assert.match(unversionedRevision, /修订字段.*02\/14\/08-08:54.*不将.*作为.*固件/);
  assert.match(unversionedRevision, /DCI.*OutG SW.*ACI.*未明确.*OutG SW.*InG SW.*不根据.*自动拒绝/);
  assert.match(element(elements, "manualSoftwareInfo").textContent, /仪表面板人工核对/);
});

test("clearing manual panel software asks for confirmation before sending or erasing", async () => {
  const { api, elements, context, storage } = createFrontend();
  const profile = { outguard_sw: "2.10.882.24", inguard_sw: "ad300210", source: "仪表面板人工核对", updated_at: new Date().toISOString() };
  storage.set("8845a-front-panel-software", JSON.stringify({ "TEST8845A001": profile }));
  api.app.activeDeviceSerial = "TEST8845A001";
  api.app.state = connectedState({ simulation: false, device: { serial_number: "TEST8845A001" } });
  element(elements, "outguardSoftware").value = "2.10.882.24";
  context.window.confirm = () => false;
  let commands = 0;
  context.fetch = async () => { commands += 1; return { ok: true, json: async () => ({ success: true }) }; };

  await api.clearManualSoftware();

  assert.equal(commands, 0);
  assert.equal(JSON.parse(storage.get("8845a-front-panel-software"))["TEST8845A001"].outguard_sw, "2.10.882.24");
  assert.equal(element(elements, "outguardSoftware").value, "2.10.882.24");
});

test("saving a new manual software profile preserves all existing device records", async () => {
  const { api, elements, context, storage } = createFrontend();
  const profiles = Object.fromEntries(Array.from({ length: 105 }, (_, index) => [`serial-${index}`, {
    outguard_sw: `2.${index}`, inguard_sw: "", source: "仪表面板人工核对", updated_at: new Date().toISOString(),
  }]));
  storage.set("8845a-front-panel-software", JSON.stringify(profiles));
  api.app.activeDeviceSerial = "TEST8845A001";
  api.app.state = connectedState({ simulation: false, device: { serial_number: "TEST8845A001" } });
  element(elements, "outguardSoftware").value = "2.10.882.24";
  mockSuccessfulCommand(context);

  await api.saveManualSoftware();

  const saved = JSON.parse(storage.get("8845a-front-panel-software"));
  assert.equal(Object.keys(saved).length, 106);
  assert.equal(saved["serial-104"].outguard_sw, "2.104");
  assert.equal(saved["TEST8845A001"].outguard_sw, "2.10.882.24");
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

function createFrontend({ preferences = null, stubChart = true, storage: providedStorage = null, lockManager: providedLockManager = null, missingElementIds = [] } = {}) {
  const elements = new Map();
  const missing = new Set(missingElementIds);
  const storage = providedStorage || new Map();
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
  const lockManager = providedLockManager || {
    request: async (_name, _options, callback) => callback({ name: _name }),
  };
  const clipboard = { text: "", async writeText(text) { this.text = text; } };
  const document = {
    listeners: new Map(),
    getElementById(id) {
      if (missing.has(id)) return null;
      if (!elements.has(id)) elements.set(id, new FakeElement(id));
      return elements.get(id);
    },
    querySelector() { return new FakeElement(); },
    createDocumentFragment() { return new FakeElement("fragment"); },
    createElement(tag) { return new FakeElement(tag); },
    addEventListener(type, listener) {
      const listeners = this.listeners.get(type) || [];
      listeners.push(listener);
      this.listeners.set(type, listeners);
    },
    async dispatch(type, event = {}) {
      const dispatched = { target: this, ...event };
      await Promise.all((this.listeners.get(type) || []).map((listener) => listener(dispatched)));
      return dispatched;
    },
  };
  const context = vm.createContext({
    document,
    window: { confirm: () => true, location: { assign() {} }, addEventListener() {} },
    localStorage: {
      getItem: (key) => storage.get(key) || null,
      setItem: (key, value) => storage.set(key, String(value)),
      removeItem: (key) => storage.delete(key),
    },
    navigator: { clipboard, locks: lockManager },
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
  vm.runInContext(`${source}\n;globalThis.__frontend = { app, ui, command, handleCommandRecoveryStorage, acknowledgeUncertainCommands, restoreUnresolvedCommands, updateStateDisplay, pollState, scanPorts, startPayload, startContinuous, startSingle, populateModeOptions, bindControls, chooseDuplicateNameAction, connectReal, connectMock, drawChart, buildPixelPointIndex, nearestPoint, appendSampleRows, saveManualSoftware, clearManualSoftware, renderSettingsComparison, refreshChartOverview, clearChartOverview, chartPointsForPlot, refreshExperimentFiles: typeof refreshExperimentFiles === "function" ? refreshExperimentFiles : null, downloadLatestCsv: typeof downloadLatestCsv === "function" ? downloadLatestCsv : null, scheduleChartRedraw: typeof scheduleChartRedraw === "function" ? scheduleChartRedraw : null, downsampleForRender: typeof downsampleForRender === "function" ? downsampleForRender : null, classifyLog: typeof classifyLog === "function" ? classifyLog : null, filterLogRows: typeof filterLogRows === "function" ? filterLogRows : null, formatLogsText: typeof formatLogsText === "function" ? formatLogsText : null, loadHistory, requestChartRange, loadHistoryEvents: typeof loadHistoryEvents === "function" ? loadHistoryEvents : null, restorePreferences, updateStats, renderEvents, focusEvent, updateAccessiblePointTable: typeof updateAccessiblePointTable === "function" ? updateAccessiblePointTable : null, clearPreferences: typeof clearPreferences === "function" ? clearPreferences : null, localExperimentName: typeof localExperimentName === "function" ? localExperimentName : null, copyAccessiblePointTable: typeof copyAccessiblePointTable === "function" ? copyAccessiblePointTable : null, updateEmptyPlotState: typeof updateEmptyPlotState === "function" ? updateEmptyPlotState : null, createPlotExportCanvas: typeof createPlotExportCanvas === "function" ? createPlotExportCanvas : null, copyExperimentPath: typeof copyExperimentPath === "function" ? copyExperimentPath : null, experimentNameError: typeof experimentNameError === "function" ? experimentNameError : null };`, context);
  if (stubChart) vm.runInContext("drawChart = () => {}; updateStats = () => {};", context);
  return { api: context.__frontend, elements, context, storage, clipboard, animationFrames };
}

function element(elements, id) {
  if (!elements.has(id)) elements.set(id, new FakeElement(id));
  return elements.get(id);
}

function flushAnimationFrames(animationFrames) {
  for (const callback of animationFrames.splice(0)) callback();
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

test("DCV state polling tolerates the absent AC bandwidth control", async () => {
  const { api, elements, context } = createFrontend({ missingElementIds: ["bandwidth"] });
  api.app.modes.set("DCV", dcv);
  api.app.selectedMode = "DCV";
  api.populateModeOptions(dcv);
  context.fetch = async () => ({ ok: true, json: async () => connectedState({ connected: false }) });

  await api.pollState();

  assert.equal(element(elements, "serviceStatus").textContent, "Web 服务在线");
  assert.notEqual(element(elements, "serviceStatus").textContent.includes("Web 服务中断"), true);
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

test("state polling batches full chart redraw into one animation frame", async () => {
  const { api, context, animationFrames } = createFrontend({ stubChart: false });
  vm.runInContext("globalThis.chartDrawCalls = 0; drawChart = () => { globalThis.chartDrawCalls += 1; };", context);
  context.fetch = async () => ({
    ok: true,
    json: async () => connectedState({
      generation: 1, sample_buffer_start: 1, samples: [], logs: [], log_seq: 0, events: [],
    }),
  });

  await api.pollState();

  assert.equal(context.chartDrawCalls, 0, "polling does not redraw synchronously");
  assert.equal(animationFrames.length, 1, "polling queues one chart redraw frame");
  assert.equal(api.app.chartFramePending, true);
  animationFrames.shift()();
  assert.equal(context.chartDrawCalls, 1);
  assert.equal(api.app.chartFramePending, false);
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
  assert.match(element(elements, "mockScenario").title, /断开.*重新连接/);
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
    device: { serial_number: "TEST8845A001" },
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

test("settings dialog keeps its sections balanced in a responsive two-column layout", () => {
  const html = fs.readFileSync(path.join(ROOT, "src/fluke8845a/web/index.html"), "utf8");
  const css = fs.readFileSync(path.join(ROOT, "src/fluke8845a/web/app.css"), "utf8");
  for (const section of ["sectionSerial", "sectionMeasurement", "sectionExperiment", "sectionAnalysis", "sectionDiagnostics"]) {
    assert.match(html, new RegExp(`href="#${section}"`));
    assert.match(html, new RegExp(`id="${section}"`));
  }
  assert.match(html, /class="dialog-nav"[^>]*aria-label="设置章节"/);
  assert.match(html, /<div class="settings-column settings-column-left">[\s\S]*<section id="sectionSerial" class="panel instrument-panel">[\s\S]*<section id="sectionExperiment" class="panel experiment-panel">/);
  assert.match(html, /<div class="settings-column settings-column-right">[\s\S]*<section id="sectionMeasurement" class="panel measurement-panel">[\s\S]*id="mode"[\s\S]*<section id="sectionAnalysis" class="analysis-panel">/);
  assert.match(html, /<section id="sectionMeasurement" class="panel measurement-panel">[\s\S]*id="mode"/);
  assert.doesNotMatch(html, /settings-side-stack/);
  assert.match(html, /<div class="settings-grid">[\s\S]*<div class="settings-column settings-column-left">[\s\S]*<div class="settings-column settings-column-right">[\s\S]*<details id="sectionDiagnostics"/);
  assert.match(html, /class="dialog-footer"[\s\S]*id="applySettings"[\s\S]*id="applySpeed"[\s\S]*id="closeSettings"/);
  assert.match(css, /\.dialog-footer\s*\{[^}]*flex:\s*0 0 auto/);
  assert.match(css, /\.settings-scroll\s*\{[^}]*flex:\s*1 1 auto/);
  assert.match(css, /\.settings-grid\s*\{[^}]*grid-template-columns:\s*repeat\(2,\s*minmax\(0,1fr\)\)/);
  assert.match(css, /\.settings-column\s*\{[^}]*display:\s*flex[^}]*flex-direction:\s*column[^}]*gap:\s*13px/);
  assert.match(css, /\.log-panel\s*\{[^}]*grid-column:\s*1\s*\/\s*-1/);
  assert.match(css, /\.settings-grid\s*\{\s*grid-template-columns:\s*1fr;\s*grid-template-areas:\s*"serial"\s*"measurement"\s*"experiment"\s*"analysis"\s*"diagnostics"/);
  assert.match(css, /\.settings-column-left\s*,\s*\.settings-column-right\s*\{\s*display:\s*contents/);
  assert.match(css, /\.instrument-panel\s*\{\s*grid-area:\s*serial/);
  assert.match(css, /\.measurement-panel\s*\{\s*grid-area:\s*measurement/);
  assert.match(css, /\.experiment-panel\s*\{\s*grid-area:\s*experiment/);
  assert.match(css, /\.analysis-panel\s*\{\s*grid-area:\s*analysis/);
  assert.match(css, /\.log-panel\s*\{\s*grid-area:\s*diagnostics/);
  const ids = [...html.matchAll(/\bid="([^"]+)"/g)].map((match) => match[1]);
  assert.equal(new Set(ids).size, ids.length, "page element IDs remain unique after moving footer actions");
});

test("experiment name hint explains explicit duplicate-name choices", () => {
  const html = fs.readFileSync(path.join(ROOT, "src/fluke8845a/web/index.html"), "utf8");

  assert.doesNotMatch(html, /同名实验自动使用独立时间戳目录/);
  assert.match(
    html,
    /发现同名实验时[\s\S]{0,100}覆盖（先备份）[\s\S]{0,100}新建独立目录[\s\S]{0,100}修改名称[\s\S]{0,100}取消[\s\S]{0,40}不会自动覆盖/,
  );
});

test("settings cards stack independently in desktop columns and reflow at 820 px in Chromium", async (t) => {
  const candidates = [
    process.env.CHROME_BIN,
    process.env.EDGE_BIN,
    "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe",
    "C:/Program Files/Google/Chrome/Application/chrome.exe",
  ].filter(Boolean);
  const browser = candidates.find((candidate) => fs.existsSync(candidate));
  if (!browser) return t.skip("Chromium is not installed; settings-card geometry cannot be measured here");

  const css = fs.readFileSync(path.join(ROOT, "src/fluke8845a/web/app.css"), "utf8");
  const source = fs.readFileSync(path.join(ROOT, "src/fluke8845a/web/index.html"), "utf8")
    .replace('<link rel="stylesheet" href="/app.css">', `<style>${css}</style>`)
    .replace(/\s*<script[^>]*>[\s\S]*?<\/script>|\s*<script[^>]*><\/script>/g, "")
    .replace(/\s*<script[^>]*src="[^"]*"[^>]*><\/script>/g, "");
  const probe = `<!doctype html><meta charset="utf-8"><pre id="result">pending</pre><script>
    const source = ${JSON.stringify(source)};
    const widths = [1440, 1000, 821, 820];
    const frames = widths.map((width) => {
      const frame = document.createElement("iframe");
      frame.style.cssText = "display:block;border:0;width:" + width + "px;height:1000px";
      frame.srcdoc = source;
      document.body.appendChild(frame);
      return { width, frame };
    });
    Promise.all(frames.map(({ width, frame }) => new Promise((resolve) => frame.addEventListener("load", () => {
      const doc = frame.contentDocument;
      const dialog = doc.querySelector("#settingsDialog");
      dialog.showModal();
      const columnLeft = doc.querySelector(".settings-column-left");
      const columnRight = doc.querySelector(".settings-column-right");
      if (!columnLeft || !columnRight) {
        resolve({ width, error: "settings cards are not grouped into independent desktop columns" });
        return;
      }
      const rect = (id) => {
        const value = doc.querySelector("#" + id).getBoundingClientRect();
        return { left: value.left, right: value.right, top: value.top, bottom: value.bottom, width: value.width, height: value.height };
      };
      const grid = doc.querySelector(".settings-grid");
      const scroll = doc.querySelector(".settings-scroll");
      resolve({
        width,
        columns: getComputedStyle(grid).gridTemplateColumns.trim().split(/\\s+/).length,
        gridRect: (() => { const r = grid.getBoundingClientRect(); return { left: r.left, right: r.right, top: r.top, bottom: r.bottom }; })(),
        sections: Object.fromEntries(["sectionSerial", "sectionMeasurement", "sectionExperiment", "sectionAnalysis", "sectionDiagnostics"].map((id) => [id, rect(id)])),
        desktopColumns: [columnLeft, columnRight].map((column) => {
          const r = column.getBoundingClientRect(); return { left: r.left, right: r.right, top: r.top, bottom: r.bottom };
        }),
        scroll: { height: scroll.clientHeight, scrollHeight: scroll.scrollHeight, width: scroll.clientWidth, scrollWidth: scroll.scrollWidth },
        dialog: rect("settingsDialog"),
      });
    })))).then((metrics) => { document.getElementById("result").textContent = JSON.stringify(metrics); });
  </script>`;
  const metrics = await inspectChromiumDom(browser, probe);
  assert.equal(metrics.length, 4);
  for (const layout of metrics) {
    assert.equal(layout.error, undefined, `${layout.width}px settings layout: ${layout.error || ""}`);
    const { sectionSerial, sectionMeasurement, sectionExperiment, sectionAnalysis, sectionDiagnostics } = layout.sections;
    if (layout.width > 820) {
      assert.equal(layout.columns, 2, `${layout.width}px keeps two settings columns`);
      assert.ok(Math.abs(layout.desktopColumns[0].left - sectionSerial.left) <= 1);
      assert.ok(Math.abs(layout.desktopColumns[0].left - sectionExperiment.left) <= 1);
      assert.ok(Math.abs(layout.desktopColumns[1].left - sectionMeasurement.left) <= 1);
      assert.ok(Math.abs(layout.desktopColumns[1].left - sectionAnalysis.left) <= 1);
      assert.ok(Math.abs((sectionSerial.bottom + 13) - sectionExperiment.top) <= 1, `${layout.width}px serial and experiment cards have only the intended column gap`);
      assert.ok(Math.abs((sectionMeasurement.bottom + 13) - sectionAnalysis.top) <= 1, `${layout.width}px measurement and analysis cards have only the intended column gap`);
      assert.ok(layout.desktopColumns[0].right < layout.desktopColumns[1].left);
      assert.ok(sectionDiagnostics.top >= Math.max(layout.desktopColumns[0].bottom, layout.desktopColumns[1].bottom) + 12);
    } else {
      assert.equal(layout.columns, 1, `${layout.width}px switches to one column at the breakpoint`);
      const ordered = [sectionSerial, sectionMeasurement, sectionExperiment, sectionAnalysis, sectionDiagnostics];
      assert.ok(ordered.every((section, index) => index === 0 || section.top > ordered[index - 1].top));
      assert.ok(ordered.every((section) => Math.abs(section.left - sectionSerial.left) <= 1));
    }
    assert.ok(layout.scroll.scrollHeight > layout.scroll.height, `${layout.width}px settings remain vertically scrollable`);
    assert.ok(layout.scroll.scrollWidth <= layout.scroll.width, `${layout.width}px settings do not overflow horizontally`);
    assert.ok(layout.dialog.right <= layout.width + 1, `${layout.width}px dialog stays inside the viewport`);
  }
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

test("cleared Web chart offers a direct new-measurement action that opens experiment settings", async () => {
  const { api, elements } = createFrontend();
  api.app.chartCleared = true;
  api.updateEmptyPlotState(false);
  assert.equal(element(elements, "emptyNewMeasurement").classList.contains("hidden"), false);
  assert.equal(element(elements, "emptyOpenSettings").classList.contains("hidden"), true);
  assert.match(element(elements, "emptyDetail").textContent, /旧实验 CSV 保留/);
  assert.match(element(elements, "emptyDetail").textContent, /无需先清空/);

  api.bindControls();
  await element(elements, "emptyNewMeasurement").dispatch("click");

  assert.equal(element(elements, "settingsDialog").showModalCalled, true);
  assert.equal(element(elements, "experimentName").focusCalled, true);
  assert.equal(element(elements, "experimentName").selectCalled, true);
});

test("new measurement action opens experiment setup without clearing the old chart first", async () => {
  const { api, elements } = createFrontend();
  const html = fs.readFileSync(path.join(ROOT, "src/fluke8845a/web/index.html"), "utf8");
  const actionBar = html.indexOf('<section class="action-bar"');
  const newMeasurement = html.indexOf('id="newMeasurement"');
  const settingsDialog = html.indexOf('<dialog id="settingsDialog"');
  assert.ok(actionBar >= 0 && newMeasurement > actionBar && newMeasurement < settingsDialog);
  assert.match(html, /新建测量：点“新建测量”→ 填名称 → 点“单次测量”或“开始连续采集”。新测量开始后自动换图，旧 CSV 保留/);
  assert.match(html, /id="newMeasurement"[^>]*class="button button-primary"[^>]*>新建测量</);
  assert.match(html, /id="clearPlot"[^>]*>清空图表/);
  assert.match(html, /id="singleFromSettings"[^>]*>单次测量/);
  assert.match(html, /id="startFromSettings"[^>]*>开始连续采集/);

  api.app.points = [{ x: 1, y: 2, status: "ok" }];
  api.bindControls();
  await element(elements, "newMeasurement").dispatch("click");

  assert.equal(element(elements, "settingsDialog").showModalCalled, true);
  assert.equal(element(elements, "experimentName").focusCalled, true);
  assert.equal(element(elements, "experimentName").selectCalled, true);
  assert.equal(api.app.points.length, 1);
});

test("settings window starts a new continuous measurement directly and closes after acceptance", async () => {
  const { api, elements, context } = createFrontend();
  api.app.modes.set("DCV", dcv);
  api.app.selectedMode = "DCV";
  api.app.state = connectedState();
  api.updateStateDisplay(api.app.state);
  element(elements, "experimentName").value = "新一轮测量";
  const requests = mockSuccessfulCommand(context);
  api.bindControls();

  await element(elements, "newMeasurement").dispatch("click");
  await element(elements, "startFromSettings").dispatch("click");

  const commands = requests.map(({ options }) => JSON.parse(options.body));
  assert.deepEqual(commands.map(({ action }) => action), ["preflight", "start"]);
  assert.equal(commands[0].payload.name, "新一轮测量");
  assert.equal(commands[1].payload.name, "新一轮测量");
  assert.equal(element(elements, "settingsDialog").closeCalled, true);
});

test("settings window starts a single measurement through the existing preflight", async () => {
  const { api, elements, context } = createFrontend();
  api.app.modes.set("DCV", dcv);
  api.app.selectedMode = "DCV";
  api.app.state = connectedState();
  api.updateStateDisplay(api.app.state);
  element(elements, "experimentName").value = "单次重测";
  const requests = mockSuccessfulCommand(context);
  api.bindControls();

  await element(elements, "newMeasurement").dispatch("click");
  await element(elements, "singleFromSettings").dispatch("click");

  const commands = requests.map(({ options }) => JSON.parse(options.body));
  assert.deepEqual(commands.map(({ action }) => action), ["preflight", "single"]);
  assert.equal(commands[1].payload.name, "单次重测");
  assert.equal(element(elements, "settingsDialog").closeCalled, true);
});

test("settings start actions match the main controls' connection and acquisition locks", () => {
  const { api, elements } = createFrontend();
  api.app.modes.set("DCV", dcv);
  api.app.selectedMode = "DCV";
  element(elements, "experimentName").value = "状态联动检查";
  element(elements, "startFromSettings").disabled = true;
  element(elements, "singleFromSettings").disabled = true;

  api.updateStateDisplay(connectedState({ connected: false, simulation: false, control_mode: "disconnected" }));
  assert.equal(element(elements, "startFromSettings").disabled, true);
  assert.equal(element(elements, "singleFromSettings").disabled, true);

  api.updateStateDisplay(connectedState({ status: "等待开始" }));
  assert.equal(element(elements, "startFromSettings").disabled, false);
  assert.equal(element(elements, "singleFromSettings").disabled, false);

  api.updateStateDisplay(connectedState({ simulation: false, control_mode: "local" }));
  assert.equal(element(elements, "startFromSettings").disabled, true);
  assert.equal(element(elements, "singleFromSettings").disabled, true);

  api.updateStateDisplay(connectedState({ status: "连续采集中" }));
  assert.equal(element(elements, "startFromSettings").disabled, true);
  assert.equal(element(elements, "singleFromSettings").disabled, true);
});

test("settings window stays open when the new-measurement preflight rejects", async () => {
  const { api, elements, context } = createFrontend();
  api.app.modes.set("DCV", dcv);
  api.app.selectedMode = "DCV";
  api.app.state = connectedState();
  api.updateStateDisplay(api.app.state);
  element(elements, "experimentName").value = "新一轮测量";
  const requests = [];
  context.fetch = async (_url, options = {}) => {
    requests.push(JSON.parse(options.body));
    return { ok: true, json: async () => ({ success: false, message: "预检拒绝" }) };
  };
  api.bindControls();

  await element(elements, "newMeasurement").dispatch("click");
  await element(elements, "singleFromSettings").dispatch("click");

  assert.deepEqual(requests.map(({ action }) => action), ["preflight"]);
  assert.notEqual(element(elements, "settingsDialog").closeCalled, true);
  assert.match(element(elements, "preflightSummary").textContent, /预检未通过/);
});

test("rejected start command explains that no new measurement started and preserves the old chart", async () => {
  const { api, elements, context } = createFrontend();
  api.app.modes.set("DCV", dcv);
  api.app.selectedMode = "DCV";
  api.app.state = connectedState();
  api.app.points = [{ id: 4, x: 3, y: 1.25, status: "ok", unit: "V" }];
  api.app.lastSample = 4;
  api.app.generation = 2;
  api.updateStateDisplay(api.app.state);
  element(elements, "experimentName").value = "新测量启动被拒绝";
  const actions = [];
  context.fetch = async (_url, options) => {
    const { action } = JSON.parse(options.body);
    actions.push(action);
    return { ok: true, json: async () => action === "preflight"
      ? { success: true, message: "预检通过", checks: ["仪器已连接", "配置已回读", "保存目录可写"] }
      : { success: false, message: "启动请求被服务拒绝" } };
  };
  api.bindControls();

  await element(elements, "newMeasurement").dispatch("click");
  await element(elements, "singleFromSettings").dispatch("click");

  assert.deepEqual(actions, ["preflight", "single"]);
  assert.notEqual(element(elements, "settingsDialog").closeCalled, true);
  assert.equal(api.app.points.length, 1);
  assert.equal(api.app.points[0].y, 1.25);
  assert.match(element(elements, "preflightSummary").textContent, /采集未启动/);
});

test("clear-plot action clears only the displayed curve and preserves the sample cursor", async () => {
  const { api, elements, context } = createFrontend();
  const requests = [];
  context.fetch = async (...args) => { requests.push(args); throw new Error("unexpected request"); };
  api.app.state = connectedState({ generation: 7, status: "已停止", events: [] });
  api.app.generation = 7;
  api.app.lastSample = 12;
  api.app.points = [{ id: 12, x: 5, y: 1.25, status: "ok", unit: "V" }];
  api.app.chartOverview = [{ id: 1, x: 0, y: 1, status: "ok", unit: "V" }];
  api.app.chartOverviewJobId = "overview-7";
  api.bindControls();

  await element(elements, "clearPlot").dispatch("click");

  assert.equal(api.app.chartCleared, true);
  assert.equal(api.app.points.length, 0);
  assert.equal(api.chartPointsForPlot().length, 0);
  assert.equal(api.app.lastSample, 12, "clearing the view must not make the next poll reload old samples");
  assert.equal(api.app.chartOverview.length, 0);
  assert.equal(api.app.chartOverviewJobId, null);
  assert.equal(requests.length, 0, "clearing the view does not issue a server command or delete saved data");
  assert.match(element(elements, "toast").textContent, /measurements\.csv.*events\.csv.*保留/);
});

test("clear-plot action is disabled during acquisition and preserves the curve", async () => {
  const { api, elements } = createFrontend();
  api.app.points = [{ id: 1, x: 0, y: 1.25, status: "ok", unit: "V" }];
  api.bindControls();
  api.updateStateDisplay(connectedState({ status: "连续采集中" }));

  assert.equal(element(elements, "clearPlot").disabled, true);
  await element(elements, "clearPlot").dispatch("click");

  assert.equal(api.app.points.length, 1);
  assert.match(element(elements, "toast").textContent, /先停止并保存/);
});

test("a new experiment generation makes the live curve visible again after clearing", async () => {
  const { api, context } = createFrontend();
  api.app.generation = 4;
  api.app.chartCleared = true;
  api.app.lastSample = 12;
  api.app.state = connectedState({ generation: 4 });
  context.fetch = async () => ({
    ok: true,
    json: async () => connectedState({
      generation: 5,
      sample_buffer_start: 1,
      samples: [{ sample_index: 1, elapsed_s: 0, value_si: 1.1, status: "ok", unit_si: "V", mode: "DCV" }],
      logs: [], log_seq: 0, events: [],
    }),
  });

  await api.pollState();

  assert.equal(api.app.chartCleared, false);
  assert.equal(api.app.points.length, 1);
  assert.equal(api.app.points[0].y, 1.1);
  assert.equal(api.app.lastSample, 1);
});

test("new measurement action is locked during an active acquisition", async () => {
  const { api, elements } = createFrontend();
  api.bindControls();
  api.updateStateDisplay(connectedState({ status: "连续采集中" }));

  assert.equal(element(elements, "newMeasurement").disabled, true);
  assert.match(element(elements, "newMeasurement").title, /先停止并保存当前实验/);
  await element(elements, "newMeasurement").dispatch("click");
  assert.notEqual(element(elements, "settingsDialog").showModalCalled, true);
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

test("Mock preflight status does not imply a physical wiring check", async () => {
  const { api, elements, context } = createFrontend();
  api.app.modes.set("DCV", dcv);
  api.app.selectedMode = "DCV";
  api.app.state = connectedState({ simulation: true });
  api.updateStateDisplay(api.app.state);
  element(elements, "experimentName").value = "模拟预检文案";
  let releasePreflight;
  let markPreflightStarted;
  const preflightStarted = new Promise((resolve) => { markPreflightStarted = resolve; });
  context.fetch = async (_url, options) => {
    assert.equal(JSON.parse(options.body).action, "preflight");
    return new Promise((resolve) => {
      releasePreflight = () => resolve({ ok: true, json: async () => ({ success: false, message: "测试结束" }) });
      markPreflightStarted();
    });
  };

  const pendingStart = api.startSingle();
  await preflightStarted;

  assert.doesNotMatch(element(elements, "preflightSummary").textContent, /接线确认|实体接线/);
  releasePreflight();
  await pendingStart;
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

test("duplicate experiment dialog offers backed-up overwrite, create-copy, rename, and cancel", async () => {
  const { api, elements } = createFrontend();
  api.bindControls();
  const dialogHtml = fs.readFileSync(path.join(ROOT, "src/fluke8845a/web/index.html"), "utf8");
  assert.match(dialogHtml, /id="duplicateRunDialog"[\s\S]*duplicateOverwrite[\s\S]*先备份[\s\S]*id="duplicateCreateCopy"[\s\S]*id="duplicateRename"[\s\S]*id="duplicateCancel"/);
  const html = fs.readFileSync(path.join(ROOT, "src/fluke8845a/web/app.js"), "utf8");
  assert.match(html, /choice === "rename"[\s\S]*experimentName"\)\.focus\(\)[\s\S]*experimentName"\)\.select\(\)/);
  for (const [buttonId, expected] of [["duplicateOverwrite", "overwrite"], ["duplicateCreateCopy", "create-copy"], ["duplicateRename", "rename"], ["duplicateCancel", "cancel"]]) {
    assert.ok(element(elements, buttonId).listeners.get("click")?.length, `${buttonId} must be connected to the duplicate-choice dialog`);
    const choicePromise = api.chooseDuplicateNameAction();
    await element(elements, buttonId).dispatch("click");
    assert.equal(await choicePromise, expected);
  }
});

test("confirmed duplicate experiment choice is sent with the start request", async () => {
  const { api, elements, context } = createFrontend();
  api.app.modes.set("DCV", dcv);
  api.app.selectedMode = "DCV";
  api.populateModeOptions(dcv);
  element(elements, "experimentName").value = "重复实验";
  api.bindControls();
  const requests = [];
  context.fetch = async (_url, options) => {
    const request = JSON.parse(options.body);
    requests.push(request);
    return { ok: true, json: async () => request.action === "preflight"
      ? { success: true, checks: ["已检查"], duplicate_name: true, duplicate_path: "D:/runs/confirmed" }
      : { success: true, message: "已启动" } };
  };

  const startPromise = api.startContinuous();
  for (let attempt = 0; attempt < 10 && !element(elements, "duplicateRunDialog").showModalCalled; attempt += 1) {
    await new Promise((resolve) => setImmediate(resolve));
  }
  assert.equal(element(elements, "duplicateRunDialog").showModalCalled, true);
  await element(elements, "duplicateCreateCopy").dispatch("click");
  await startPromise;

  const start = requests.find((request) => request.action === "start");
  assert.equal(start?.payload?.duplicate_name_action, "create-copy");
});

test("confirmed backed-up overwrite choice is sent with the start request", async () => {
  const { api, elements, context } = createFrontend();
  api.app.modes.set("DCV", dcv);
  api.app.selectedMode = "DCV";
  api.populateModeOptions(dcv);
  element(elements, "experimentName").value = "重复实验";
  api.bindControls();
  const requests = [];
  context.fetch = async (_url, options) => {
    const request = JSON.parse(options.body);
    requests.push(request);
    return { ok: true, json: async () => request.action === "preflight"
      ? { success: true, checks: ["已检查"], duplicate_name: true, duplicate_path: "D:/runs/confirmed" }
      : { success: true, message: "已启动" } };
  };

  const startPromise = api.startContinuous();
  for (let attempt = 0; attempt < 10 && !element(elements, "duplicateRunDialog").showModalCalled; attempt += 1) {
    await new Promise((resolve) => setImmediate(resolve));
  }
  assert.equal(element(elements, "duplicateRunDialog").showModalCalled, true);
  await element(elements, "duplicateOverwrite").dispatch("click");
  await startPromise;

  const start = requests.find((request) => request.action === "start");
  assert.equal(start?.payload?.duplicate_name_action, "overwrite");
  assert.equal(start?.payload?.expected_duplicate_path, "D:/runs/confirmed");
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
  const dci = { ...dcv, value: "DCI", label: "直流电流", quantity: "current", unit: "A", ranges: [.01, .1, .4, 1, 3, 10], nplc: [1], bandwidths: [], analog_filter: true, safety_limits: "8845A：100 µA–10 A；400 mA 连续；440 mA 和 11 A 输入保险丝限制。" };
  api.populateModeOptions(dci);
  assert.match(element(elements, "terminalDiagram").innerHTML, /mA \/ 400 mA/);
  assert.match(element(elements, "terminalDiagram").innerHTML, /10 A/);
  assert.equal(element(elements, "modeSafetyLimits").textContent, dci.safety_limits);
  assert.equal(element(elements, "modeSafetyLimits").classList.contains("hidden"), false);
  assert.match(element(elements, "rangeLimitHint").textContent, /OutG SW ≥ 2\.0/);
  assert.match(element(elements, "modeOptions").innerHTML, /NPLC 越高.*自动零点/);

  const res4 = { ...dcv, value: "RES4", label: "四线电阻", quantity: "resistance", unit: "Ohm", ranges: [100, 1e3, 1e6, 1e8], nplc: [1], bandwidths: [], safety_limits: "8845A：100 Ω–100 MΩ。" };
  api.populateModeOptions(res4);
  assert.match(element(elements, "terminalDiagram").innerHTML, /SENSE HI/);
  assert.equal(element(elements, "modeSafetyLimits").textContent, res4.safety_limits);
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
    serial_number: "TEST8845A001", range_firmware: "1.9.0.0", range_firmware_source: "面板核对的 OutG SW",
    idn_version_candidate: "2.10.882.24",
  }, configuration: { input_terminals: "FRONT" }, active_settings: {
    mode: "DCI", autorange: false, range_si: .4,
  } }));
  const oldFirmware = element(elements, "rangeLimitHint").textContent;
  assert.match(oldFirmware, /400 mA.*1\.9\.0\.0.*面板核对的 OutG SW.*低于.*2\.0/);
  assert.doesNotMatch(oldFirmware, /2\.10\.882\.24/);
  assert.match(oldFirmware, /FRONT.*回读/);
  assert.match(oldFirmware, /下一步.*核对.*仪表面板.*OutG SW/);

  element(elements, "range").value = "1";
  api.updateStateDisplay(connectedState({ simulation: false, device: {
    serial_number: "TEST8845A001", range_firmware: "2.10.882.24", range_firmware_source: "面板核对的 OutG SW",
  }, configuration: { input_terminals: "REAR" }, active_settings: {
    mode: "DCI", autorange: false, range_si: 1,
  } }));
  const rearTerminal = element(elements, "rangeLimitHint").textContent;
  assert.match(rearTerminal, /1 A.*REAR.*前面板/);
  assert.match(rearTerminal, /面板核对的 OutG SW.*2\.10\.882\.24/);
  assert.match(rearTerminal, /下一步.*面板.*接线/);

  element(elements, "autorange").checked = true;
  api.updateStateDisplay(connectedState({ simulation: false, device: {
    serial_number: "TEST8845A001", range_firmware: null, range_firmware_source: "未知",
  }, configuration: {}, active_settings: { mode: "DCI", autorange: true } }));
  const unknownReadback = element(elements, "rangeLimitHint").textContent;
  assert.match(unknownReadback, /端子.*未回读|未回读.*端子/);
  assert.match(unknownReadback, /版本.*未核实|未核实.*版本/);
  assert.doesNotMatch(unknownReadback, /已确认 FRONT|已确认支持/);

  const aci = { ...dcv, value: "ACI", label: "交流电流", quantity: "current", unit: "A", ranges: [.01, .1, .4, 1, 3, 10], nplc: [], bandwidths: [3, 20, 200] };
  api.app.modes.set("ACI", aci);
  api.populateModeOptions(aci);
  element(elements, "autorange").checked = false;
  element(elements, "range").value = "0.4";
  api.updateStateDisplay(connectedState({ simulation: false, device: {
    serial_number: "TEST8845A001", range_firmware: "1.0.700.17", range_firmware_source: "面板核对的 OutG SW",
  }, configuration: { input_terminals: "FRONT" }, active_settings: {
    mode: "ACI", autorange: false, range_si: .4,
  } }));
  const aciFirmware = element(elements, "rangeLimitHint").textContent;
  assert.match(aciFirmware, /ACI.*1\.0\.700\.18/);
  assert.match(aciFirmware, /官方资料未明确.*OutG SW.*InG SW/);
  assert.match(aciFirmware, /本软件不根据 OutG SW 或 InG SW 自动拒绝 400 mA 档/);
  assert.match(aciFirmware, /请在仪表面板核对软件版本/);
  assert.doesNotMatch(aciFirmware, /低于门槛 1\.0\.700\.18/);
  assert.doesNotMatch(aciFirmware, /ACI 400 mA 档要求 OutG SW/);
  assert.equal(requests, 0, "guidance is rendered from cached state without a device request");
});

test("firmware version field tooltips distinguish DCI and ACI source evidence", () => {
  const html = fs.readFileSync(path.join(ROOT, "src", "fluke8845a", "web", "index.html"), "utf8");
  const outguard = html.match(/<label title="([^"]*)">OutG SW/);
  const inguard = html.match(/<label title="([^"]*)">InG SW/);
  assert.ok(outguard, "OutG version tooltip exists");
  assert.ok(inguard, "InG version tooltip exists");
  assert.match(outguard[1], /DCI.*OutG SW/);
  assert.match(outguard[1], /ACI.*未明确.*OutG.*InG.*不根据.*自动拒绝/);
  assert.match(inguard[1], /DCI.*不依据 InG SW/);
  assert.match(inguard[1], /ACI.*未明确.*不根据.*自动拒绝/);
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
  assert.match(element(elements, "controlModeHelp").textContent, /SYST:LOC.*SCPI.*回读.*核对仪表面板/);
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
  const luminance = (hex) => {
    const normalized = hex.length === 4
      ? `#${[...hex.slice(1)].map((value) => `${value}${value}`).join("")}`
      : hex;
    const channels = normalized.slice(1).match(/../g).map((value) => parseInt(value, 16) / 255).map((value) => value <= 0.04045 ? value / 12.92 : ((value + 0.055) / 1.055) ** 2.4);
    return channels[0] * 0.2126 + channels[1] * 0.7152 + channels[2] * 0.0722;
  };
  const declaredTextColor = (declarations, description) => {
    const color = declarations.split(";")
      .map((declaration) => declaration.trim().match(/^color\s*:\s*(#[\da-fA-F]{3}(?:[\da-fA-F]{3})?)$/))
      .find(Boolean);
    assert.ok(color, `${description} has an explicit foreground color`);
    return color[1];
  };
  const contrast = (foreground, background) => {
    const values = [luminance(foreground), luminance(background)].sort((a, b) => b - a);
    return (values[0] + 0.05) / (values[1] + 0.05);
  };
  assert.equal(declaredTextColor("border-color: #ff0000; color: #123", "parser regression"), "#123");
  const screenColor = css.match(/--screen:\s*(#[\da-fA-F]{6})/);
  assert.ok(screenColor, "main measurement readout color token exists");
  assert.ok(contrast(screenColor[1], "#1d262a") >= 3.0, "large numeric readout contrast must be at least 3:1");
  const hintColor = declaredTextColor(hintRule[1], "settings hint");
  assert.ok(contrast(hintColor, "#f7f6ef") >= 4.5, `${hintColor} contrast must be at least 4.5:1`);
  const sectionKickerRule = css.match(/\.section-kicker\s*\{([^}]*)\}/);
  assert.ok(sectionKickerRule);
  const sectionKickerColor = declaredTextColor(sectionKickerRule[1], "section label");
  assert.ok(contrast(sectionKickerColor, "#f1efe6") >= 4.5, "section labels must meet 4.5:1 on the chart and dialog background");
  const stageRule = css.match(/\.connection-stages li\s*\{([^}]*)\}/);
  assert.ok(stageRule);
  const stageColor = declaredTextColor(stageRule[1], "connection stage");
  assert.ok(contrast(stageColor, "#f4f6f2") >= 4.5, "connection stage labels must meet 4.5:1 on their badge background");
  const eventValueRule = css.match(/\.event-list \.event-value\s*\{([^}]*)\}/);
  assert.ok(eventValueRule);
  const eventValueColor = declaredTextColor(eventValueRule[1], "event value");
  assert.ok(contrast(eventValueColor, "#fffefa") >= 4.5, "event-value labels must meet 4.5:1 on the event list background");
  const darkTextRule = css.match(/\.raw-value\s*\{([^}]*)\}/);
  assert.ok(darkTextRule);
  const darkTextColor = declaredTextColor(darkTextRule[1], "dark readout auxiliary text");
  assert.ok(contrast(darkTextColor, "#1d262a") >= 4.5, "dark readout auxiliary text contrast must be at least 4.5:1");
  const placeholderRule = css.match(/input::placeholder\s*\{([^}]*)\}/);
  assert.ok(placeholderRule);
  const placeholderColor = declaredTextColor(placeholderRule[1], "input hint");
  assert.ok(contrast(placeholderColor, "#fffefa") >= 4.5, "input hint contrast must be at least 4.5:1");

  const readableTextRule = (selector, background, description) => {
    const escapedSelector = selector.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    const rule = css.match(new RegExp(`${escapedSelector}\\s*\\{([^}]*)\\}`));
    assert.ok(rule, `${description} style exists`);
    const color = declaredTextColor(rule[1], description);
    assert.ok(contrast(color, background) >= 4.5, `${description} contrast must be at least 4.5:1`);
    return rule[1];
  };
  const shortcutRule = readableTextRule(".application-shortcut-help", "#252d31", "acquisition shortcut help");
  assert.match(shortcutRule, /font-size:\s*12px/);
  readableTextRule(".version-label", "#30383c", "top-bar version label");
  readableTextRule(".telemetry-heading span", "#f4f4ed", "telemetry secondary heading");
  readableTextRule(".mock-note p", "#e9f4ed", "Mock explanatory text");
  readableTextRule("#chartGestureHint", "#f1efe6", "plot gesture hint");
  readableTextRule(".stat-card .stat-count", "#f5f5ee", "statistic sample count");

  const statusAndReadoutPairs = [
    [".reading-mode", "#1d262a", "readout mode"],
    [".reading-unit", "#1d262a", "readout unit"],
    [".eyebrow", "#1d262a", "readout eyebrow"],
    [".quick-metrics span", "#1d262a", "readout metric label"],
    [".connection-badge.connected", "#2b443a", "connected badge"],
    [".connection-badge.mock", "#51422d", "Mock badge"],
    [".connection-summary.mock", "#1d262a", "Mock connection summary"],
    [".connection-summary.connected", "#1d262a", "connected summary"],
    [".command-status", "#30383c", "command status"],
    [".critical-error", "#472f2d", "critical error"],
    ['.connection-stages li[data-status="running"]', "#e6f5f4", "running connection stage"],
    ['.connection-stages li[data-status="success"]', "#eaf5ed", "successful connection stage"],
    ['.connection-stages li[data-status="failure"]', "#fbeeee", "failed connection stage"],
    [".button-primary", "#167b80", "primary action"],
    [".scenario-badge", "#51422d", "Mock scenario badge"],
  ];
  for (const [selector, background, description] of statusAndReadoutPairs) {
    readableTextRule(selector, background, description);
  }

  const chartSource = fs.readFileSync(path.join(ROOT, "src/fluke8845a/web/app.js"), "utf8");
  const chartScaleLabels = chartSource.slice(chartSource.indexOf('ctx.fillStyle = "#faf9f2"'), chartSource.indexOf("const plottedMode = app.modes.get(rows[0]?.mode || app.selectedMode)"));
  assert.match(chartScaleLabels, /ctx\.font = "12px Segoe UI, Microsoft YaHei UI, sans-serif"/);
  assert.doesNotMatch(chartScaleLabels, /ctx\.font = "11px/);
  const narrowMockMarker = css.match(/\.scenario-badge::before\s*\{([^}]*)\}/);
  assert.ok(narrowMockMarker);
  assert.match(narrowMockMarker[1], /font-size:\s*12px/);
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

test("measurement status keeps Real or Mock and the full scenario name visible on narrow screens", () => {
  const { api, elements } = createFrontend();
  api.updateStateDisplay(connectedState({ mock_scenario: "阶跃响应与恢复" }));
  assert.equal(element(elements, "connectionSummary").textContent, "模拟器");
  assert.equal(element(elements, "scenarioSummary").textContent, "模拟场景：阶跃响应与恢复");

  api.updateStateDisplay(connectedState({
    simulation: false, control_mode: "computer", mock_scenario: null,
    connection_text: "真实 Fluke 8845A 已连接",
  }));
  assert.equal(element(elements, "connectionSummary").textContent, "真实仪器 · Computer");
  assert.equal(element(elements, "scenarioSummary").classList.contains("hidden"), true);

  const css = fs.readFileSync(path.join(ROOT, "src/fluke8845a/web/app.css"), "utf8");
  const narrow = css.match(/@media \(max-width: 380px\)\s*\{([\s\S]*?)\n\}/);
  assert.ok(narrow);
  assert.doesNotMatch(narrow[1], /\.connection-summary|\.scenario-summary[^}]*display:\s*none/);
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
  assert.match(element(elements, "commandStatus").textContent, /正在读取仪器身份.*诊断结果/);
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

test("instrument diagnosis progress names the readbacks and visible result", async () => {
  const { api, elements, context } = createFrontend();
  let finish;
  context.fetch = () => new Promise((resolve) => { finish = resolve; });

  const pending = api.command("diagnose");

  assert.match(element(elements, "commandStatus").textContent, /读取仪器身份.*测量功能.*输入端子.*错误队列/);
  assert.match(element(elements, "commandStatus").textContent, /显示诊断结果/);
  finish({ json: async () => ({ success: true, message: "诊断查询已完成" }) });
  await pending;
});

test("long command progress text remains fully readable instead of being clipped", async (t) => {
  const candidates = [
    process.env.CHROME_BIN,
    process.env.EDGE_BIN,
    "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe",
    "C:/Program Files/Google/Chrome/Application/chrome.exe",
  ].filter(Boolean);
  const browser = candidates.find((candidate) => fs.existsSync(candidate));
  if (!browser) return t.skip("Chromium is not installed; command progress wrapping cannot be measured here");

  const css = fs.readFileSync(path.join(ROOT, "src/fluke8845a/web/app.css"), "utf8");
  const message = "停止并保存：正在停止采集，等待采集线程完成写入并关闭 CSV；完成后报告有效样本数、异常样本数、实际文件路径及保存结果。";
  const probe = `<!doctype html><meta charset="utf-8"><style>${css}</style>
    <section class="action-bar" style="width:480px">
      <button class="button button-danger">停止并保存</button>
      <span id="commandStatus" class="command-status">${message}</span>
    </section><pre id="result">pending</pre><script>
      requestAnimationFrame(() => {
        const status = document.querySelector("#commandStatus");
        const rect = status.getBoundingClientRect();
        document.querySelector("#result").textContent = JSON.stringify({
          text: status.textContent,
          clientWidth: status.clientWidth,
          scrollWidth: status.scrollWidth,
          clientHeight: status.clientHeight,
          scrollHeight: status.scrollHeight,
          height: rect.height,
        });
      });
    </script>`;
  const metrics = await inspectChromiumDom(browser, probe);

  assert.equal(metrics.text, message);
  assert.ok(metrics.scrollWidth <= metrics.clientWidth + 1, `progress text is not clipped horizontally: ${JSON.stringify(metrics)}`);
  assert.ok(metrics.clientHeight >= metrics.scrollHeight - 1, `progress text is not clipped vertically: ${JSON.stringify(metrics)}`);
  assert.ok(metrics.height > 20, `long progress text wraps onto visible lines: ${JSON.stringify(metrics)}`);
});

test("started Web commands are polled to completion instead of being reported as failures", async () => {
  const { api, elements, context } = createFrontend();
  const requests = [];
  context.setTimeout = (callback, delay) => delay === 400 ? globalThis.setTimeout(callback, 0) : 1;
  context.fetch = async (url, options = {}) => {
    requests.push({ url, options });
    if (url === "/api/command") {
      return {
        status: 202,
        json: async () => ({ success: false, pending: true, operation_id: "a".repeat(32), message: "仍在处理" }),
      };
    }
    assert.equal(url, `/api/command-status?id=${"a".repeat(32)}`);
    return {
      ok: true,
      status: 200,
      json: async () => requests.filter((request) => request.url.startsWith("/api/command-status")).length === 1
        ? { pending: true }
        : { pending: false, result: { success: true, message: "完成" } },
    };
  };

  const result = await api.command("zero");

  assert.equal(result.success, true);
  assert.equal(requests.filter((request) => request.url === "/api/command").length, 1);
  assert.equal(requests.filter((request) => request.url.startsWith("/api/command-status")).length, 2);
  assert.match(element(elements, "toast").textContent, /完成/);
  assert.equal(element(elements, "zero").disabled, false);
});

test("uncertain Web commands stay locked until the user acknowledges a manual state check", async () => {
  const { api, elements, context } = createFrontend();
  context.fetch = async () => { throw new Error("offline"); };
  let confirmed = false;
  context.window.confirm = () => confirmed;

  const result = await api.command("diagnose");

  assert.equal(result.uncertain, true);
  assert.equal(api.app.pendingCommands.has("diagnose"), true);
  assert.equal(element(elements, "diagnose").disabled, true);
  assert.equal(element(elements, "acknowledgeUncertainCommand").classList.contains("hidden"), false);
  assert.match(element(elements, "commandStatus").textContent, /结果未确认.*核对仪器状态/);
  assert.equal(api.acknowledgeUncertainCommands(), false);
  assert.equal(api.app.pendingCommands.has("diagnose"), true);

  confirmed = true;
  assert.equal(api.acknowledgeUncertainCommands(), true);
  assert.equal(api.app.pendingCommands.has("diagnose"), false);
  assert.equal(element(elements, "diagnose").disabled, false);
  assert.equal(element(elements, "acknowledgeUncertainCommand").classList.contains("hidden"), true);
});

test("status polling backs off and exposes recovery after repeated local-service failures", async () => {
  const { api, elements, context } = createFrontend();
  const requests = [];
  context.setTimeout = (callback, delay) => globalThis.setTimeout(callback, 0);
  context.fetch = async (url) => {
    requests.push(url);
    if (url === "/api/command") {
      return {
        status: 202,
        json: async () => ({ success: false, pending: true, operation_id: "b".repeat(32), message: "仍在处理" }),
      };
    }
    throw new Error("offline");
  };

  const result = await api.command("zero");

  assert.equal(result.uncertain, true);
  assert.equal(requests.filter((url) => url.startsWith("/api/command-status")).length, 3);
  assert.equal(api.app.pendingCommands.has("zero"), true);
  assert.equal(element(elements, "acknowledgeUncertainCommand").classList.contains("hidden"), false);
  assert.match(element(elements, "commandStatus").textContent, /连续 3 次无法查询状态/);
});

test("command recovery is persisted before POST and tracks the server operation id", async () => {
  const storage = new Map();
  const { api, context } = createFrontend({ storage });
  context.setTimeout = (callback) => globalThis.setTimeout(callback, 0);
  const operationId = "c".repeat(32);
  context.fetch = async (url) => {
    const record = JSON.parse(storage.get("8845a-unresolved-command:zero") || "null");
    if (url === "/api/command") {
      assert.equal(record.action, "zero");
      assert.equal(record.state, "sending");
      return {
        status: 202,
        json: async () => ({ success: false, pending: true, operation_id: operationId, message: "仍在处理" }),
      };
    }
    assert.equal(record.operation_id, operationId);
    return {
      ok: true,
      status: 200,
      json: async () => ({ pending: false, result: { success: true, message: "完成" } }),
    };
  };

  const result = await api.command("zero");

  assert.equal(result.success, true);
  assert.equal(storage.has("8845a-unresolved-command:zero"), false);
});

test("a pending server operation is restored and queried after a page reload", async () => {
  const storage = new Map([[
    "8845a-unresolved-command:stop",
    JSON.stringify({ action: "stop", state: "pending", operation_id: "d".repeat(32), started_at: 1 }),
  ]]);
  const { api, context } = createFrontend({ storage });
  context.setTimeout = (callback) => globalThis.setTimeout(callback, 0);
  let statusQueries = 0;
  context.fetch = async (url) => {
    assert.equal(url, `/api/command-status?id=${"d".repeat(32)}`);
    statusQueries += 1;
    return {
      ok: true,
      status: 200,
      json: async () => ({ pending: false, result: { success: true, message: "已停止" } }),
    };
  };

  await api.restoreUnresolvedCommands();

  assert.equal(statusQueries, 1);
  assert.equal(api.app.pendingCommands.has("stop"), false);
  assert.equal(storage.has("8845a-unresolved-command:stop"), false);
});

test("command recovery storage changes keep other tabs synchronized", () => {
  const { api, elements } = createFrontend();
  const record = { action: "zero", state: "uncertain", message: "请核对仪器状态" };

  api.handleCommandRecoveryStorage({
    key: "8845a-unresolved-command:zero", newValue: JSON.stringify(record),
  });

  assert.equal(api.app.pendingCommands.has("zero"), true);
  assert.equal(element(elements, "zero").disabled, true);
  assert.match(element(elements, "commandStatus").textContent, /请核对仪器状态/);

  api.handleCommandRecoveryStorage({ key: "8845a-unresolved-command:zero", newValue: null });
  assert.equal(api.app.pendingCommands.has("zero"), false);
  assert.equal(element(elements, "zero").disabled, false);
});

test("legacy shared recovery records migrate to per-action keys", async () => {
  const storage = new Map([[
    "8845a-unresolved-command",
    JSON.stringify([{ action: "zero", state: "uncertain", message: "需人工核对" }]),
  ]]);
  const { api } = createFrontend({ storage });

  await api.restoreUnresolvedCommands();

  assert.equal(storage.has("8845a-unresolved-command"), false);
  assert.equal(JSON.parse(storage.get("8845a-unresolved-command:zero")).action, "zero");
  assert.equal(api.app.pendingCommands.has("zero"), true);
});

test("two Web tabs serialize the same action and keep independent unresolved records", async () => {
  const storage = new Map();
  const heldLocks = new Set();
  const sharedLocks = {
    request: async (name, options, callback) => {
      if (options.ifAvailable && heldLocks.has(name)) return callback(null);
      heldLocks.add(name);
      try { return await callback({ name }); }
      finally { heldLocks.delete(name); }
    },
  };
  const firstTab = createFrontend({ storage, lockManager: sharedLocks });
  const secondTab = createFrontend({ storage, lockManager: sharedLocks });
  let finishFirst;
  let firstPosts = 0;
  let secondPosts = 0;
  firstTab.context.fetch = async (url) => {
    if (url === "/api/command") {
      firstPosts += 1;
      return new Promise((resolve) => { finishFirst = resolve; });
    }
    throw new Error(`unexpected request: ${url}`);
  };
  secondTab.context.fetch = async (url) => {
    if (url === "/api/command") secondPosts += 1;
    throw new Error("offline");
  };

  const running = firstTab.api.command("zero");
  await Promise.resolve();
  const duplicate = await secondTab.api.command("zero");
  assert.equal(duplicate.success, false);
  assert.equal(secondPosts, 0, "the second tab must not POST the locked action");
  finishFirst({ status: 200, json: async () => ({ success: true, message: "完成" }) });
  await running;

  firstTab.context.fetch = async () => { throw new Error("offline"); };
  const uncertain = await firstTab.api.command("zero");
  assert.equal(uncertain.uncertain, true);
  assert.ok(storage.has("8845a-unresolved-command:zero"));
  const staleTabDuplicate = await secondTab.api.command("zero");
  assert.equal(staleTabDuplicate.success, false);
  assert.equal(secondPosts, 0, "a stale tab must respect a persisted unresolved action");

  const otherAction = await secondTab.api.command("diagnose");
  assert.equal(otherAction.uncertain, true);
  assert.equal(secondPosts, 1);
  assert.ok(storage.has("8845a-unresolved-command:zero"));
  assert.ok(storage.has("8845a-unresolved-command:diagnose"));
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

test("READ and SCPI response timeouts both require stopping and manual recovery", () => {
  const { api, elements } = createFrontend();
  api.app.modes.set("DCV", dcv);
  api.app.latestInvalidPoint = { id: 5, status: "timeout", error: "READ? 响应超时" };
  api.updateStateDisplay(connectedState({ simulation: false, status: "采集中", experiment_dir: "D:/runs/read-timeout", last_experiment_dir: "D:/runs/read-timeout", last_error: "READ? 响应超时", stats: {
    attempt_count: 5, valid_count: 3, abnormal_count: 2,
  } }));
  const readTimeout = element(elements, "acquisitionFeedback").textContent;
  assert.match(readTimeout, /READ 超时.*立即停止采集.*关闭连接/);
  assert.match(readTimeout, /不会自动重试或重连/);
  assert.match(readTimeout, /手动重新连接.*新实验/);
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

test("Mock timeout feedback does not claim a real serial disconnect", () => {
  const { api, elements } = createFrontend();
  api.app.modes.set("DCV", dcv);
  api.app.latestInvalidPoint = { id: 2, status: "timeout", error: "READ? 模拟超时" };
  api.updateStateDisplay(connectedState({
    simulation: true,
    status: "采集中",
    experiment_dir: "D:/runs/mock-timeout",
    last_experiment_dir: "D:/runs/mock-timeout",
    stats: { attempt_count: 2, valid_count: 1, abnormal_count: 1 },
  }));

  const feedback = element(elements, "acquisitionFeedback").textContent;
  assert.match(feedback, /MOCK.*模拟/);
  assert.match(feedback, /连续\s*3\s*次.*超时.*停止/);
  assert.match(feedback, /不涉及真实串口/);
  assert.doesNotMatch(feedback, /关闭连接|手动重新连接/);
});

test("completed READ timeout keeps manual recovery guidance even when state says ended", () => {
  const { api, elements } = createFrontend();
  api.app.modes.set("DCV", dcv);
  api.app.latestInvalidPoint = { id: 5, status: "timeout", error: "READ? 响应超时" };
  api.updateStateDisplay(connectedState({
    connected: true, simulation: false, status: "实验已结束", experiment_dir: "",
    last_experiment_dir: "D:/runs/recovered-read", last_error: "READ? 响应超时",
    stats: { attempt_count: 6, valid_count: 5, abnormal_count: 1 },
  }));

  const feedback = element(elements, "acquisitionFeedback").textContent;
  assert.match(feedback, /READ 超时.*立即停止采集.*关闭连接/);
  assert.match(feedback, /手动重新连接.*新实验/);
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
  assert.match(feedback, /不会自动重试或重连/);
  assert.match(feedback, /已保存尝试\s*4.*有效\s*3.*异常\s*1/);
});

test("Worker串口通信异常状态 receives immediate-stop recovery guidance", () => {
  const { api, elements } = createFrontend();
  api.app.modes.set("DCV", dcv);
  api.updateStateDisplay(connectedState({
    connected: false,
    simulation: false,
    status: "串口通信异常，连接已关闭；请重新连接后开始新实验/分段",
    connection_text: "COM3 连接已关闭",
    experiment_dir: "",
    last_experiment_dir: "D:/runs/communication-error",
    last_error: "串口通信异常",
    stats: { attempt_count: 4, valid_count: 3, abnormal_count: 1 },
  }));

  const feedback = element(elements, "acquisitionFeedback").textContent;
  assert.match(feedback, /串口通信异常.*立即停止采集.*关闭连接/);
  assert.match(feedback, /手动重新连接.*新实验/);
  assert.match(feedback, /已保存尝试\s*4.*有效\s*3.*异常\s*1/);
});

test("READ timeouts and serial communication failures stop immediately and require a new segment", () => {
  const { api, elements } = createFrontend();
  api.app.modes.set("DCV", dcv);
  api.app.latestInvalidPoint = { id: 7, status: "timeout", error: "READ? 响应超时" };
  api.updateStateDisplay(connectedState({
    connected: true, simulation: false, status: "采集中", experiment_dir: "D:/runs/timeout",
    last_experiment_dir: "D:/runs/timeout", stats: { attempt_count: 7, valid_count: 6, abnormal_count: 1 },
  }));

  const feedback = element(elements, "acquisitionFeedback").textContent;
  assert.match(feedback, /立即停止采集.*保存.*关闭连接/);
  assert.match(feedback, /不会自动重试或重连/);
  assert.match(feedback, /人工确认.*重新连接.*新实验|人工确认.*新实验.*分段/);
  assert.doesNotMatch(feedback, /连续\s*3\s*次|下一采样周期重试/);

  api.app.latestInvalidPoint = { id: 8, status: "error", error: "串口通信失败" };
  api.updateStateDisplay(connectedState({
    connected: true, simulation: false, status: "采集中", experiment_dir: "D:/runs/timeout",
    last_experiment_dir: "D:/runs/timeout", latest: { sample_index: 8, status: "error" },
    stats: { attempt_count: 8, valid_count: 6, abnormal_count: 2 },
  }));
  const communicationFeedback = element(elements, "acquisitionFeedback").textContent;
  assert.match(communicationFeedback, /串口通信异常.*立即停止采集.*关闭连接/);
  assert.match(communicationFeedback, /不会自动重试或重连/);
  assert.doesNotMatch(communicationFeedback, /连续\s*3\s*次|下一采样周期重试/);
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

test("recent invalid sample remains visible with recovery status after a newer valid reading", () => {
  const { api, elements } = createFrontend();
  api.app.modes.set("DCV", dcv);
  api.app.latestInvalidPoint = { id: 5, status: "timeout", error: "READ? 响应超时" };
  api.updateStateDisplay(connectedState({
    connected: true, simulation: false, status: "采集中", experiment_dir: "D:/runs/recovered",
    last_experiment_dir: "D:/runs/recovered", last_error: "READ? 响应超时",
    latest: { sample_index: 6, status: "ok", value_si: 1.2, unit_si: "V", mode: "DCV", raw_reading: "1.2" },
    stats: { attempt_count: 6, valid_count: 5, abnormal_count: 1 },
  }));

  assert.equal(element(elements, "criticalError").classList.contains("hidden"), false);
  assert.match(element(elements, "criticalError").textContent, /最近无效样本 #5.*READ\? 响应超时.*后续有效读数已恢复/);
});

test("long acquisition recovery guidance wraps and remains visible in Chromium", async (t) => {
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
  const metrics = await inspectChromiumDom(browser, probe);
  assert.equal(metrics.whiteSpace, "normal");
  assert.notEqual(metrics.overflow, "hidden");
  assert.notEqual(metrics.textOverflow, "ellipsis");
  assert.ok(metrics.height > (parseFloat(metrics.lineHeight) || 14) * 1.5);
  assert.equal(metrics.scrollHeight, metrics.clientHeight);
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

test("ZERO confirmation shows the last auto-range snapshot and warns that it can change", async () => {
  const { api, elements, context } = createFrontend();
  api.app.modes.set("DCV", dcv);
  api.app.selectedMode = "DCV";
  api.app.state = connectedState({ simulation: false, active_settings: {
    mode: "DCV", autorange: true, range_si: null, range_readback_si: 10,
    nplc: 1, analog_filter: false, autozero: true,
  } });
  let confirmation = "";
  context.window.confirm = (message) => { confirmation = message; return false; };
  api.bindControls();

  await element(elements, "zero").dispatch("click");

  assert.match(confirmation, /自动量程/);
  assert.match(confirmation, /当前实际量程回读 10 V/);
  assert.match(confirmation, /可能随输入变化/);
});

test("ACV and ACI ZERO confirmations mark analog filtering as not applicable", async (t) => {
  for (const [value, label, unit] of [["ACV", "交流电压", "V"], ["ACI", "交流电流", "A"]]) {
    await t.test(value, async () => {
      const { api, elements, context } = createFrontend();
      const mode = { ...dcv, value, label, unit, nplc: [], bandwidths: [3, 20, 200], analog_filter: false };
      api.app.modes.set(value, mode);
      api.app.selectedMode = value;
      api.app.state = connectedState({ simulation: false, active_settings: {
        mode: value, autorange: true, range_si: null, range_readback_si: null,
        nplc: null, ac_bandwidth_hz: 20, analog_filter: false, autozero: true,
      } });
      let confirmation = "";
      context.window.confirm = (message) => { confirmation = message; return false; };
      api.bindControls();

      await element(elements, "zero").dispatch("click");

      assert.match(confirmation, /模拟滤波：不适用/);
      assert.doesNotMatch(confirmation, /模拟滤波：关/);
    });
  }
});

test("Web ZERO replaces old feedback with pending and current router rejection feedback", async () => {
  const { api, elements, context } = createFrontend();
  api.app.modes.set("DCV", dcv);
  api.app.selectedMode = "DCV";
  api.app.state = connectedState({ zero_result: "previous successful ZERO feedback", active_settings: {
    mode: "DCV", autorange: true, nplc: 1, analog_filter: false, autozero: true,
  } });
  api.updateStateDisplay(api.app.state);
  context.window.confirm = () => true;
  let resolveFetch;
  let markFetchStarted;
  const fetchStarted = new Promise((resolve) => { markFetchStarted = resolve; });
  context.fetch = () => {
    markFetchStarted();
    return new Promise((resolve) => { resolveFetch = resolve; });
  };
  api.bindControls();

  const click = element(elements, "zero").dispatch("click");
  await fetchStarted;
  assert.equal(element(elements, "zeroResult").textContent, "ZERO执行中；等待本次操作反馈。");
  api.updateStateDisplay(connectedState({ zero_result: "previous successful ZERO feedback" }));
  assert.equal(element(elements, "zeroResult").textContent, "ZERO执行中；等待本次操作反馈。");

  const rejectedFeedback = "ZERO未执行：请停止采集后再执行 ZERO";
  resolveFetch({
    status: 400,
    json: async () => ({
      success: false,
      message: "请停止采集后再执行 ZERO",
      zero_result: rejectedFeedback,
    }),
  });
  await click;

  assert.equal(element(elements, "zeroResult").textContent, rejectedFeedback);
  assert.equal(element(elements, "zeroResult").classList.contains("hidden"), false);
  assert.doesNotMatch(element(elements, "zeroResult").textContent, /previous successful/);
});

test("Web ZERO labels a blocked same-tab command as not sent", async () => {
  const { api, elements, context } = createFrontend();
  api.app.modes.set("DCV", dcv);
  api.app.selectedMode = "DCV";
  api.app.state = connectedState({ active_settings: {
    mode: "DCV", autorange: true, nplc: 1, analog_filter: false, autozero: true,
  } });
  context.window.confirm = () => true;
  api.app.pendingCommands.add("zero");
  api.bindControls();

  await element(elements, "zero").dispatch("click");

  assert.equal(
    element(elements, "zeroResult").textContent,
    "ZERO未发送：此操作正在执行，请等待完成；本次命令未发送",
  );
});

test("statistics context names the live cache and excludes chart zoom from the scope", () => {
  const { api, elements } = createFrontend();
  api.app.modes.set("DCV", dcv);
  api.app.selectedMode = "DCV";
  api.app.state = connectedState();
  api.app.points = [
    { id: 1, x: 0, y: 1, status: "ok", unit: "V" },
    { id: 2, x: 1, y: 3, status: "ok", unit: "V" },
  ];

  api.updateStats();

  assert.match(element(elements, "analysisContext").textContent, /当前实时缓存/);
  assert.match(element(elements, "analysisContext").textContent, /图表缩放和平移不影响统计范围/);
  assert.doesNotMatch(element(elements, "analysisContext").textContent, /当前显示数据点/);
});

test("ZERO stays disabled until the selected mode has confirmed configuration readback", () => {
  const { api, elements } = createFrontend();
  api.app.modes.set("DCV", dcv);
  api.app.selectedMode = "DCV";

  api.updateStateDisplay(connectedState({ active_settings: {} }));
  assert.equal(element(elements, "zero").disabled, true);
  assert.match(element(elements, "zero").title, /先读取或应用当前模式配置/);

  api.updateStateDisplay(connectedState({ active_settings: { mode: "DCV", nplc: 1 } }));
  assert.equal(element(elements, "zero").disabled, false);
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
  assert.ok(strokes.some((stroke) => stroke.dash.length > 0 && stroke.path.length === 2
    && stroke.path[0][0] === "M" && stroke.path[1][0] === "L"
    && Math.abs(stroke.path[0][2] - stroke.path[1][2]) < 1e-8));
});

test("web chart text and reference markers meet contrast targets", () => {
  const css = fs.readFileSync(path.join(ROOT, "src/fluke8845a/web/app.css"), "utf8");
  const luminance = (hex) => {
    const channels = hex.slice(1).match(/../g).map((value) => parseInt(value, 16) / 255);
    const linear = channels.map((value) => value <= 0.04045 ? value / 12.92 : ((value + 0.055) / 1.055) ** 2.4);
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2];
  };
  const contrast = (foreground, background) => {
    const [first, second] = [luminance(foreground), luminance(background)].sort((a, b) => b - a);
    return (first + 0.05) / (second + 0.05);
  };
  const bodyRule = css.match(/body\s*\{([^}]*)\}/);
  assert.ok(bodyRule, "body style exists");
  const token = (name) => {
    const value = css.match(new RegExp(`${name}:\\s*(#[\\da-fA-F]{6})`));
    assert.ok(value, `${name} color token exists`);
    return value[1];
  };
  const bodyTextToken = bodyRule[1].match(/color:\s*var\((--[\w-]+)\)/);
  const bodyBackgroundToken = bodyRule[1].match(/background:\s*var\((--[\w-]+)\)/);
  assert.ok(bodyTextToken && bodyBackgroundToken, "body uses explicit foreground and background tokens");
  assert.ok(contrast(token(bodyTextToken[1]), token(bodyBackgroundToken[1])) >= 4.5, "default body text must contrast with the dark canvas");

  const { api, elements } = createFrontend({ stubChart: false });
  api.app.modes.set("DCV", { ...dcv, quantity: "voltage" });
  api.app.selectedMode = "DCV";
  api.app.points = [
    { id: 1, x: 0, y: 1, status: "ok", unit: "V" },
    { id: 2, x: 1, y: 2, status: "ok", unit: "V" },
    { id: 3, x: 2, y: 3, status: "ok", unit: "V" },
  ];
  api.app.state = connectedState({ events: [{ elapsed_s: 1, text: "负载切换" }] });
  api.app.baseline = 1.5;
  element(elements, "autoFollow").checked = false;
  const context = element(elements, "plot").canvasContext;
  const textDraws = [];
  context.fillText = (text, x, y) => textDraws.push({ text: String(text), color: context.fillStyle, x, y });
  api.drawChart();

  const chartBackground = "#faf9f2";
  for (const label of ["电压 (V)", "实验经过时间"]) {
    const draw = textDraws.find((item) => item.text === label);
    assert.ok(draw, `chart renders ${label}`);
    assert.ok(contrast(draw.color, chartBackground) >= 4.5, `${label} must meet normal-text contrast`);
  }
  const dashedSegments = context.strokes.filter((stroke) => stroke.dash.length > 0 && stroke.path.length >= 2);
  const baseline = dashedSegments.find((stroke) => Math.abs(stroke.path[0][2] - stroke.path[1][2]) < 1e-8);
  const event = dashedSegments.find((stroke) => Math.abs(stroke.path[0][1] - stroke.path[1][1]) < 1e-8);
  assert.ok(baseline, "the active baseline is drawn");
  assert.ok(event, "the experiment event marker is drawn");
  assert.ok(contrast(baseline.color, chartBackground) >= 3.0, "baseline line must meet non-text contrast");
  assert.ok(contrast(event.color, chartBackground) >= 3.0, "event line must meet non-text contrast");
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

test("history chart honors the worker break_before marker even when sample IDs are adjacent", () => {
  const { api, elements } = createFrontend({ stubChart: false });
  api.app.modes.set("DCV", dcv);
  api.app.imported = [
    { id: 1, x: 0, y: 1, status: "ok", unit: "V", mode: "DCV", break_before: false },
    { id: 2, x: 1, y: 2, status: "ok", unit: "V", mode: "DCV", break_before: true },
  ];
  element(elements, "autoFollow").checked = false;
  api.drawChart();

  const curve = element(elements, "plot").canvasContext.strokes.find((stroke) => stroke.color === "#138b92");
  assert.equal(curve.path.filter(([kind]) => kind === "M").length, 2);
});

test("late chart overview from a prior experiment cannot repopulate or unlock the new request", async () => {
  const { api, context } = createFrontend();
  const requests = [];
  context.fetch = (url) => new Promise((resolve) => requests.push({ url: String(url), resolve }));
  api.app.generation = 1;
  api.app.state = { generation: 1, experiment_dir: "old-run", last_experiment_dir: "old-run" };

  const oldRequest = api.refreshChartOverview();
  api.app.generation = 2;
  api.app.state = { generation: 2, experiment_dir: "new-run", last_experiment_dir: "new-run" };
  api.clearChartOverview();
  const newRequest = api.refreshChartOverview();
  const newRequestStarted = requests.length === 2;

  requests[0].resolve({
    ok: true,
    json: async () => ({
      status: "ready", job_id: "old-job", revision: 1,
      rows: [{ id: 100, x: 600, y: 30, mode: "RES2", unit: "Ω" }],
    }),
  });
  await oldRequest;
  const newRequestStillInFlight = api.app.chartOverviewInFlight;
  if (requests[1]) {
    requests[1].resolve({
      ok: true,
      json: async () => ({
        status: "ready", job_id: "new-job", revision: 1,
        rows: [{ id: 1, x: 0, y: 2, mode: "DCV", unit: "V" }],
      }),
    });
  }
  await newRequest;

  assert.equal(newRequestStarted, true);
  assert.equal(newRequestStillInFlight, true);
  assert.equal(api.app.chartOverviewJobId, "new-job");
  assert.deepEqual(api.app.chartOverview.map(({ id, mode, unit }) => ({ id, mode, unit })), [
    { id: 1, mode: "DCV", unit: "V" },
  ]);
  assert.equal(api.app.chartOverviewInFlight, false);
});

test("failed chart range requests clear their state so the same view can retry", async () => {
  const { api, context } = createFrontend();
  api.app.chartOverviewJobId = "chart-job-1";
  let rangeCalls = 0;
  context.fetch = async (url) => {
    if (String(url).includes("/api/chart/range/cancel?")) {
      return { ok: true, status: 200, json: async () => ({ status: "expired" }) };
    }
    rangeCalls += 1;
    if (rangeCalls === 1) return { ok: false, status: 429, json: async () => ({ error: "busy" }) };
    return { ok: true, status: 200, json: async () => ({ status: "ready", rows: [] }) };
  };

  assert.equal(await api.requestChartRange(0, 10, 100), false);
  assert.equal(api.app.chartRangeRequest, null);
  assert.equal(await api.requestChartRange(0, 10, 100), true);
  assert.equal(rangeCalls, 2);
});

test("changing views cancels a submitted first chart range request before its response", async () => {
  const { api, context } = createFrontend();
  api.app.chartOverviewJobId = "chart-job-1";
  const calls = [];
  let resolveFirstRequest;
  context.fetch = (url) => {
    calls.push(String(url));
    if (String(url).includes("/api/chart/range/cancel?")) {
      return Promise.resolve({ ok: true, status: 200, json: async () => ({ status: "cancelled" }) });
    }
    if (!resolveFirstRequest) return new Promise((resolve) => { resolveFirstRequest = resolve; });
    return Promise.resolve({ ok: true, status: 200, json: async () => ({ status: "ready", rows: [] }) });
  };

  const firstRequest = api.requestChartRange(0, 10, 100);
  assert.equal(typeof resolveFirstRequest, "function");
  assert.equal(await api.requestChartRange(20, 30, 100), true);
  assert.ok(calls.some((url) => url.includes("/api/chart/range/cancel?")), calls.join("\n"));
  resolveFirstRequest({ ok: true, status: 200, json: async () => ({ status: "running", rows: [] }) });
  assert.equal(await firstRequest, false);
});

test("history import warns and refuses mixed modes, and explains files with no valid readings", async () => {
  const { api, elements } = createFrontend();
  api.app.modes.set("DCV", dcv);
  api.app.modes.set("DCI", { ...dcv, value: "DCI", label: "直流电流", unit: "A" });
  const file = (text) => ({ name: "measurements.csv", size: text.length, text: async () => text });

  await api.loadHistory(file("elapsed_s,value_si,unit_si,mode,status\n0,1,V,DCV,ok\n1,2,A,DCI,ok\n"));
  assert.match(element(elements, "toast").textContent, /^警告：.*混合.*(模式|单位).*未加载/);
  assert.equal(element(elements, "toast").classList.contains("warning"), true);
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
    constructor(url) { this.url = url; this.messages = []; DeferredWorker.instances.push(this); }
    postMessage(message) {
      this.message = message;
      this.messages.push(message);
      if (message?.type === "cancel") queueMicrotask(() => this.onmessage?.({ data: { ok: false, cancelled: true, error: "导入已取消" } }));
    }
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

test("history import accepts a 1 GiB file and cancels the previous job through its worker protocol", async () => {
  const { api, elements, context } = createFrontend();
  api.app.modes.set("DCV", dcv);
  class DeferredWorker {
    constructor(url) { this.url = url; this.messages = []; DeferredWorker.instances.push(this); }
    postMessage(message) {
      this.message = message;
      this.messages.push(message);
      if (message?.type === "cancel") queueMicrotask(() => this.onmessage?.({ data: { ok: false, cancelled: true, error: "导入已取消" } }));
    }
    terminate() { this.terminated = true; }
  }
  DeferredWorker.instances = [];
  context.Worker = DeferredWorker;
  const first = api.loadHistory({ name: "first.csv", size: 20 });
  const oneGiB = { name: "one-gib.csv", size: 1024 ** 3 };
  const second = api.loadHistory(oneGiB);
  const [firstWorker, secondWorker] = DeferredWorker.instances;
  await first;

  assert.equal(firstWorker.terminated, true);
  assert.equal(firstWorker.message.type, "cancel");
  assert.equal(firstWorker.message.jobId, firstWorker.messages[0].jobId);
  assert.equal(secondWorker.message.file, oneGiB);
  assert.equal(secondWorker.message.reportProgress, true);
  assert.equal(secondWorker.message.pixelWidth, 800);
  secondWorker.onmessage({ data: { ok: true, rows: [], modeValue: "DCV", unit: "V", experimentId: "large-run", stats: null } });
  await second;
  assert.doesNotMatch(element(elements, "toast").textContent, /超过 50 MB/);
  assert.equal(api.app.importedFileName, "one-gib.csv");
  firstWorker.onmessage({ data: { ok: true, rows: [{ x: 0, y: 1, status: "ok", mode: "DCV", unit: "V", raw: "1", id: 1 }], modeValue: "DCV", unit: "V", experimentId: "stale-run" } });
  assert.deepEqual(Array.from(api.app.imported), []);
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

test("history event units are escaped before rendering imported CSV values", async () => {
  const { api, elements } = createFrontend();
  api.app.imported = [{ id: 1, x: 0, y: 1, status: "ok", unit: "V" }];
  api.app.importedExperimentId = "run-a";
  const unit = '<img src=x onerror="alert(1)">';
  const csvUnit = unit.replaceAll('"', '""');
  const csv = `experiment_id,elapsed_s,event,sample_index,value_si,unit_si\nrun-a,1,step,1,1,"${csvUnit}"\n`;

  await api.loadHistoryEvents({ name: "events.csv", size: csv.length, text: async () => csv });

  assert.equal(api.app.importedEvents.length, 1);
  assert.equal(api.app.importedEvents[0].unit_si, unit);
  const html = element(elements, "eventList").innerHTML;
  assert.doesNotMatch(html, /<img\s+src=x/i);
  assert.match(html, /&lt;img\s+src=x/i);
});

test("history directory import pairs only a same-directory events.csv", async () => {
  const html = fs.readFileSync(path.join(ROOT, "src/fluke8845a/web/index.html"), "utf8");
  assert.match(html, /id="historyDirectory"[^>]*webkitdirectory/);
  assert.match(html, /加载测量 CSV 后，可单独选择配套 events\.csv（会核对实验标识）/);
  assert.match(html, /id="eventsHistoryFile"[^>]*accept="\.csv,text\/csv"/);
  assert.match(html, /单独加载测量 CSV 后.*配套 events\.csv.*核对实验标识.*加载实验文件夹.*自动配对同目录文件/);
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

test("browser download results are listed separately from experiment directory files", async () => {
  const { api, elements, context } = createFrontend();
  api.bindControls();
  context.URL = { createObjectURL: () => "blob:safe-events", revokeObjectURL() {} };
  context.fetch = async () => ({
    ok: true,
    headers: { get: (name) => name.toLowerCase() === "content-disposition"
      ? "attachment; filename*=UTF-8''events_excel_safe.csv" : null },
    blob: async () => ({ type: "text/csv" }),
  });

  await element(elements, "downloadEventsSafe").dispatch("click");

  const results = element(elements, "filesExportResults").children;
  assert.equal(results.length, 1);
  assert.match(results[0].textContent, /事件安全副本.*events_excel_safe\.csv.*浏览器下载/);
  assert.equal(results[0].innerHTML, "");

  await api.refreshExperimentFiles();
  assert.equal(element(elements, "filesExportResults").children.length, 1);
});

test("experiment file dialog centralizes the file list, history imports, and result exports", async (t) => {
  const candidates = [
    process.env.CHROME_BIN,
    process.env.EDGE_BIN,
    "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe",
    "C:/Program Files/Google/Chrome/Application/chrome.exe",
  ].filter(Boolean);
  const browser = candidates.find((candidate) => fs.existsSync(candidate));
  if (!browser) return t.skip("Chromium is not installed; experiment file ownership cannot be checked in a DOM");

  const html = fs.readFileSync(path.join(ROOT, "src/fluke8845a/web/index.html"), "utf8");
  const embeddedHtml = JSON.stringify(html).replace(/</g, "\\u003c");
  const probe = `<!doctype html><meta charset="utf-8"><pre id="result">pending</pre><script>
    const parsed = new DOMParser().parseFromString(${embeddedHtml}, "text/html");
    const filesDialog = parsed.querySelector("#filesDialog");
    const importArea = filesDialog?.querySelector("#filesImportActions");
    const exportArea = filesDialog?.querySelector("#filesExportActions");
    const importIds = ["historyFile", "historyDirectory", "eventsHistoryFile", "historyImportProgress", "cancelHistoryImport"];
    const exportIds = ["downloadCsv", "downloadEventsSafe", "downloadMeasurementsSafe", "downloadSharePackage", "savePlot", "filesExportResults", "csvFormulaExportWarning"];
    document.querySelector("#result").textContent = JSON.stringify({
      hasDirectory: !!filesDialog?.querySelector("#filesDirectory"),
      hasFileList: !!filesDialog?.querySelector("#filesList"),
      importsCentralized: importIds.every((id) => importArea?.querySelector("#" + id)),
      exportsCentralized: exportIds.every((id) => exportArea?.querySelector("#" + id)),
      chartHasFileActions: [...importIds, ...exportIds].some((id) => parsed.querySelector(".chart-actions #" + id)),
    });
  </script>`;
  const result = await inspectChromiumDom(browser, probe);

  assert.equal(result.hasDirectory, true, "the file area identifies the experiment directory");
  assert.equal(result.hasFileList, true, "the file area lists files from the experiment directory");
  assert.equal(result.importsCentralized, true, "measurement, folder, and companion-event imports share one area");
  assert.equal(result.exportsCentralized, true, "measurement, event, plot, and sharing exports share one area");
  assert.equal(result.chartHasFileActions, false, "file operations are kept out of the chart toolbar");
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

test("hovering a chart event marker shows its time, local timestamp, and label", async () => {
  const { api, elements, animationFrames } = createFrontend({ stubChart: false });
  const event = {
    elapsed_s: 5,
    text: "load switched",
    timestamp_local: "2026-10-15T12:34:56+08:00",
  };
  api.app.modes.set("DCV", dcv);
  api.app.selectedMode = "DCV";
  api.app.state = connectedState({ events: [event] });
  api.app.points = [
    { id: 1, x: 0, y: 1, status: "ok", unit: "V" },
    { id: 2, x: 10, y: 2, status: "ok", unit: "V" },
  ];
  element(elements, "autoFollow").checked = false;
  api.drawChart();

  const marker = api.app.chartEventHits[0];
  assert.ok(marker, "the event is represented by a chart marker");
  const scale = api.app.chartScale;
  await element(elements, "plot").dispatch("pointermove", {
    clientX: marker.x,
    clientY: (scale.top + scale.bottom) / 2,
  });
  while (animationFrames.length) animationFrames.shift()();

  assert.equal(api.app.hoveredEventIndex, 0);
  assert.equal(api.app.cursor, null, "hovering the marker suppresses the measurement point cursor");
  const cursorText = element(elements, "pointCursor").textContent;
  assert.match(cursorText, /事件 · t=5 s/);
  assert.ok(cursorText.includes(expectedLocalDateTime(event.timestamp_local)), "the cursor includes the event's local timestamp");
  assert.match(cursorText, /load switched/);
  assert.ok(
    element(elements, "plotOverlay").canvasContext.calls.some(([, text]) => text === event.text),
    "the overlay labels the hovered marker with the event name",
  );
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

test("Web application shortcuts invoke acquisition actions and leave editable fields alone", async () => {
  const html = fs.readFileSync(path.join(ROOT, "src/fluke8845a/web/index.html"), "utf8");
  const { api, elements, context } = createFrontend();
  api.bindControls();
  const clicked = [];
  const actionIds = ["newMeasurement", "start", "pause", "resume", "stop", "openSettings", "markEvent"];
  for (const id of actionIds) element(elements, id).click = () => clicked.push(id);
  element(elements, "resume").classList.add("hidden");

  const shortcuts = [
    ["n", "newMeasurement"], ["r", "start"], ["p", "pause"], ["s", "stop"], [",", "openSettings"], ["Enter", "markEvent"],
  ];
  for (const [key, actionId] of shortcuts) {
    let prevented = false;
    await context.document.dispatch("keydown", {
      target: { tagName: "BODY" }, key, ctrlKey: true, altKey: true,
      preventDefault() { prevented = true; },
    });
    assert.deepEqual(clicked, [actionId], `${key} invokes its matching action`);
    assert.equal(prevented, true, `${key} does not trigger browser handling`);
    clicked.length = 0;
  }

  element(elements, "resume").classList.remove("hidden");
  await context.document.dispatch("keydown", {
    target: { tagName: "BODY" }, key: "p", ctrlKey: true, altKey: true,
    preventDefault() {},
  });
  assert.deepEqual(clicked, ["resume"], "Ctrl+Alt+P resumes when the resume action is visible");
  clicked.length = 0;

  for (const target of [
    { tagName: "INPUT" }, { tagName: "TEXTAREA" }, { tagName: "SELECT" }, { tagName: "DIV", isContentEditable: true },
  ]) {
    let prevented = false;
    await context.document.dispatch("keydown", {
      target, key: "r", ctrlKey: true, altKey: true,
      preventDefault() { prevented = true; },
    });
    assert.deepEqual(clicked, [], "typing targets do not start acquisition");
    assert.equal(prevented, false, "typing targets retain their keyboard behavior");
  }

  for (const event of [
    { key: "r", ctrlKey: true },
    { key: "p", ctrlKey: true },
    { key: "s", ctrlKey: true, shiftKey: true },
    { key: "r", ctrlKey: true, altKey: true, repeat: true },
    { key: "r", ctrlKey: true, altKey: true, isComposing: true },
  ]) {
    let prevented = false;
    await context.document.dispatch("keydown", {
      target: { tagName: "BODY" }, ...event,
      preventDefault() { prevented = true; },
    });
    assert.deepEqual(clicked, [], "browser-native, repeated, and IME-composition keys are ignored");
    assert.equal(prevented, false);
  }

  assert.match(html, /id="applicationShortcutHelp"[^>]*>[\s\S]*?Ctrl\+Alt\+N[\s\S]*?Ctrl\+Alt\+R[\s\S]*?Ctrl\+Alt\+Enter/);
  assert.match(html, /id="newMeasurement"[^>]*aria-keyshortcuts="Control\+Alt\+N"/);
  assert.match(html, /id="openSettings"[^>]*aria-keyshortcuts="Control\+Alt\+Comma"/);
  assert.match(html, /id="start"[^>]*aria-keyshortcuts="Control\+Alt\+R"/);
  assert.match(html, /id="pause"[^>]*aria-keyshortcuts="Control\+Alt\+P"/);
  assert.match(html, /id="resume"[^>]*aria-keyshortcuts="Control\+Alt\+P"/);
  assert.match(html, /id="stop"[^>]*aria-keyshortcuts="Control\+Alt\+S"/);
  assert.match(html, /id="markEvent"[^>]*aria-keyshortcuts="Control\+Alt\+Enter"/);
});

test("keyboard Y-axis zoom goes below the previous fixed floor at large readings", async () => {
  const { api, elements } = createFrontend({ stubChart: false });
  api.app.modes.set("RES2", { value: "RES2", label: "两线电阻", quantity: "resistance", unit: "Ohm", ranges: [], nplc: [], bandwidths: [] });
  api.app.points = Array.from({ length: 3 }, (_, index) => ({
    id: index + 1, x: index, y: 30e6, status: "ok", unit: "Ohm", mode: "RES2",
  }));
  element(elements, "autoFollow").checked = false;
  api.drawChart();
  const canvas = element(elements, "plot");

  for (let index = 0; index < 450; index += 1) {
    await canvas.dispatch("keydown", { key: "+", preventDefault() {} });
  }

  const span = api.app.yView[1] - api.app.yView[0];
  assert.ok(Number.isFinite(span) && span > 0, `keyboard zoom must preserve a finite positive range, got ${span}`);
  assert.ok(span < 1e-30, `keyboard zoom should continue below the previous 1e-24 SI floor, got ${span} Ω`);
});

test("keyboard Y-axis zoom-out keeps the view finite near the numeric range limit", async () => {
  const { api, elements } = createFrontend({ stubChart: false });
  api.app.modes.set("DCV", dcv);
  api.app.points = [{ id: 1, x: 0, y: 0.1, status: "ok", unit: "V", mode: "DCV" }];
  api.drawChart();
  api.app.yView = [-7.5e307, 7.5e307];
  element(elements, "autoFollow").checked = false;
  api.drawChart();

  await element(elements, "plot").dispatch("keydown", { key: "-", preventDefault() {} });

  const range = api.app.yView;
  const span = range[1] - range[0];
  assert.ok(range.every(Number.isFinite), `zoom-out endpoints must remain finite, got ${range}`);
  assert.ok(Number.isFinite(span) && span > 0, `zoom-out span must remain finite and positive, got ${span}`);
  assert.ok(span > 1.5e308, `zoom-out should widen the initial 1.5e308 span without overflowing, got ${span}`);
});

test("one large Y-axis wheel input can expand beyond the former million-fold span cap", async () => {
  const { api, elements } = createFrontend({ stubChart: false });
  api.app.modes.set("DCV", dcv);
  api.app.points = [{ id: 1, x: 0, y: 1, status: "ok", unit: "V", mode: "DCV" }];
  element(elements, "autoFollow").checked = false;
  api.drawChart();
  const initialSpan = api.app.chartScale.yMax - api.app.chartScale.yMin;

  await element(elements, "plot").dispatch("wheel", {
    clientX: 400, clientY: 200, deltaY: 2400, preventDefault() {},
  });

  const range = api.app.yView;
  const span = range[1] - range[0];
  assert.ok(range.every(Number.isFinite), `expanded Y endpoints must stay finite, got ${range}`);
  assert.ok(Number.isFinite(span) && span > initialSpan * 1e7,
    `one large wheel input should exceed the former 1e6 span cap, initial=${initialSpan}, actual=${span}`);
});

test("Y-axis wheel remains finite and centered at the largest representable span", async () => {
  const { api, elements } = createFrontend({ stubChart: false });
  api.app.modes.set("DCV", dcv);
  api.app.points = [{ id: 1, x: 0, y: 1, status: "ok", unit: "V", mode: "DCV" }];
  api.app.yView = [-Number.MAX_VALUE / 2, Number.MAX_VALUE / 2];
  element(elements, "autoFollow").checked = false;
  api.drawChart();
  const initialScale = api.app.chartScale;
  const centerY = (initialScale.top + initialScale.bottom) / 2;

  await element(elements, "plot").dispatch("wheel", {
    clientX: 400, clientY: centerY, deltaY: 120, preventDefault() {},
  });

  const range = api.app.yView;
  const span = range[1] - range[0];
  assert.ok(range.every(Number.isFinite), `Y endpoints must stay finite, got ${range}`);
  assert.ok(Number.isFinite(span) && span > 0, `Y span must stay finite and positive, got ${span}`);
  assert.ok(Math.abs((range[0] + range[1]) / 2) < 1e292, `the centered zoom anchor must not drift, got ${range}`);
});

test("Y-axis zoom-in stays finite when recovering from the full representable span", async () => {
  const { api, elements } = createFrontend({ stubChart: false });
  api.app.modes.set("DCV", dcv);
  api.app.points = [{ id: 1, x: 0, y: 0.1, status: "ok", unit: "V", mode: "DCV" }];
  api.app.yView = [-Number.MAX_VALUE, Number.MAX_VALUE];
  element(elements, "autoFollow").checked = false;
  api.drawChart();

  await element(elements, "plot").dispatch("wheel", {
    clientX: 400, clientY: 200, deltaY: -120, preventDefault() {},
  });

  const range = api.app.yView;
  const span = range[1] - range[0];
  assert.ok(range.every(Number.isFinite), `zoom-in endpoints must stay finite, got ${range}`);
  assert.ok(Number.isFinite(span) && span > 0, `zoom-in span must stay finite and positive, got ${span}`);
});

test("manual Y-axis span applies the requested displayed-unit range around the current center", async () => {
  const { api, elements } = createFrontend({ stubChart: false });
  api.app.modes.set("RES2", { value: "RES2", label: "两线电阻", quantity: "resistance", unit: "Ohm", ranges: [], nplc: [], bandwidths: [] });
  api.app.selectedMode = "RES2";
  api.app.points = Array.from({ length: 3 }, (_, index) => ({
    id: index + 1, x: index, y: 30e6, status: "ok", unit: "Ohm", mode: "RES2",
  }));
  const rawPointsBefore = api.app.points.map((point) => ({ ...point }));
  element(elements, "autoFollow").checked = true;
  api.drawChart();
  const before = api.app.chartScale;
  const center = (before.yMin + before.yMax) / 2;
  const pageMarkup = fs.readFileSync(path.join(ROOT, "src/fluke8845a/web/index.html"), "utf8");
  assert.match(pageMarkup, /id="yAxisSpan"[^>]*title="[^"]*1e-12 MΩ[^"]*"/);
  assert.match(pageMarkup, /id="yAxisSpan"[^>]*title="[^"]*最小跨度受计算机浮点精度限制[^"]*"/);
  assert.match(pageMarkup, /id="yAxisSpan"[^>]*title="[^"]*不会提升仪表分辨率[^"]*"/);
  assert.match(pageMarkup, /id="yAxisSpan"[^>]*title="[^"]*不修改原始读数[^"]*"/);

  element(elements, "yAxisSpan").value = "0.000001";
  await element(elements, "applyYAxisSpan").dispatch("click");

  const range = api.app.yView;
  assert.ok(Array.isArray(range), "applying a custom Y span creates a manual view");
  assert.equal(element(elements, "yAxisSpanUnit").textContent, "Ω");
  assert.equal(element(elements, "autoFollow").checked, false);
  assert.ok(Math.abs((range[1] - range[0]) - 1) < 1e-5, `expected a 1 Ω range from 0.000001 MΩ, got ${range[1] - range[0]} Ω`);
  assert.ok(Math.abs((range[0] + range[1]) / 2 - center) < 1e-7, "manual range stays centered on the current view");
  assert.deepEqual(api.app.points, rawPointsBefore, "changing the axis view leaves raw samples unchanged");
});

test("manual Y-axis span keeps the displayed unit stable while the input is focused", async () => {
  const { api, context, elements } = createFrontend({ stubChart: false });
  api.app.modes.set("RES2", { value: "RES2", label: "两线电阻", quantity: "resistance", unit: "Ohm", ranges: [], nplc: [], bandwidths: [] });
  api.app.selectedMode = "RES2";
  api.app.points = [{ id: 1, x: 0, y: 30e6, status: "ok", unit: "Ohm", mode: "RES2" }];
  api.drawChart();

  const input = element(elements, "yAxisSpan");
  input.value = "0.000001";
  context.document.activeElement = input;
  await input.dispatch("focus");
  api.app.points = [{ id: 1, x: 0, y: 30_000, status: "ok", unit: "Ohm", mode: "RES2" }];
  api.drawChart();

  assert.equal(element(elements, "yAxisSpanUnit").textContent, "MΩ", "the unit label stays paired with the number being edited");
  await element(elements, "applyYAxisSpan").dispatch("click");
  assert.ok(Math.abs((api.app.yView[1] - api.app.yView[0]) - 1) < 1e-6, "the input is converted using the unit shown when editing began");
});

test("manual Y-axis span rejects zero and negative ranges without changing the view", async () => {
  const { api, elements } = createFrontend({ stubChart: false });
  api.app.modes.set("DCV", dcv);
  api.app.points = [{ id: 1, x: 0, y: 1, status: "ok", unit: "V", mode: "DCV" }];
  api.drawChart();
  const before = [...api.app.yView || [api.app.chartScale.yMin, api.app.chartScale.yMax]];

  for (const invalidSpan of ["0", "-1"]) {
    element(elements, "yAxisSpan").value = invalidSpan;
    await element(elements, "applyYAxisSpan").dispatch("click");
    assert.deepEqual(api.app.yView || [api.app.chartScale.yMin, api.app.chartScale.yMax], before);
    assert.match(element(elements, "toast").textContent, /大于 0/);
  }
});

test("manual Y-axis span can go below the former 1e-100 SI floor around a high-magnitude reading", async () => {
  const { api, elements } = createFrontend({ stubChart: false });
  api.app.modes.set("RES2", { value: "RES2", label: "两线电阻", quantity: "resistance", unit: "Ohm", ranges: [], nplc: [], bandwidths: [] });
  api.app.selectedMode = "RES2";
  api.app.points = [{ id: 1, x: 0, y: 30e6, status: "ok", unit: "Ohm", mode: "RES2" }];
  api.drawChart();

  element(elements, "yAxisSpan").value = "1e-206";
  await element(elements, "applyYAxisSpan").dispatch("click");

  const span = api.app.yView[1] - api.app.yView[0];
  assert.ok(Number.isFinite(span) && Math.abs(span / 1e-200 - 1) < 1e-12, `expected a 1e-200 Ω span, got ${span} Ω`);
  assert.equal(api.app.chartScale.yOrigin, 30e6, "the plot keeps a stable absolute reference separate from its offset coordinates");
  assert.ok(api.app.chartScale.yMin < 0 && api.app.chartScale.yMax > 0, "the view is centered on the sample's offset from the origin");
  assert.equal(api.app.points[0].y, 30e6, "raw readings remain unchanged");
  assert.ok(element(elements, "plot").canvasContext.calls.some(([, text]) => String(text).includes("刻度相对 30 MΩ")), "very fine ticks identify their absolute reference");
  assert.doesNotMatch(element(elements, "toast").textContent, /纵轴显示下限/);
});

test("Y-axis units follow the visible zoom span and retain the absolute reference", async () => {
  const { api, elements } = createFrontend({ stubChart: false });
  api.app.modes.set("RES2", { value: "RES2", label: "两线电阻", quantity: "resistance", unit: "Ohm", ranges: [], nplc: [], bandwidths: [] });
  api.app.selectedMode = "RES2";
  api.app.points = [{ id: 1, x: 0, y: 30e6, status: "ok", unit: "Ohm", mode: "RES2" }];
  api.app.yOrigin = 30e6;
  api.app.yView = [-0.05, 0.05];
  api.drawChart();

  assert.equal(api.app.chartScale.axisUnit, "mΩ");
  assert.ok(element(elements, "plot").canvasContext.calls.some(([, text]) => text === "电阻 (mΩ)"));
  assert.ok(element(elements, "plot").canvasContext.calls.some(([, text]) => String(text).includes("刻度相对 30 MΩ")));
  assert.equal(element(elements, "yAxisSpanUnit").textContent, "mΩ");
  assert.ok(Math.abs(Number(element(elements, "yAxisSpan").value) - 100) < 1e-8);
  assert.equal(api.app.points[0].y, 30e6, "axis scaling is display-only and preserves the raw reading");
});

test("subnormal Y-axis spans keep distinct, same-scale tick labels", async () => {
  const { api, elements } = createFrontend({ stubChart: false });
  api.app.modes.set("DCV", dcv);
  api.app.selectedMode = "DCV";
  api.app.points = [{ id: 1, x: 0, y: 0, status: "ok", unit: "V", mode: "DCV" }];
  api.drawChart();

  element(elements, "yAxisSpan").value = "1e-314";
  await element(elements, "applyYAxisSpan").dispatch("click");

  const scale = api.app.chartScale;
  const span = scale.yMax - scale.yMin;
  const labels = element(elements, "plot").canvasContext.calls
    .map(([, text]) => String(text))
    .filter((text) => /e-3\d{2}$/.test(text));
  assert.ok(span > 0 && span < 2e-323, `expected a representable subnormal span, got ${span} V`);
  assert.ok(new Set(labels).size >= 2, `expected distinct tiny tick labels, got ${labels.join(", ")}`);
  assert.ok(labels.every((label) => Math.abs(Number(label) * scale.axisFactor) <= span), `ticks should stay in the same scale as the axis span ${span}: ${labels}`);
});

test("deep zoom on a high resistance reading keeps its unit, input, and ticks representable", async () => {
  const { api, elements, animationFrames } = createFrontend({ stubChart: false });
  api.app.modes.set("RES2", { value: "RES2", label: "两线电阻", quantity: "resistance", unit: "Ohm", ranges: [], nplc: [], bandwidths: [] });
  api.app.selectedMode = "RES2";
  api.app.points = [{ id: 1, x: 0, y: 30e6, status: "ok", unit: "Ohm", mode: "RES2" }];
  element(elements, "autoFollow").checked = false;
  api.drawChart();
  const canvas = element(elements, "plot");
  const sampleY = api.app.chartScale.yToPixel(30e6);

  for (let index = 0; index < 900; index += 1) {
    await canvas.dispatch("wheel", { clientX: 400, clientY: sampleY, deltaY: -120, preventDefault() {} });
  }

  flushAnimationFrames(animationFrames);
  const scale = api.app.chartScale;
  const span = scale.yMax - scale.yMin;
  const labels = canvas.canvasContext.calls.map(([, text]) => String(text)).filter((text) => /e-3\d{2}$/.test(text));
  assert.ok(span > 0 && span < 2e-323, `expected wheel zoom near floating-point resolution, got ${span} Ω`);
  assert.equal(element(elements, "yAxisSpanUnit").textContent, "pΩ", "the field uses an engineering prefix without altering raw SI data");
  assert.ok(Number(element(elements, "yAxisSpan").value) > 0, "the current span remains visible in the editable field");
  assert.ok(new Set(labels).size >= 2, `expected distinct tiny Ω tick labels, got ${labels.join(", ")}`);
});

test("manual fine span rebases a view centered near zero from a 30 MΩ first sample", async () => {
  const { api, elements, animationFrames } = createFrontend({ stubChart: false });
  api.app.modes.set("RES2", { value: "RES2", label: "两线电阻", quantity: "resistance", unit: "Ohm", ranges: [], nplc: [], bandwidths: [] });
  api.app.selectedMode = "RES2";
  api.app.points = [{ id: 1, x: 0, y: 30e6, status: "ok", unit: "Ohm", mode: "RES2" }];
  api.drawChart();
  api.app.yView = [-30e6 - 1, -30e6 + 1];
  api.drawChart();

  element(elements, "yAxisSpan").value = "1e-100";
  await element(elements, "applyYAxisSpan").dispatch("click");

  const span = api.app.chartScale.yMax - api.app.chartScale.yMin;
  assert.equal(api.app.chartScale.yOrigin, 0, "the plot origin follows the absolute view center near zero");
  assert.ok(Math.abs(span / 1e-100 - 1) < 1e-12, `expected 1e-100 Ω around absolute zero, got ${span} Ω`);
  assert.equal(api.app.points[0].y, 30e6, "rebasing never changes the raw measurement");
});

test("fine-scale Y-axis cursor, baseline, and raw sample share the same origin offset", async () => {
  const { api, elements } = createFrontend({ stubChart: false });
  api.app.modes.set("RES2", { value: "RES2", label: "两线电阻", quantity: "resistance", unit: "Ohm", ranges: [], nplc: [], bandwidths: [] });
  api.app.selectedMode = "RES2";
  api.app.points = [{ id: 1, x: 0, y: 30e6, status: "ok", unit: "Ohm", mode: "RES2" }];
  api.app.baseline = 30e6;
  api.drawChart();
  element(elements, "yAxisSpan").value = "1e-30";
  await element(elements, "applyYAxisSpan").dispatch("click");

  const scale = api.app.chartScale;
  assert.equal(scale.yOrigin, 30e6, "chartScale exposes the stable absolute-to-offset origin");
  assert.ok(Math.abs(scale.yToPixel(30e6) - (scale.top + scale.bottom) / 2) < 1e-8, "the raw reading maps through its origin offset");
  const baselineStroke = element(elements, "plot").canvasContext.strokes.find((stroke) => stroke.dash.length > 0
    && stroke.path.length === 2 && Math.abs(stroke.path[0][2] - stroke.path[1][2]) < 1e-8);
  assert.ok(baselineStroke && Math.abs(baselineStroke.path[0][2] - (scale.top + scale.bottom) / 2) < 1e-8, "the absolute baseline is translated into the same offset view");
  api.app.cursor = { x: (scale.left + scale.right) / 2, y: (scale.top + scale.bottom) / 2 };
  api.drawChart();
  assert.ok(element(elements, "plotOverlay").canvasContext.calls.some(([, text]) => String(text).includes("30 MΩ")), "the inverse cursor mapping restores the absolute reading");
  assert.equal(api.app.points[0].y, 30e6, "drawing and cursor inspection do not rewrite the raw point");
});

test("manual Y-axis span near zero is independent of a distant outlier", async () => {
  const { api, elements } = createFrontend({ stubChart: false });
  api.app.modes.set("DCV", dcv);
  api.app.points = [
    { id: 1, x: 0, y: 0, status: "ok", unit: "V", mode: "DCV" },
    { id: 2, x: 1, y: 1e12, status: "ok", unit: "V", mode: "DCV" },
  ];
  api.app.yView = [-1e12, 1e12];
  element(elements, "autoFollow").checked = false;
  api.drawChart();

  element(elements, "yAxisSpan").value = "1e-15";
  await element(elements, "applyYAxisSpan").dispatch("click");

  const span = api.app.yView[1] - api.app.yView[0];
  assert.ok(Math.abs(span - 1e-12) < 1e-18, `expected a 1 pV span around zero, got ${span} V`);
});

test("chart axis title includes the measurement quantity and SI unit", () => {
  const { api, elements } = createFrontend({ stubChart: false });
  api.app.modes.set("RES2", { value: "RES2", label: "两线电阻", quantity: "resistance", unit: "Ohm", ranges: [], nplc: [], bandwidths: [] });
  api.app.selectedMode = "RES2";
  api.app.points = [{ id: 1, x: 0, y: 30e6, status: "ok", unit: "Ohm", mode: "RES2" }];
  element(elements, "autoFollow").checked = false;

  api.drawChart();

  assert.ok(element(elements, "plot").canvasContext.calls.some(([, text]) => text === "电阻 (MΩ)"));
  assert.ok(element(elements, "plot").canvasContext.calls.some(([, text, , , font]) => text !== "电阻 (MΩ)" && /12px/.test(font || "")), "axis labels use a readable 12px canvas font");
});

test("narrow Y-axis ranges keep enough tick precision for large readings", () => {
  const { api, elements } = createFrontend({ stubChart: false });
  api.app.modes.set("RES2", { value: "RES2", label: "两线电阻", quantity: "resistance", unit: "Ohm", ranges: [], nplc: [], bandwidths: [] });
  api.app.points = [
    { id: 1, x: 0, y: 29_999_999.8, status: "ok", unit: "Ohm", mode: "RES2" },
    { id: 2, x: 1, y: 30_000_000.2, status: "ok", unit: "Ohm", mode: "RES2" },
  ];
  api.app.yView = [-0.5, 0.5];
  element(elements, "autoFollow").checked = false;

  api.drawChart();

  const labels = element(elements, "plot").canvasContext.calls
    .map(([, text]) => String(text))
    .filter((text) => /^[-+]?\d+(?:\.\d+)?$/.test(text));
  assert.ok(element(elements, "plot").canvasContext.calls.some(([, text]) => String(text).includes("刻度相对 30 MΩ")));
  assert.ok(labels.some((label) => Number(label) < 1), `expected relative sub-ohm tick labels, got ${labels.join(", ")}`);
});

test("deep Y-axis zoom keeps adjacent high-magnitude tick labels distinct as relative offsets", () => {
  const { api, elements } = createFrontend({ stubChart: false });
  api.app.modes.set("RES2", { value: "RES2", label: "两线电阻", quantity: "resistance", unit: "Ohm", ranges: [], nplc: [], bandwidths: [] });
  api.app.points = [{ id: 1, x: 0, y: 30e6, status: "ok", unit: "Ohm", mode: "RES2" }];
  const minimumSpan = 30e6 * Number.EPSILON;
  api.app.yView = [-minimumSpan / 2, minimumSpan / 2];
  element(elements, "autoFollow").checked = false;

  api.drawChart();

  const labels = element(elements, "plot").canvasContext.calls
    .map(([, text]) => String(text))
    .filter((text) => /^[-+]?\d+(?:\.\d+)?(?:e[+-]?\d+)?$/.test(text));
  const widestLabel = Math.max(0, ...labels.map((label) => label.length * 6));
  assert.ok(element(elements, "plot").canvasContext.calls.some(([, text]) => String(text).includes("刻度相对 30 MΩ")), "relative ticks identify the absolute origin");
  assert.ok(new Set(labels).size > 1, `adjacent Y ticks must be distinct, got ${labels.join(", ")}`);
  assert.equal(api.app.chartScale.axisUnit, "nΩ");
  assert.ok(api.app.chartScale.left >= widestLabel + 9, "long precise labels remain inside the canvas");
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

test("rapid wheel events preserve every zoom step and render once per animation frame", async () => {
  const { api, elements, animationFrames } = createFrontend({ stubChart: false });
  api.app.modes.set("DCV", dcv);
  api.app.points = Array.from({ length: 101 }, (_, index) => ({ id: index + 1, x: index, y: index, status: "ok", unit: "V" }));
  const canvas = element(elements, "plot");
  element(elements, "autoFollow").checked = false;
  api.drawChart();
  const initialSpan = api.app.chartScale.xMax - api.app.chartScale.xMin;
  let plotRenders = 0;
  canvas.canvasContext.clearRect = () => { plotRenders += 1; };

  for (let index = 0; index < 4; index += 1) {
    await canvas.dispatch("wheel", {
      clientX: 400, clientY: 200, deltaY: 120, shiftKey: true, preventDefault() {},
    });
  }

  assert.equal(animationFrames.length, 1, "the burst schedules only one chart redraw");
  assert.equal(plotRenders, 0, "wheel handlers do not synchronously redraw the chart");
  animationFrames.shift()();
  assert.equal(plotRenders, 1, "the animation frame renders the chart once");
  const zoomedSpan = api.app.xView[1] - api.app.xView[0];
  const expectedSpan = initialSpan * Math.exp(4 * 0.9);
  assert.ok(Math.abs(zoomedSpan / expectedSpan - 1) < 1e-10,
    `all four zoom increments are retained; expected ${expectedSpan}, got ${zoomedSpan}`);
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

  const { api, elements, animationFrames } = createFrontend({ stubChart: false });
  api.app.points = Array.from({ length: 11 }, (_, index) => ({ id: index + 1, x: index, y: index + 10, status: "ok", unit: "V" }));
  element(elements, "autoFollow").checked = false;
  api.drawChart();
  const canvas = element(elements, "plot");
  const bounds = () => ({
    x: [...(api.app.xView || [api.app.chartScale.xMin, api.app.chartScale.xMax])],
    y: [...(api.app.yView || [api.app.chartScale.yMin, api.app.chartScale.yMax])],
    yOrigin: api.app.chartScale.yOrigin,
  });
  const zoom = async (modifiers) => {
    const before = bounds();
    await canvas.dispatch("wheel", { clientX: 400, clientY: 200, deltaY: -120, preventDefault() {}, ...modifiers });
    flushAnimationFrames(animationFrames);
    const after = bounds();
    return { before, after };
  };

  const plain = await zoom({});
  assert.deepEqual(plain.after.x, plain.before.x);
  assert.ok(plain.after.y[1] - plain.after.y[0] < plain.before.y[1] - plain.before.y[0]);
  const gui = fs.readFileSync(path.join(ROOT, "src/fluke8845a/gui.py"), "utf8");
  assert.match(gui, /zoom_sensitivity = 0\.18 if modifiers & Qt\.KeyboardModifier\.AltModifier else 0\.9/);
  assert.match(gui, /factor = math\.exp\(-float\(delta\) \/ 120\.0 \* zoom_sensitivity\)/);
  const webFactor = (plain.after.y[1] - plain.after.y[0]) / (plain.before.y[1] - plain.before.y[0]);
  assert.ok(Math.abs(webFactor - Math.exp(-0.9)) < 1e-12, "one upward wheel notch uses the desktop zoom factor");

  const shift = await zoom({ shiftKey: true });
  assert.ok(shift.after.x[1] - shift.after.x[0] < shift.before.x[1] - shift.before.x[0]);
  assert.deepEqual(shift.after.y, shift.before.y);

  const alt = await zoom({ altKey: true });
  const altFactor = (alt.after.y[1] - alt.after.y[0]) / (alt.before.y[1] - alt.before.y[0]);
  assert.ok(Math.abs(altFactor - Math.exp(-0.18)) < 1e-12, "Alt keeps the finer zero-anchored wheel step");

  const ctrl = await zoom({ ctrlKey: true });
  assert.ok(ctrl.after.x[1] - ctrl.after.x[0] < ctrl.before.x[1] - ctrl.before.x[0]);
  assert.ok(ctrl.after.y[1] - ctrl.after.y[0] < ctrl.before.y[1] - ctrl.before.y[0]);

  const altOverrides = await zoom({ altKey: true, ctrlKey: true, shiftKey: true });
  assert.deepEqual(altOverrides.after.x, altOverrides.before.x);
  assert.ok(altOverrides.after.y[1] - altOverrides.after.y[0] < altOverrides.before.y[1] - altOverrides.before.y[0]);
  const zeroBefore = (-(altOverrides.before.yOrigin || 0) - altOverrides.before.y[0]) / (altOverrides.before.y[1] - altOverrides.before.y[0]);
  const zeroAfter = (-(altOverrides.after.yOrigin || 0) - altOverrides.after.y[0]) / (altOverrides.after.y[1] - altOverrides.after.y[0]);
  assert.ok(Math.abs(zeroAfter - zeroBefore) < 1e-12, `Alt keeps zero at the same vertical screen position while zooming (${zeroBefore} → ${zeroAfter}, origins ${altOverrides.before.yOrigin} → ${altOverrides.after.yOrigin})`);
});

test("a short run of wheel steps can shrink the Y span by at least six orders of magnitude", async () => {
  const { api, elements, animationFrames } = createFrontend({ stubChart: false });
  api.app.modes.set("RES2", { value: "RES2", label: "两线电阻", quantity: "resistance", unit: "Ohm", ranges: [], nplc: [], bandwidths: [] });
  api.app.points = [{ id: 1, x: 0, y: 30e6, status: "ok", unit: "Ohm", mode: "RES2" }];
  element(elements, "autoFollow").checked = false;
  api.drawChart();
  const initialSpan = api.app.chartScale.yMax - api.app.chartScale.yMin;
  const canvas = element(elements, "plot");

  for (let index = 0; index < 16; index += 1) {
    await canvas.dispatch("wheel", { clientX: 400, clientY: 200, deltaY: -120, preventDefault() {} });
  }

  flushAnimationFrames(animationFrames);
  const span = api.app.chartScale.yMax - api.app.chartScale.yMin;
  assert.ok(span > 0 && Number.isFinite(span), `Y span must stay finite and positive, got ${span} Ω`);
  assert.ok(span < initialSpan * 1e-6, `16 upward notches should shrink the view at least 1,000,000×; ${initialSpan} → ${span} Ω`);
});

test("Y-axis wheel zoom reaches a few floating-point steps without collapsing at large readings", async () => {
  const { api, elements, animationFrames } = createFrontend({ stubChart: false });
  api.app.modes.set("RES2", { value: "RES2", label: "两线电阻", quantity: "resistance", unit: "Ohm", ranges: [], nplc: [], bandwidths: [] });
  api.app.points = Array.from({ length: 3 }, (_, index) => ({
    id: index + 1, x: index, y: 30e6, status: "ok", unit: "Ohm", mode: "RES2",
  }));
  element(elements, "autoFollow").checked = false;
  api.drawChart();
  const canvas = element(elements, "plot");

  for (let index = 0; index < 240; index += 1) {
    await canvas.dispatch("wheel", { clientX: 400, clientY: 200, deltaY: -120, preventDefault() {} });
  }

  flushAnimationFrames(animationFrames);
  const span = api.app.chartScale.yMax - api.app.chartScale.yMin;
  assert.ok(span < 1e-8, `expected Y span below 10 nΩ, got ${span} Ω`);
  assert.ok(span > 0 && Number.isFinite(span), `Y span must remain finite and nonzero, got ${span}`);
});

test("Y-axis wheel can zoom below the former 1e-100 SI floor near a 30 MΩ sample", async () => {
  const { api, elements, animationFrames } = createFrontend({ stubChart: false });
  api.app.modes.set("RES2", { value: "RES2", label: "两线电阻", quantity: "resistance", unit: "Ohm", ranges: [], nplc: [], bandwidths: [] });
  api.app.points = [{ id: 1, x: 0, y: 30e6, status: "ok", unit: "Ohm", mode: "RES2" }];
  element(elements, "autoFollow").checked = false;
  api.drawChart();
  const canvas = element(elements, "plot");
  const sampleY = api.app.chartScale.yToPixel(30e6);

  api.app.yView = [-5e-91, 5e-91];
  api.drawChart();

  for (let index = 0; index < 200; index += 1) {
    await canvas.dispatch("wheel", { clientX: 400, clientY: sampleY, deltaY: -120, preventDefault() {} });
  }

  flushAnimationFrames(animationFrames);
  const span = api.app.chartScale.yMax - api.app.chartScale.yMin;
  assert.ok(Number.isFinite(span) && span > 0, `Y span must remain finite and nonzero, got ${span} Ω`);
  assert.ok(span < 1e-100, `expected wheel zoom to pass the former 1e-100 Ω floor, got ${span} Ω`);
  assert.ok(api.app.chartScale.yMin < 0 && api.app.chartScale.yMax > 0, "the sample remains centered in its offset coordinates");
});

test("Y-axis wheel zoom at milliamp readings is not clamped to the one-amp precision floor", async () => {
  const { api, elements, animationFrames } = createFrontend({ stubChart: false });
  api.app.modes.set("DCI", { value: "DCI", label: "直流电流", quantity: "current", unit: "A", ranges: [], nplc: [], bandwidths: [] });
  api.app.points = Array.from({ length: 3 }, (_, index) => ({
    id: index + 1, x: index, y: 5e-3, status: "ok", unit: "A", mode: "DCI",
  }));
  element(elements, "autoFollow").checked = false;
  api.drawChart();
  const canvas = element(elements, "plot");

  for (let index = 0; index < 240; index += 1) {
    await canvas.dispatch("wheel", { clientX: 400, clientY: 200, deltaY: -120, preventDefault() {} });
  }

  flushAnimationFrames(animationFrames);
  const span = api.app.chartScale.yMax - api.app.chartScale.yMin;
  assert.ok(Number.isFinite(span) && span > 0, `Y span must remain finite and nonzero, got ${span} A`);
  assert.ok(span < 1e-17, `expected milliamp readings to zoom below the old absolute floor, got ${span} A`);
});

test("Alt-wheel can zoom near zero despite a distant high-magnitude outlier", async () => {
  const { api, elements, animationFrames } = createFrontend({ stubChart: false });
  api.app.modes.set("DCV", dcv);
  api.app.points = [
    { id: 1, x: 0, y: 0, status: "ok", unit: "V", mode: "DCV" },
    { id: 2, x: 1, y: 1e-8, status: "ok", unit: "V", mode: "DCV" },
    { id: 3, x: 2, y: 1e12, status: "ok", unit: "V", mode: "DCV" },
  ];
  element(elements, "autoFollow").checked = false;
  api.drawChart();
  const canvas = element(elements, "plot");

  for (let index = 0; index < 240; index += 1) {
    await canvas.dispatch("wheel", { clientX: 400, clientY: 200, deltaY: -120, altKey: true, preventDefault() {} });
  }

  flushAnimationFrames(animationFrames);
  const span = api.app.chartScale.yMax - api.app.chartScale.yMin;
  assert.ok(api.app.chartScale.yMin <= 0 && api.app.chartScale.yMax >= 0, "Alt-wheel keeps zero inside the visible Y range");
  assert.ok(api.app.chartScale.yMin <= 1e-8 && api.app.chartScale.yMax >= 1e-8, "the nearby low reading remains inside the zoomed Y range");
  assert.ok(Number.isFinite(span) && span > 0, `Y span must remain finite and nonzero, got ${span} V`);
  assert.ok(span < 1e-5, `expected Alt-wheel to zoom below the outlier-based floor, got ${span} V`);
});

test("Alt-wheel rebases a 30 MΩ plot before zooming around absolute zero", async () => {
  const { api, elements, animationFrames } = createFrontend({ stubChart: false });
  api.app.modes.set("RES2", { value: "RES2", label: "两线电阻", quantity: "resistance", unit: "Ohm", ranges: [], nplc: [], bandwidths: [] });
  api.app.points = [{ id: 1, x: 0, y: 30e6, status: "ok", unit: "Ohm", mode: "RES2" }];
  element(elements, "autoFollow").checked = false;
  api.drawChart();
  const canvas = element(elements, "plot");

  for (let index = 0; index < 600; index += 1) {
    await canvas.dispatch("wheel", { clientX: 400, clientY: 200, deltaY: -120, altKey: true, preventDefault() {} });
  }

  flushAnimationFrames(animationFrames);
  const scale = api.app.chartScale;
  const span = scale.yMax - scale.yMin;
  assert.equal(scale.yOrigin, 0, "Alt-wheel zero is the new coordinate origin");
  assert.ok(Number.isFinite(span) && span > 0, `Alt-wheel span must remain finite and positive, got ${span} Ω`);
  assert.ok(span < 1e-30, `expected Alt-wheel to zoom below the previous 1e-24 Ω floor around zero, got ${span} Ω`);
  assert.equal(api.app.points[0].y, 30e6, "the sample remains in its original absolute units");
});

test("touch pinch can narrow the Y axis to a few floating-point steps at large readings", async () => {
  const { api, elements } = createFrontend({ stubChart: false });
  api.app.modes.set("RES2", { value: "RES2", label: "两线电阻", quantity: "resistance", unit: "Ohm", ranges: [], nplc: [], bandwidths: [] });
  api.app.points = Array.from({ length: 3 }, (_, index) => ({
    id: index + 1, x: index, y: 30e6, status: "ok", unit: "Ohm", mode: "RES2",
  }));
  api.app.yView = [-1e-4, 1e-4];
  element(elements, "autoFollow").checked = false;
  api.drawChart();
  const canvas = element(elements, "plot");

  await canvas.dispatch("pointerdown", { pointerId: 1, pointerType: "touch", clientX: 150, clientY: 180 });
  await canvas.dispatch("pointerdown", { pointerId: 2, pointerType: "touch", clientX: 250, clientY: 180 });
  await canvas.dispatch("pointermove", { pointerId: 2, pointerType: "touch", clientX: 100_250, clientY: 180, preventDefault() {} });
  await canvas.dispatch("pointermove", { pointerId: 2, pointerType: "touch", clientX: 1_000_150, clientY: 180, preventDefault() {} });
  await canvas.dispatch("pointermove", { pointerId: 2, pointerType: "touch", clientX: 10_000_150, clientY: 180, preventDefault() {} });

  const span = api.app.yView[1] - api.app.yView[0];
  assert.ok(span < 1e-8, `expected pinch Y span below 10 nΩ, got ${span} Ω`);
  assert.ok(span > 0 && Number.isFinite(span), `Y span must remain finite and nonzero, got ${span}`);
});

test("manual zoom of either axis turns off auto-follow and updates the visible status", async () => {
  const { api, elements, animationFrames } = createFrontend({ stubChart: false });
  api.app.points = Array.from({ length: 21 }, (_, index) => ({ id: index + 1, x: index, y: index + 1, status: "ok", unit: "V" }));
  element(elements, "autoFollow").checked = true;
  api.drawChart();

  await element(elements, "plot").dispatch("wheel", { clientX: 400, clientY: 200, deltaY: -120, preventDefault() {} });
  flushAnimationFrames(animationFrames);

  assert.equal(element(elements, "autoFollow").checked, false);
  assert.match(element(elements, "viewModeStatus").textContent, /自动跟随已关闭/);
  const html = fs.readFileSync(path.join(ROOT, "src/fluke8845a/web/index.html"), "utf8");
  assert.match(html, /手动缩放或平移任一坐标轴.*自动跟随关闭/);
});

test("Shift-wheel time-axis zoom turns off auto-follow and preserves the reading axis", async () => {
  const { api, elements, animationFrames } = createFrontend({ stubChart: false });
  api.app.points = Array.from({ length: 101 }, (_, index) => ({ id: index + 1, x: index, y: index + 1, status: "ok", unit: "V" }));
  element(elements, "autoFollow").checked = true;
  api.drawChart();
  const initialXSpan = api.app.chartScale.xMax - api.app.chartScale.xMin;
  const initialYRange = [api.app.chartScale.yMin, api.app.chartScale.yMax];

  await element(elements, "plot").dispatch("wheel", {
    clientX: 400, clientY: 200, deltaY: -120, shiftKey: true, preventDefault() {},
  });
  flushAnimationFrames(animationFrames);

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

test("full Web layout fits narrow and tablet viewports and keeps touch targets accessible in Chromium", async (t) => {
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
  assert.match(html, /<button id="openFiles"[^>]*aria-label="实验文件"/);
  const childDocument = html.replace("</body>", "</body>").replace(/<script[\s\S]*?<\/script>/g, "");
  const probe = `<!doctype html><meta charset="utf-8"><pre id="result">pending</pre><script>
    const source = ${JSON.stringify(childDocument)};
    const widths = [280, 349, 350, 351, 580, 581, 600, 640, 641, 820, 821, 900, 901];
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
      const connectionBadge = doc.querySelector("#connectionBadge");
      connectionBadge.classList.add("connected");
      connectionBadge.querySelector("span").textContent = "已连接 · FLUKE,8845A,TEST8845A001,02/14/08-08:54";
      const measured = [".topbar", ".topbar-right", "#connectionBadge", "#openFiles", "#openSettings", "#shutdownService", ".workspace", ".measurement-area", ".readout-panel", ".chart-panel", ".plot-wrap", ".chart-context", ".chart-context-actions", "#pointCursor", "#chartGestureHint", ".chart-actions .toggle-label"]
        .map((selector) => { const node = doc.querySelector(selector); const rect = node.getBoundingClientRect(); return [selector, rect.left, rect.right]; });
      const overflowing = [...doc.body.querySelectorAll("*")].map((node) => {
        const rect = node.getBoundingClientRect();
        return [node.id || node.className || node.tagName, rect.left, rect.right, node.scrollWidth, node.clientWidth];
      }).filter(([, left, right]) => left < -1 || right > doc.documentElement.clientWidth + 1).slice(0, 12);
      const targetRect = doc.querySelector(".chart-actions .toggle-label").getBoundingClientRect();
      const readTouchTargets = () => [...doc.querySelectorAll("button, .file-button, .dialog-nav a, details > summary, .option-check, .confirm-row, .toggle-label")]
        .filter((node) => {
          const rect = node.getBoundingClientRect();
          return getComputedStyle(node).display !== "none" && rect.width > 0 && rect.height > 0;
        })
        .map((node) => { const rect = node.getBoundingClientRect(); return [node.id || node.className || node.parentElement.id || "summary", rect.width, rect.height]; });
      doc.querySelector("#settingsDialog").showModal();
      const criticalError = doc.querySelector("#criticalError");
      criticalError.classList.remove("hidden");
      criticalError.textContent = "测试错误提示";
      const settingsDialog = doc.querySelector("#settingsDialog");
      const settingsRect = settingsDialog.getBoundingClientRect();
      const settingsScrollRect = settingsDialog.querySelector(".settings-scroll").getBoundingClientRect();
      const settingsFooterRect = settingsDialog.querySelector(".dialog-footer").getBoundingClientRect();
      const settingsTouchTargets = readTouchTargets();
      settingsDialog.close();
      const filesDialog = doc.querySelector("#filesDialog");
      filesDialog.showModal();
      const filesRect = filesDialog.getBoundingClientRect();
      const filesListRect = filesDialog.querySelector("#filesList").getBoundingClientRect();
      const filesFooterRect = filesDialog.querySelector(".files-dialog-footer").getBoundingClientRect();
      const filesTouchTargets = readTouchTargets();
      resolve({ viewport, bodyMinWidth: getComputedStyle(doc.body).minWidth, bodyClient: doc.body.clientWidth, bodyScroll: doc.body.scrollWidth,
        documentClient: doc.documentElement.clientWidth, documentScroll: doc.documentElement.scrollWidth,
        touchTarget: [targetRect.width, targetRect.height], touchTargets: [...settingsTouchTargets, ...filesTouchTargets],
        dialogs: [
          { id: "settingsDialog", left: settingsRect.left, right: settingsRect.right, top: settingsRect.top, bottom: settingsRect.bottom,
            scrollLeft: settingsScrollRect.left, scrollRight: settingsScrollRect.right, footerLeft: settingsFooterRect.left, footerRight: settingsFooterRect.right },
          { id: "filesDialog", left: filesRect.left, right: filesRect.right, top: filesRect.top, bottom: filesRect.bottom,
            contentLeft: filesListRect.left, contentRight: filesListRect.right, footerLeft: filesFooterRect.left, footerRight: filesFooterRect.right },
        ], measured, overflowing });
    })))).then((metrics) => { document.getElementById("result").textContent = JSON.stringify(metrics); });
  </script>`;
  const metrics = await inspectChromiumDom(browser, probe);
  assert.equal(metrics.length, 13);
  for (const layout of metrics) {
    assert.equal(layout.bodyScroll, layout.bodyClient, `${layout.viewport}px body has no horizontal overflow: ${JSON.stringify(layout)}`);
    assert.equal(layout.documentScroll, layout.documentClient, `${layout.viewport}px document has no horizontal overflow`);
    assert.ok(layout.documentClient <= layout.viewport, `${layout.viewport}px document client area stays within the viewport`);
    assert.deepEqual(layout.overflowing, [], `${layout.viewport}px visible content does not cross the viewport edge`);
    assert.equal(layout.bodyMinWidth, layout.viewport <= 350 ? "0px" : "280px", `${layout.viewport}px min-width breakpoint is applied at the intended boundary`);
    if (layout.viewport <= 820) {
      assert.ok(layout.touchTarget[0] >= 44, `${layout.viewport}px chart toggle target is at least 44 CSS px wide`);
      assert.ok(layout.touchTarget[1] >= 44, `${layout.viewport}px chart toggle target is at least 44 CSS px tall`);
      for (const [selector, width, height] of layout.touchTargets) {
        assert.ok(width >= 44, `${layout.viewport}px ${selector} target is at least 44 CSS px wide, got ${width}`);
        assert.ok(height >= 44, `${layout.viewport}px ${selector} target is at least 44 CSS px tall, got ${height}`);
      }
    }
    for (const dialog of layout.dialogs) {
      assert.ok(dialog.left >= -1 && dialog.right <= layout.documentClient + 1, `${layout.viewport}px ${dialog.id} fits the viewport`);
      assert.ok(dialog.top >= -1 && dialog.bottom <= 900 + 1, `${layout.viewport}px ${dialog.id} fits the probe height`);
      const left = dialog.scrollLeft ?? dialog.contentLeft;
      const right = dialog.scrollRight ?? dialog.contentRight;
      assert.ok(left >= dialog.left - 1 && right <= dialog.right + 1, `${layout.viewport}px ${dialog.id} content fits its dialog`);
      assert.ok(dialog.footerLeft >= dialog.left - 1 && dialog.footerRight <= dialog.right + 1, `${layout.viewport}px ${dialog.id} footer fits its dialog`);
    }
    for (const [selector, left, right] of layout.measured) {
      assert.ok(left >= -1 && right <= layout.documentClient + 1, `${selector} stays within ${layout.viewport}px viewport: ${left}..${right}`);
    }
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

test("diagnostic logs can be filtered and the export button downloads only the selected source", async () => {
  const { api, elements, context } = createFrontend();
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

  let exportedBlob;
  let revokedUrl;
  let downloadedLink;
  context.Blob = class {
    constructor(parts, options) { this.parts = parts; this.type = options.type; }
    async text() { return this.parts.join(""); }
  };
  context.URL = {
    createObjectURL(blob) { exportedBlob = blob; return "blob:test-logs"; },
    revokeObjectURL(url) { revokedUrl = url; },
  };
  const createElement = context.document.createElement.bind(context.document);
  context.document.createElement = (tag) => {
    const created = createElement(tag);
    if (tag === "a") created.click = () => { downloadedLink = { href: created.href, download: created.download }; };
    return created;
  };
  api.bindControls();
  api.app.logRows = rows;
  element(elements, "logFilter").value = "scpi";
  await element(elements, "logFilter").dispatch("change");
  await element(elements, "exportLogs").dispatch("click");

  assert.equal(exportedBlob.type, "text/plain;charset=utf-8");
  assert.match(await exportedBlob.text(), /\[SCPI\] > SENS:FUNC\?/);
  assert.doesNotMatch(await exportedBlob.text(), /timeout|COM3|开始实验/);
  assert.equal(downloadedLink.href, "blob:test-logs");
  assert.match(downloadedLink.download, /^8845A_scpi_logs_.*\.txt$/);
  assert.equal(revokedUrl, "blob:test-logs");
  assert.match(element(elements, "toast").textContent, /已导出 1 条日志/);
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
  assert.match(javascript, /\/api\/latest-events-safe\.csv/);
  assert.match(javascript, /已导出事件安全副本；原始 events\.csv 未修改/);
  const { api, elements } = createFrontend();
  api.updateStateDisplay(connectedState({
    status: "连续采集中", experiment_dir: "D:\\experiments\\active", last_experiment_dir: "D:\\experiments\\active",
  }));
  assert.equal(element(elements, "downloadEventsSafe").disabled, true);
  assert.match(element(elements, "downloadEventsSafe").title, /停止.*采集/);
  api.updateStateDisplay(connectedState({ status: "等待开始", experiment_dir: "", last_experiment_dir: "D:\\experiments\\done" }));
  assert.equal(element(elements, "downloadEventsSafe").disabled, false);
  assert.match(element(elements, "downloadEventsSafe").title, /原始 events\.csv 不会更改/);
});

test("measurement export is a separate safety copy and stays disabled during acquisition", () => {
  const html = fs.readFileSync(path.join(ROOT, "src/fluke8845a/web/index.html"), "utf8");
  const javascript = fs.readFileSync(path.join(ROOT, "src/fluke8845a/web/app.js"), "utf8");
  assert.match(html, /id="downloadMeasurementsSafe"[^>]*disabled[^>]*原始 measurements\.csv 不会更改/);
  assert.match(javascript, /\/api\/latest-measurements-safe\.csv/);
  assert.match(javascript, /已导出测量安全副本；原始 measurements\.csv 未修改/);
  const { api, elements } = createFrontend();
  api.updateStateDisplay(connectedState({ status: "连续采集中", experiment_dir: "D:\\experiments\\active", last_experiment_dir: "D:\\experiments\\active" }));
  assert.equal(element(elements, "downloadMeasurementsSafe").disabled, true);
  assert.match(element(elements, "downloadMeasurementsSafe").title, /停止.*采集/);
  api.updateStateDisplay(connectedState({ status: "等待开始", experiment_dir: "", last_experiment_dir: "D:\\experiments\\done" }));
  assert.equal(element(elements, "downloadMeasurementsSafe").disabled, false);
  assert.match(element(elements, "downloadMeasurementsSafe").title, /原始 measurements\.csv 不会更改/);
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
