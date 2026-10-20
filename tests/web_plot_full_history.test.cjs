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
    this.checked = false;
    this.value = "";
    this.textContent = "";
    this.innerHTML = "";
    this.classList = { add() {}, remove() {}, toggle() {}, contains() { return false; } };
  }
  addEventListener() {}
  getBoundingClientRect() { return { left: 0, top: 0, width: 1200, height: 480 }; }
}

function createFrontend(fetchImpl) {
  const elements = new Map();
  const document = {
    getElementById(id) {
      if (!elements.has(id)) elements.set(id, new FakeElement(id));
      return elements.get(id);
    },
    querySelector() { return new FakeElement("query"); },
  };
  const ids = { value: 0 };
  const context = vm.createContext({
    document,
    window: { devicePixelRatio: 1, addEventListener() {} },
    fetch: fetchImpl,
    URLSearchParams,
    setTimeout,
    clearTimeout,
    requestAnimationFrame: (callback) => setTimeout(callback, 0),
    crypto: { randomUUID: () => `request-${++ids.value}` },
    console,
  });
  const source = fs.readFileSync(path.join(ROOT, "src/fluke8845a/web/app.js"), "utf8")
    .replace(/\ninitialize\(\);\s*$/, "");
  vm.runInContext(`${source}\n;globalThis.__plot = { app, ui, appendSampleRows, refreshChartOverview, chartPointsForPlot, requestChartRange, drawChart };`, context);
  vm.runInContext("updateAccessiblePointTable = () => {}; updateChartAccessibilitySummary = () => {};", context);
  return { api: context.__plot, elements };
}

function attachCanvas(api) {
  const noop = () => {};
  const context = new Proxy({
    measureText: (value) => ({ width: String(value).length * 6 }),
  }, {
    get(target, key) { return key in target ? target[key] : noop; },
    set(target, key, value) { target[key] = value; return true; },
  });
  api.ui.canvas = {
    width: 0,
    height: 0,
    getBoundingClientRect: () => ({ left: 0, top: 0, width: 1200, height: 480 }),
    getContext: () => context,
  };
  api.ui.overlay = null;
}

function response(body, status = 200) {
  return { ok: status >= 200 && status < 300, status, json: async () => body };
}

test("live plot keeps a bounded full-run overview after the recent sample cache rolls", async () => {
  const overviewRows = [
    { id: 1, x: 0, y: 1, status: "ok", unit: "V", mode: "DCV" },
    { id: 500, x: 499, y: 8, status: "ok", unit: "V", mode: "DCV" },
    { id: 5002, x: 5001, y: 1.5, status: "ok", unit: "V", mode: "DCV", breakBefore: true },
    { id: 9000, x: 8999, y: 2, status: "ok", unit: "V", mode: "DCV" },
  ];
  const { api, elements } = createFrontend(async (url) => {
    assert.match(url, /^\/api\/chart\/overview\?/);
    return response({ job_id: "chart-job-1", status: "ready", revision: 4, rows: overviewRows });
  });
  elements.set("autoFollow", new FakeElement("autoFollow"));
  elements.get("autoFollow").checked = false;
  api.app.state = { experiment_dir: "D:/runs/current", last_experiment_dir: "D:/runs/current" };
  api.appendSampleRows(Array.from({ length: 6000 }, (_, index) => ({
    sample_index: index + 1,
    elapsed_s: index,
    value_si: index,
    status: "ok",
    mode: "DCV",
    unit_si: "V",
  })));

  await api.refreshChartOverview();
  const plotted = api.chartPointsForPlot();
  attachCanvas(api);
  api.drawChart();

  assert.equal(api.app.points.length, 5000, "recent exact-detail buffer stays bounded");
  assert.equal(plotted[0].x, 0, "overview includes the first experiment point");
  assert.equal(plotted.at(-1).x, 8999, "overview includes the latest experiment extent");
  assert.ok(plotted.some((point) => point.y === 8), "overview preserves a peak lost from the recent cache");
  assert.ok(plotted.some((point) => point.breakBefore), "overview carries a data-gap marker");
  assert.equal(api.app.chartScale.dataMinX, 0, "the actual plot includes the first experiment point");
  assert.equal(api.app.chartScale.dataMaxX, 8999, "the actual plot includes the latest experiment point");
});

test("zooming an older interval asynchronously replaces overview points with range detail", async () => {
  const calls = [];
  const detailRows = [
    { id: 21, x: 20, y: 1.1, status: "ok", unit: "V", mode: "DCV" },
    { id: 22, x: 21, y: 1.2, status: "ok", unit: "V", mode: "DCV" },
  ];
  const { api } = createFrontend(async (url) => {
    calls.push(url);
    if (url.startsWith("/api/chart/range?") && !url.includes("start=")) {
      return response({ request_id: "request-1", status: "ready", job_id: "chart-job-1", start_s: 20, end_s: 22, rows: detailRows });
    }
    return response({ request_id: "request-1", status: "running", job_id: "chart-job-1" }, 202);
  });
  api.ui.canvas = null;
  api.app.chartOverviewJobId = "chart-job-1";
  api.app.chartOverviewStatus = "ready";
  api.app.xView = [20, 22];

  await api.requestChartRange(20, 22, 800);

  assert.ok(calls.some((url) => url.startsWith("/api/chart/range?") && url.includes("start=")), "starts an async range request");
  assert.ok(calls.some((url) => url.startsWith("/api/chart/range?") && !url.includes("start=")), "polls the range job");
  assert.deepEqual(Array.from(api.app.chartRangeRows, (point) => [point.id, point.x, point.y]), [
    [21, 20, 1.1], [22, 21, 1.2],
  ]);
  assert.deepEqual(Array.from(api.chartPointsForPlot(), (point) => point.id), [21, 22]);
});

test("drawing a panned older window requests and renders exact range detail", async () => {
  const calls = [];
  const detailRows = [
    { id: 21, x: 20, y: 1.1, status: "ok", unit: "V", mode: "DCV" },
    { id: 22, x: 21, y: 1.2, status: "ok", unit: "V", mode: "DCV" },
  ];
  const { api, elements } = createFrontend(async (url) => {
    calls.push(url);
    return response({ request_id: "request-1", status: "ready", job_id: "chart-job-1", rows: detailRows });
  });
  attachCanvas(api);
  elements.get("autoFollow").checked = false;
  api.app.state = { experiment_dir: "D:/runs/current", last_experiment_dir: "D:/runs/current", events: [] };
  api.app.chartOverviewJobId = "chart-job-1";
  api.app.chartOverview = [
    { id: 1, x: 0, y: 1, status: "ok", unit: "V", mode: "DCV", summary: true },
    { id: 9000, x: 8999, y: 2, status: "ok", unit: "V", mode: "DCV", summary: true },
  ];
  api.app.xView = [20, 22];
  api.appendSampleRows(Array.from({ length: 6000 }, (_, index) => ({
    sample_index: index + 10001,
    elapsed_s: index + 10000,
    value_si: 1.5,
    status: "ok",
    mode: "DCV",
    unit_si: "V",
  })));

  api.drawChart();
  await new Promise((resolve) => setTimeout(resolve, 0));

  assert.ok(calls.some((url) => url.startsWith("/api/chart/range?") && url.includes("start=20")), "drawing an old interval starts a detail request");
  assert.deepEqual(Array.from(api.app.chartScale.rows, (point) => point.id), [21, 22], "the actual plot switches to the returned detail rows");
  assert.equal(api.app.chartScale.dataMinX, 0, "detail rows retain the full-run pan start bound");
  assert.equal(api.app.chartScale.dataMaxX, 15999, "detail rows retain the full-run pan end bound");
});
