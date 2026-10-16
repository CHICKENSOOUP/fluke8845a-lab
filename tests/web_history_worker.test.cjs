"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const ROOT = path.resolve(__dirname, "..");
const WORKER_SOURCE = path.join(ROOT, "src/fluke8845a/web/history_worker.js");

async function runWorker(file, modes = [{ value: "DCV", unit: "V" }], kind = "measurements") {
  let handler;
  const messages = [];
  const context = vm.createContext({
    self: {
      addEventListener(type, callback) { if (type === "message") handler = callback; },
      postMessage(message) { messages.push(message); },
    },
    Number, Set, Error,
  });
  vm.runInContext(fs.readFileSync(WORKER_SOURCE, "utf8"), context);
  await handler({ data: { file, modes, kind, experimentId: "run-7" } });
  return messages[0];
}

test("history worker preserves CSV columns, sample ids, quoted fields, BOM, and invalid-read gaps", async () => {
  const result = await runWorker({ size: 256, text: async () =>
    '\uFEFFelapsed_s,value_si,unit_si,mode,status,sample_index,raw_reading,experiment_id\r\n0,1.25,V,DCV,ok,8,"1,25 V",run-7\r\n1,,V,DCV,timeout,9,timeout,run-7\r\n'
  });

  assert.equal(result.ok, true);
  assert.deepEqual(Array.from(result.rows, ({ x, y, status, mode, unit, raw, id }) => ({ x, y: Number.isNaN(y) ? "NaN" : y, status, mode, unit, raw, id })), [
    { x: 0, y: 1.25, status: "ok", mode: "DCV", unit: "V", raw: "1,25 V", id: 8 },
    { x: 1, y: "NaN", status: "timeout", mode: "DCV", unit: "V", raw: "timeout", id: 9 },
  ]);
  assert.equal(result.experimentId, "run-7");
});

test("history worker returns the existing mixed-mode and invalid-reading diagnostics", async () => {
  const mixed = await runWorker({ size: 80, text: async () => "elapsed_s,value_si,unit_si,mode,status\n0,1,V,DCV,ok\n1,2,A,DCI,ok\n" }, [
    { value: "DCV", unit: "V" }, { value: "DCI", unit: "A" },
  ]);
  assert.match(mixed.error, /CSV 混合了不同测量模式/);

  const invalid = await runWorker({ size: 80, text: async () => "elapsed_s,value_si,unit_si,mode,status\n0,,V,DCV,timeout\n" });
  assert.match(invalid.error, /CSV 没有有效读数/);
});

test("history worker enforces the 50 MB limit before reading file contents", async () => {
  let read = false;
  const result = await runWorker({ size: 50 * 1024 * 1024 + 1, text: async () => { read = true; return ""; } });
  assert.equal(read, false);
  assert.match(result.error, /超过 50 MB/);
});

test("history worker parses matching event CSV away from the page thread", async () => {
  const result = await runWorker({ size: 128, text: async () =>
    '\uFEFFexperiment_id,elapsed_s,event,timestamp_local,sample_index,value_si,unit_si\r\nrun-7,4,"load, enabled",2026-10-16T12:00:00+08:00,9,1.25,V\r\n'
  }, undefined, "events");

  assert.equal(result.ok, true);
  assert.deepEqual(Array.from(result.events, ({ experiment_id, elapsed_s, text, timestamp_local, sample_index, value_si, unit_si }) =>
    ({ experiment_id, elapsed_s, text, timestamp_local, sample_index, value_si, unit_si })), [
    { experiment_id: "run-7", elapsed_s: 4, text: "load, enabled", timestamp_local: "2026-10-16T12:00:00+08:00", sample_index: 9, value_si: 1.25, unit_si: "V" },
  ]);

  const mismatch = await runWorker({ size: 64, text: async () => "experiment_id,elapsed_s,event\nrun-8,1,wrong run\n" }, undefined, "events");
  assert.match(mismatch.error, /实验标识不一致/);

  const emptyId = await runWorker({ size: 64, text: async () => "experiment_id,elapsed_s,event\n,1,unlinked event\n" }, undefined, "events");
  assert.match(emptyId.error, /实验标识不一致/);

  const mixedId = await runWorker({ size: 80, text: async () => "experiment_id,elapsed_s,event\nrun-7,1,linked event\n,2,unlinked event\n" }, undefined, "events");
  assert.match(mixedId.error, /实验标识不一致/);
});
