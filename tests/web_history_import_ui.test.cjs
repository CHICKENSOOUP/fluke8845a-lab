"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const ROOT = path.resolve(__dirname, "..");

class FakeElement {
  constructor(id) {
    this.id = id;
    this.value = "";
    this.textContent = "";
    this.innerHTML = "";
    this.checked = false;
    this.disabled = false;
    this.listeners = new Map();
    this.classList = {
      values: new Set(),
      add: (name) => this.classList.values.add(name),
      remove: (name) => this.classList.values.delete(name),
      toggle: (name, force) => {
        const enabled = force === undefined ? !this.classList.values.has(name) : Boolean(force);
        if (enabled) this.classList.values.add(name);
        else this.classList.values.delete(name);
        return enabled;
      },
      contains: (name) => this.classList.values.has(name),
    };
  }
  addEventListener(type, handler) {
    const items = this.listeners.get(type) || [];
    items.push(handler);
    this.listeners.set(type, items);
  }
  getBoundingClientRect() { return { width: 640, height: 360, left: 0, top: 0 }; }
}

function createFrontend() {
  const elements = new Map();
  const workers = [];
  const document = {
    getElementById(id) {
      if (!elements.has(id)) elements.set(id, new FakeElement(id));
      return elements.get(id);
    },
    querySelector() { return new FakeElement("query"); },
  };
  class DeferredWorker {
    constructor(url) { this.url = url; this.messages = []; this.terminated = false; workers.push(this); }
    postMessage(message) { this.messages.push(message); }
    terminate() { this.terminated = true; }
  }
  const context = vm.createContext({
    document,
    window: { devicePixelRatio: 1, addEventListener() {} },
    Worker: DeferredWorker,
    URLSearchParams,
    crypto: { randomUUID: () => "history-job-test" },
    setTimeout: () => 1,
    clearTimeout() {},
    requestAnimationFrame(callback) { callback(); },
    fetch: async () => { throw new Error("not used by import tests"); },
    console,
  });
  const source = fs.readFileSync(path.join(ROOT, "src/fluke8845a/web/app.js"), "utf8")
    .replace(/\ninitialize\(\);\s*$/, "");
  vm.runInContext(`${source}\n;globalThis.__historyUi = { app, ui, loadHistory, cancelHistoryImport, updateStats };`, context);
  vm.runInContext("drawChart = () => {}; updateAccessiblePointTable = () => {}; updateChartAccessibilitySummary = () => {}; renderEvents = () => {};", context);
  return { api: context.__historyUi, context, elements, workers };
}

function element(elements, id) {
  if (!elements.has(id)) elements.set(id, new FakeElement(id));
  return elements.get(id);
}

test("history import streams progress for large files and uses exact worker statistics", async () => {
  const { api, elements, workers } = createFrontend();
  api.app.modes.set("DCV", { value: "DCV", label: "直流电压", unit: "V" });
  const file = { name: "long-run.csv", size: 1024 ** 3 };
  let settled = false;
  const pending = api.loadHistory(file).then((result) => { settled = true; return result; });
  const worker = workers[0];
  const request = worker.messages[0];

  assert.match(String(worker.url), /history_worker\.js/);
  assert.equal(request.file, file);
  assert.equal(request.reportProgress, true);
  assert.equal(request.pixelWidth, 640);
  assert.equal(request.jobId, "history-job-test");
  worker.onmessage({ data: {
    type: "progress", jobId: request.jobId, phase: "validate", bytesRead: 64 * 1024 * 1024,
    totalBytes: file.size, rowsRead: 100000, progress: 0.03125,
  } });
  await Promise.resolve();

  assert.equal(settled, false, "progress messages must not finish the import");
  assert.match(element(elements, "historyImportProgress").textContent, /校验.*3%|3%.*校验/);
  assert.equal(element(elements, "cancelHistoryImport").classList.contains("hidden"), false);

  worker.onmessage({ data: {
    ok: true,
    rows: [
      { x: 0, y: 1, status: "ok", mode: "DCV", unit: "V", raw: "1", id: 1, break_before: false },
      { x: 1, y: 3, status: "ok", mode: "DCV", unit: "V", raw: "3", id: 2, break_before: true },
    ],
    modeValue: "DCV", unit: "V", experimentId: "run-1", rowCount: 100000,
    stats: { rowCount: 100000, validCount: 90_000, invalidCount: 10_000, min: -5, max: 15, mean: 2.5, sampleStdDev: 1.25 },
  } });
  assert.equal(await pending, true);
  api.updateStats();

  assert.deepEqual({ ...api.app.importedStats }, {
    rowCount: 100000, validCount: 90000, invalidCount: 10000, min: -5, max: 15, mean: 2.5, sampleStdDev: 1.25,
  });
  assert.match(element(elements, "analysisContext").textContent, /n=90000/);
  assert.equal(element(elements, "historyImportProgress").classList.contains("hidden"), true);
  assert.equal(element(elements, "cancelHistoryImport").classList.contains("hidden"), true);
});

test("cancel action sends the worker job id and waits for its cancellation result", async () => {
  const { api, elements, workers } = createFrontend();
  api.app.modes.set("DCV", { value: "DCV", label: "直流电压", unit: "V" });
  const pending = api.loadHistory({ name: "long-run.csv", size: 1024 ** 3 });
  const worker = workers[0];
  const request = worker.messages[0];

  api.cancelHistoryImport();

  assert.equal(worker.messages.at(-1).type, "cancel");
  assert.equal(worker.messages.at(-1).jobId, request.jobId);
  assert.match(element(elements, "historyImportProgress").textContent, /正在取消/);
  worker.onmessage({ data: { ok: false, cancelled: true, error: "导入已取消" } });

  assert.equal(await pending, false);
  assert.equal(worker.terminated, true);
  assert.equal(api.app.imported, null);
});

test("history import shows a warning when the worker ignores an incomplete CSV tail", async () => {
  const { api, elements, workers } = createFrontend();
  api.app.modes.set("DCV", { value: "DCV", label: "直流电压", unit: "V" });
  const pending = api.loadHistory({ name: "interrupted.csv", size: 1024 });
  const worker = workers[0];

  worker.onmessage({ data: {
    ok: true,
    rows: [{ x: 0, y: 1, status: "ok", mode: "DCV", unit: "V", raw: "1", id: 1, break_before: false }],
    modeValue: "DCV", unit: "V", experimentId: "run-1", rowCount: 1,
    warnings: ["测量 CSV 尾部有未完成记录，已忽略该尾部；原文件未修改。"],
  } });

  assert.equal(await pending, true);
  assert.match(element(elements, "toast").textContent, /未完成记录/);
  assert.equal(element(elements, "toast").classList.contains("warning"), true);
});

test("event editor and safe-export controls keep the CSV formula warning visible", () => {
  const html = fs.readFileSync(path.join(ROOT, "src/fluke8845a/web/index.html"), "utf8");
  const eventStart = html.indexOf('<div class="event-entry">');
  const eventEnd = html.indexOf('<div id="experimentPathDetail"', eventStart);
  assert.ok(eventStart >= 0 && eventEnd > eventStart, "event warning is placed next to event entry");
  const eventEditor = html.slice(eventStart, eventEnd);
  assert.match(eventEditor, /若以\s*=、\+、-\s*或\s*@\s*开头/);
  assert.match(eventEditor, /公式/);
  assert.match(eventEditor, /安全副本/);

  const exportStart = html.indexOf('id="downloadEventsSafe"');
  const exportEnd = html.indexOf('<div id="chartContext"', exportStart);
  const exportArea = html.slice(exportStart, exportEnd);
  assert.match(exportArea, /id="downloadEventsSafe"[^>]*>导出事件安全副本/);
  assert.match(exportArea, /公式/);
  assert.match(exportArea, /原始 events\.csv.*(?:不变|不会更改)/);

  const shareButton = html.match(/<button id="downloadSharePackage"[^>]*>(.*?)<\/button>/);
  assert.ok(shareButton, "the redacted sharing ZIP stays visible beside safe CSV exports");
  assert.equal(shareButton[1], "导出脱敏分享包");
  assert.match(html, /脱敏分享包会对包内 CSV 文本做公式安全处理，并隐藏设备身份和本机路径/);
});
