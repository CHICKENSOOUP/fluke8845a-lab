"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const ROOT = path.resolve(__dirname, "..");
const WORKER_SOURCE = path.join(ROOT, "src/fluke8845a/web/history_worker.js");
const DEFAULT_MODES = [{ value: "DCV", unit: "V" }];

function chunkedFile(chunks, size = chunks.reduce((sum, chunk) => sum + chunk.byteLength, 0)) {
  let textCalls = 0;
  return {
    name: "history.csv",
    size,
    get textCalls() { return textCalls; },
    async text() { textCalls++; throw new Error("large-file path must not call file.text()"); },
    stream() {
      let index = 0;
      return {
        getReader() {
          return {
            async read() {
              if (index >= chunks.length) return { done: true };
              return { done: false, value: chunks[index++] };
            },
            async cancel() {},
            releaseLock() {},
          };
        },
      };
    },
  };
}

function utf8Chunks(text, sizes = [1]) {
  const bytes = new TextEncoder().encode(text);
  const chunks = [];
  let offset = 0, sizeIndex = 0;
  while (offset < bytes.length) {
    const size = sizes[sizeIndex++ % sizes.length];
    chunks.push(bytes.slice(offset, Math.min(bytes.length, offset + size)));
    offset += size;
  }
  return chunks;
}

function slicedFile(text) {
  const bytes = new TextEncoder().encode(text);
  let textCalls = 0, sliceCalls = 0;
  return {
    name: "sliced-history.csv",
    size: bytes.byteLength,
    get textCalls() { return textCalls; },
    get sliceCalls() { return sliceCalls; },
    async text() { textCalls++; throw new Error("slice path must not call file.text()"); },
    slice(start, end) {
      sliceCalls++;
      return { arrayBuffer: async () => bytes.slice(start, end).buffer };
    },
  };
}

function makeVirtualGiBFile(firstChunk) {
  const size = 1024 ** 3;
  let cancelCalls = 0, textCalls = 0, streams = 0;
  return {
    name: "virtual-1-gib.csv",
    size,
    get cancelCalls() { return cancelCalls; },
    get textCalls() { return textCalls; },
    get streams() { return streams; },
    async text() { textCalls++; throw new Error("1 GiB import must not call file.text()"); },
    stream() {
      streams++;
      let sent = 0, first = true;
      return {
        getReader() {
          return {
            async read() {
              if (sent >= size) return { done: true };
              const value = first ? firstChunk : new Uint8Array(0);
              first = false;
              sent += value.byteLength;
              return { done: false, value };
            },
            async cancel() { cancelCalls++; sent = size; },
            releaseLock() {},
          };
        },
      };
    },
  };
}

function createWorkerHarness(onMessage = () => {}) {
  let handler;
  const messages = [];
  const context = vm.createContext({
    self: {
      addEventListener(type, callback) { if (type === "message") handler = callback; },
      postMessage(message) { messages.push(message); onMessage(message, send); },
    },
    Number, Set, Map, Error, Math, Object, String, Promise, TextDecoder, Uint8Array,
    ArrayBuffer, setTimeout, clearTimeout,
  });
  vm.runInContext(fs.readFileSync(WORKER_SOURCE, "utf8"), context);
  function send(data) { return handler({ data }); }
  return { messages, send };
}

function resultMessage(messages) {
  return [...messages].reverse().find((message) => typeof message?.ok === "boolean");
}

test("stream parser preserves quoted commas, doubled quotes, CRLF and UTF-8 across chunk boundaries", async () => {
  const csv = '\uFEFFelapsed_s,value_si,unit_si,mode,status,sample_index,raw_reading,experiment_id\r\n'
    + '0,1.25,V,DCV,ok,8,"含中文, ""quoted""\r\ncontinued",run-7\r\n'
    + '1,,V,DCV,timeout,9,timeout,run-7\r\n';
  const file = chunkedFile(utf8Chunks(csv, [1, 2, 3, 5, 8]));
  const harness = createWorkerHarness();

  await harness.send({ file, modes: DEFAULT_MODES, jobId: "quoted" });
  const result = resultMessage(harness.messages);

  assert.equal(result.ok, true);
  assert.equal(result.experimentId, "run-7");
  assert.deepEqual(Array.from(result.rows, ({ x, y, status, raw, id }) => ({
    x, y: Number.isNaN(y) ? "NaN" : y, status, raw, id,
  })), [
    { x: 0, y: 1.25, status: "ok", raw: '含中文, "quoted"\r\ncontinued', id: 8 },
    { x: 1, y: "NaN", status: "timeout", raw: "timeout", id: 9 },
  ]);
  assert.equal(result.stats.validCount, 1);
  assert.equal(file.textCalls, 0);
});

test("1 GiB streaming imports are accepted without text(), report progress, and can be cancelled", async () => {
  const header = "elapsed_s,value_si,unit_si,mode,status,sample_index,raw_reading,experiment_id\r\n";
  const row = "0,1,V,DCV,ok,1,1 V,run-7\r\n";
  const body = new TextEncoder().encode(header + row.repeat(12000));
  const file = makeVirtualGiBFile(body);
  const harness = createWorkerHarness((message, send) => {
    if (message?.type === "progress" && message.phase === "validate" && message.bytesRead > 0) {
      void send({ type: "cancel", jobId: "gib" });
    }
  });

  await harness.send({ file, modes: DEFAULT_MODES, jobId: "gib", reportProgress: true, pixelWidth: 640 });
  const result = resultMessage(harness.messages);
  const progress = harness.messages.filter((message) => message?.type === "progress");

  assert.equal(result.ok, false);
  assert.equal(result.cancelled, true);
  assert.equal(file.textCalls, 0);
  assert.equal(file.streams, 1);
  assert.equal(file.cancelCalls, 1);
  assert.ok(progress.length > 0);
  assert.ok(progress.every((message) => message.totalBytes === 1024 ** 3));
});

test("Blob-like history files are read in bounded slices and produce a full-run summary", async () => {
  const header = "elapsed_s,value_si,unit_si,mode,status,sample_index,raw_reading,experiment_id\r\n";
  const data = Array.from({ length: 9000 }, (_, index) =>
    `${index},${index},V,DCV,ok,${index + 1},reading,run-7`).join("\r\n") + "\r\n";
  const file = slicedFile(header + data);
  const harness = createWorkerHarness();

  await harness.send({ file, modes: DEFAULT_MODES, jobId: "slices", reportProgress: true, pixelWidth: 50 });
  const result = resultMessage(harness.messages);

  assert.equal(result.ok, true);
  assert.equal(result.rowCount, 9000);
  assert.equal(result.stats.validCount, 9000);
  assert.equal(result.stats.min, 0);
  assert.equal(result.stats.max, 8999);
  assert.ok(result.rows.length <= 100);
  assert.equal(file.textCalls, 0);
  assert.ok(file.sliceCalls >= 5);
});

test("measurement history keeps streamed full-run statistics and bounds plot rows by pixel budget", async () => {
  const rows = ["elapsed_s,value_si,unit_si,mode,status,sample_index,raw_reading,experiment_id"];
  for (let index = 0; index < 500; index++) {
    const value = index === 251 ? 9999 : index === 31 ? -50 : index;
    const status = index === 320 ? "timeout" : "ok";
    rows.push(`${index / 10},${status === "ok" ? value : ""},V,DCV,${status},${index + 1},${value} V,run-7`);
  }
  const file = chunkedFile(utf8Chunks(`${rows.join("\r\n")}\r\n`, [37, 101, 251]));
  const harness = createWorkerHarness();

  await harness.send({ file, modes: DEFAULT_MODES, jobId: "decimate", reportProgress: true, pixelWidth: 12, maxPoints: 24 });
  const result = resultMessage(harness.messages);
  const progress = harness.messages.filter((message) => message?.type === "progress");

  assert.equal(result.ok, true);
  assert.equal(result.rowCount, 500);
  assert.equal(result.stats.validCount, 499);
  assert.equal(result.stats.min, -50);
  assert.equal(result.stats.max, 9999);
  assert.ok(result.rows.length <= 24);
  assert.ok(result.rows.some((point) => point.y === -50));
  assert.ok(result.rows.some((point) => point.y === 9999));
  assert.ok(result.rows.some((point) => point.status === "timeout"));
  assert.ok(result.rows.every((point) => typeof point.break_before === "boolean"));
  assert.deepEqual([...new Set(progress.map((message) => message.phase))].sort(), ["sample", "validate"]);
  assert.equal(progress.at(-1).progress, 1);
});

test("event CSV stays linked to the measurement experiment when fields cross chunks", async () => {
  const csv = "experiment_id,elapsed_s,event,timestamp_local,sample_index,value_si,unit_si\r\n"
    + 'run-7,4,"load, ""enabled""",2026-10-16T12:00:00+08:00,9,1.25,V\r\n';
  const file = chunkedFile(utf8Chunks(csv, [2, 7, 1, 11]));
  const harness = createWorkerHarness();

  await harness.send({ file, kind: "events", experimentId: "run-7", jobId: "events" });
  const result = resultMessage(harness.messages);

  assert.equal(result.ok, true);
  assert.deepEqual(Array.from(result.events, (event) => ({
    experiment_id: event.experiment_id, elapsed_s: event.elapsed_s, text: event.text,
    sample_index: event.sample_index, value_si: event.value_si, unit_si: event.unit_si,
  })), [{ experiment_id: "run-7", elapsed_s: 4, text: 'load, "enabled"', sample_index: 9, value_si: 1.25, unit_si: "V" }]);
});
