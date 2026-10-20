"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const ROOT = path.resolve(__dirname, "..");
const WORKER_SOURCE = path.join(ROOT, "src/fluke8845a/web/history_worker.js");

async function runWorker(file, modes = [{ value: "DCV", unit: "V" }], kind = "measurements") {
  if (typeof file.stream !== "function" && typeof file.text === "function" && file.size <= 1024 * 1024) {
    const bytes = new TextEncoder().encode(await file.text());
    file = {
      ...file,
      size: bytes.byteLength,
      stream() {
        let sent = false;
        return { getReader: () => ({
          async read() { if (sent) return { done: true }; sent = true; return { done: false, value: bytes }; },
          async cancel() {}, releaseLock() {},
        }) };
      },
    };
  }
  let handler;
  const messages = [];
  const context = vm.createContext({
    self: {
      addEventListener(type, callback) { if (type === "message") handler = callback; },
      postMessage(message) { messages.push(message); },
    },
    Number, Set, Map, Error, Math, Object, String, Promise, TextDecoder, TextEncoder, Uint8Array,
    ArrayBuffer, setTimeout, clearTimeout,
  });
  vm.runInContext(fs.readFileSync(WORKER_SOURCE, "utf8"), context);
  await handler({ data: { file, modes, kind, experimentId: "run-7" } });
  return [...messages].reverse().find((message) => typeof message?.ok === "boolean");
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

test("history worker ignores and reports unterminated measurement and event tails", async () => {
  const measurements = await runWorker({ size: 256, text: async () =>
    "experiment_id,sample_index,elapsed_s,mode,unit_si,value_si,status\r\n"
    + "run-7,1,0,DCV,V,1,ok\r\n"
    + "run-7,2,1,DCV,V,2,ok\r\n"
    + "run-7,3,2,DCV,V,999,\"unfinished"
  });

  assert.equal(measurements.ok, true, measurements.error);
  assert.equal(measurements.rowCount, 2);
  assert.equal(measurements.stats.validCount, 2);
  assert.equal(measurements.rows.length, 2);
  assert.ok(measurements.warnings.some((warning) => warning.includes("未完成记录")));

  const events = await runWorker({ size: 128, text: async () =>
    "experiment_id,elapsed_s,event\r\n"
    + "run-7,0.5,valid event\r\n"
    + "run-7,0.75,\"unfinished event"
  }, undefined, "events");

  assert.equal(events.ok, true, events.error);
  assert.equal(events.events.length, 1);
  assert.ok(events.warnings.some((warning) => warning.includes("未完成记录")));
});

test("history worker keeps syntactically complete EOF measurement and event records without newline", async () => {
  const measurements = await runWorker({ size: 1, text: async () =>
    "experiment_id,sample_index,elapsed_s,mode,unit_si,value_si,status,raw_reading\r\n"
    + "run-7,1,0,DCV,V,1,ok,\"1 V\"\r\n"
    + "run-7,2,1,DCV,V,2,ok,\"2,000 V\""
  });

  assert.equal(measurements.ok, true, measurements.error);
  assert.equal(measurements.rowCount, 2);
  assert.equal(measurements.rows.length, 2);
  assert.equal(measurements.rows[1].raw, "2,000 V");
  assert.equal(measurements.warnings.length, 1);
  assert.match(measurements.warnings[0], /无法判断/);

  const events = await runWorker({ size: 1, text: async () =>
    "experiment_id,elapsed_s,event\r\nrun-7,0.5,\"load enabled, final state\""
  }, undefined, "events");

  assert.equal(events.ok, true, events.error);
  assert.deepEqual(Array.from(events.events, ({ elapsed_s, text }) => ({ elapsed_s, text })), [
    { elapsed_s: 0.5, text: "load enabled, final state" },
  ]);
  assert.equal(events.warnings.length, 1);
  assert.match(events.warnings[0], /无法判断/);
});

test("history worker accepts a complete quoted EOF row when its closing quote arrives in the last chunk", async () => {
  const bytes = new TextEncoder().encode(
    "experiment_id,sample_index,elapsed_s,mode,unit_si,value_si,status,raw_reading\r\n"
    + "run-7,1,0,DCV,V,1,ok,\"1,000 V\""
  );
  const chunks = [bytes.slice(0, -1), bytes.slice(-1)];
  let readCalls = 0;
  const result = await runWorker({
    size: bytes.byteLength,
    stream() {
      let next = 0;
      return { getReader: () => ({
        async read() {
          readCalls++;
          if (next >= chunks.length) return { done: true };
          return { done: false, value: chunks[next++] };
        },
        async cancel() {},
        releaseLock() {},
      }) };
    },
  });

  assert.equal(result.ok, true, `${result.error}; reader calls: ${readCalls}`);
  assert.equal(readCalls, 6);
  assert.equal(result.rows.length, 1);
  assert.equal(result.rows[0].raw, "1,000 V");
  assert.equal(result.warnings.length, 1);
  assert.match(result.warnings[0], /CR\/LF/);
});

test("history worker rejects a truncated UTF-8 code point instead of replacing imported text", async () => {
  const prefix = new TextEncoder().encode(
    "experiment_id,elapsed_s,event\r\nrun-7,0.5,valid event"
  );
  const bytes = new Uint8Array(prefix.length + 1);
  bytes.set(prefix);
  bytes[bytes.length - 1] = 0xE4;
  let sent = false;
  const result = await runWorker({
    size: bytes.byteLength,
    stream() {
      return { getReader: () => ({
        async read() {
          if (sent) return { done: true };
          sent = true;
          return { done: false, value: bytes };
        },
        async cancel() {},
        releaseLock() {},
      }) };
    },
  }, undefined, "events");

  assert.equal(result.ok, false);
  assert.match(result.error, /UTF-8/);
});

test("history worker returns the existing mixed-mode and invalid-reading diagnostics", async () => {
  const mixed = await runWorker({ size: 80, text: async () => "elapsed_s,value_si,unit_si,mode,status\n0,1,V,DCV,ok\n1,2,A,DCI,ok\n" }, [
    { value: "DCV", unit: "V" }, { value: "DCI", unit: "A" },
  ]);
  assert.match(mixed.error, /CSV 混合了不同测量模式/);

  const invalid = await runWorker({ size: 80, text: async () => "elapsed_s,value_si,unit_si,mode,status\n0,,V,DCV,timeout\n" });
  assert.match(invalid.error, /CSV 没有有效读数/);
});

test("history worker rejects duplicate measurement columns instead of choosing one value field", async () => {
  const result = await runWorker({
    size: 80,
    text: async () => "elapsed_s,value_si,value_si,status\n0,1.25,9.75,ok\n",
  });

  assert.equal(result.ok, false);
  assert.match(result.error, /重复列.*value_si/);
});

test("history worker validates only finite successful reads but preserves each row's mode and unit", async () => {
  const result = await runWorker({ size: 120, text: async () =>
    "elapsed_s,value_si,unit_si,mode,status\n0,,A,DCI,timeout\nnot-a-time,3,mV,ACV,ok\n2,4,   ,   ,timeout\n,5,A,DCI,ok\n3, ,A,DCI,ok\n1,2,V,DCV,ok\n"
  }, [
    { value: "DCV", unit: "V" }, { value: "DCI", unit: "A" }, { value: "ACV", unit: "V" },
  ]);

  assert.equal(result.ok, true, result.error);
  assert.equal(result.modeValue, "DCV");
  assert.equal(result.unit, "V");
  assert.equal(result.stats.validCount, 1);
  assert.equal(result.rows.length, 6);
  assert.equal(result.rows[0].status, "timeout");
  assert.equal(result.rows[0].mode, "DCI");
  assert.equal(result.rows[0].unit, "A");
  assert.equal(Number.isNaN(result.rows[1].x), true);
  assert.equal(result.rows[1].mode, "ACV");
  assert.equal(result.rows[1].unit, "mV");
  assert.equal(result.rows[2].mode, "");
  assert.equal(result.rows[2].unit, "");
  assert.equal(Number.isNaN(result.rows[3].x), true);
  assert.equal(result.rows[3].mode, "DCI");
  assert.equal(Number.isNaN(result.rows[4].y), true);
  assert.equal(result.rows[4].mode, "DCI");
  assert.equal(result.rows[4].unit, "A");
  assert.equal(result.rows[5].mode, "DCV");
  assert.equal(result.rows[5].unit, "V");
});

test("history worker rejects valid readings that mix units", async () => {
  const result = await runWorker({ size: 100, text: async () =>
    "elapsed_s,value_si,unit_si,mode,status\n0,1,V,DCV,ok\n1,2,mV,DCV,ok\n"
  });

  assert.equal(result.ok, false);
  assert.match(result.error, /混合了不同单位/);
});

test("history worker enforces the 2 GiB cap before opening a file stream", async () => {
  let opened = false;
  const result = await runWorker({
    size: 2 * 1024 * 1024 * 1024 + 1,
    stream() { opened = true; throw new Error("oversized file must not be opened"); },
  });
  assert.equal(opened, false);
  assert.match(result.error, /超过 2 GiB/);
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
