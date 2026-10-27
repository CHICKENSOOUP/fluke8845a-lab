"use strict";

const MAX_HISTORY_BYTES = 2 * 1024 * 1024 * 1024;
const MAX_EVENTS_BYTES = 10 * 1024 * 1024;
const MAX_SMALL_TEXT_FALLBACK_BYTES = 1024 * 1024;
const MAX_CSV_RECORD_CHARS = 4 * 1024;
const PROGRESS_CHUNK_BYTES = 256 * 1024;
const DEFAULT_PLOT_POINTS = 3000;
const MAX_PLOT_POINTS = 6000;

const activeJobs = new Map();
let jobSequence = 0;

function createCsvParser(onRow) {
  let row = [], cell = "", inQuotes = false, quotePending = false;
  let skipLf = false, firstCharacter = true, recordChars = 0;

  function countCharacter() {
    recordChars++;
    if (recordChars > MAX_CSV_RECORD_CHARS) {
      throw new Error("CSV 单行超过 4 KiB；请检查是否存在未闭合引号或异常字段");
    }
  }

  function append(value) {
    cell += value;
  }

  function finishField() {
    row.push(cell);
    cell = "";
  }

  function finishRow() {
    finishField();
    if (row.some((value) => value !== "")) onRow(row);
    row = [];
    recordChars = 0;
  }

  function push(text) {
    for (let index = 0; index < text.length; index++) {
      const character = text[index];
      if (firstCharacter) {
        firstCharacter = false;
        if (character === "\uFEFF") continue;
      }
      if (skipLf) {
        skipLf = false;
        if (character === "\n") continue;
      }

      countCharacter();
      if (inQuotes) {
        if (quotePending) {
          quotePending = false;
          if (character === '"') {
            append('"');
            continue;
          }
          inQuotes = false;
        } else if (character === '"') {
          quotePending = true;
          continue;
        } else {
          append(character);
          continue;
        }
      }

      if (character === '"') inQuotes = true;
      else if (character === ",") finishField();
      else if (character === "\r") { finishRow(); skipLf = true; }
      else if (character === "\n") finishRow();
      else append(character);
    }
  }

  function finish() {
    const hasEofRecord = recordChars > 0 || cell !== "" || row.length > 0 || quotePending;
    const incompleteTail = inQuotes && !quotePending;
    const missingFinalTerminator = !incompleteTail && hasEofRecord;
    if (missingFinalTerminator) {
      // CSV permits a final complete record without CR/LF; a quote pending at EOF closes a quoted field.
      inQuotes = false;
      quotePending = false;
      finishRow();
    } else {
      row = [];
      cell = "";
      inQuotes = false;
      quotePending = false;
    }
    return { incompleteTail, missingFinalTerminator };
  }

  return { push, finish };
}

function cancelledError() {
  const error = new Error("导入已取消");
  error.cancelled = true;
  return error;
}

function throwIfCancelled(job) {
  if (job.cancelled) throw cancelledError();
}

function progressFraction(phase, bytesRead, totalBytes, done) {
  const ratio = totalBytes > 0 ? Math.min(1, bytesRead / totalBytes) : (done ? 1 : 0);
  if (phase === "validate") return ratio * 0.5;
  if (phase === "sample") return 0.5 + ratio * 0.5;
  return ratio;
}

function postProgress(job, phase, bytesRead, totalBytes, rowsRead, done = false, force = false) {
  if (!job.reportProgress) return;
  if (!force && !done && bytesRead - job.lastProgressBytes < PROGRESS_CHUNK_BYTES) return;
  job.lastProgressBytes = bytesRead;
  self.postMessage({
    type: "progress",
    jobId: job.id,
    phase,
    bytesRead,
    totalBytes,
    rowsRead,
    progress: progressFraction(phase, bytesRead, totalBytes, done),
  });
}

function decodeCsvUtf8(decoder, chunk, options) {
  try {
    return decoder.decode(chunk, options);
  } catch {
    throw new Error("CSV 不是有效的 UTF-8 编码，包含不完整或无效字节序列；源文件未修改。");
  }
}

async function yieldToMessageQueue() {
  if (typeof setTimeout === "function") await new Promise((resolve) => setTimeout(resolve, 0));
  else await Promise.resolve();
}

async function readCsvFile(file, onRow, job, phase) {
  let decoder = null;
  let bytesRead = 0, rowsRead = 0, completed = false;
  job.lastProgressBytes = 0;
  const parser = createCsvParser((row) => {
    throwIfCancelled(job);
    onRow(row);
    rowsRead++;
  });

  const consume = async (chunk, textByteLength = null) => {
    throwIfCancelled(job);
    let text;
    if (typeof chunk === "string") {
      bytesRead += textByteLength == null ? chunk.length : textByteLength;
      text = chunk;
    } else {
      if (!chunk || typeof chunk.byteLength !== "number") throw new Error("CSV 流返回了无效数据块");
      decoder ||= new TextDecoder("utf-8", { fatal: true });
      bytesRead += chunk.byteLength;
      text = decodeCsvUtf8(decoder, chunk, { stream: true });
    }
    if (bytesRead > file.size) throw new Error("CSV 流读取字节数超过文件声明大小");
    parser.push(text);
    postProgress(job, phase, bytesRead, file.size, rowsRead);
    await yieldToMessageQueue();
    throwIfCancelled(job);
  };

  let reader = null;
  try {
    // Blob/File.slice lets us enforce a small, predictable allocation per read.
    if (typeof file.slice === "function" && typeof file.slice(0, 1)?.arrayBuffer === "function") {
      for (let offset = 0; offset < file.size; offset += 256 * 1024) {
        throwIfCancelled(job);
        const buffer = await file.slice(offset, Math.min(file.size, offset + 256 * 1024)).arrayBuffer();
        await consume(new Uint8Array(buffer));
      }
    } else if (typeof file.stream === "function") {
      const stream = file.stream();
      if (!stream || typeof stream.getReader !== "function") throw new Error("浏览器未提供可分块读取的文件流");
      reader = stream.getReader();
      job.reader = reader;
      while (true) {
        throwIfCancelled(job);
        const result = await reader.read();
        if (result.done) break;
        await consume(result.value);
      }
    } else if (typeof file.text === "function" && file.size <= MAX_SMALL_TEXT_FALLBACK_BYTES) {
      // Compatibility for tiny legacy test doubles only; large browser Files must use stream()/slice().
      await consume(await file.text(), file.size);
    } else {
      throw new Error("无法分块读取此文件；请在支持 File.stream() 的浏览器中重新加载页面");
    }

    throwIfCancelled(job);
    if (bytesRead !== file.size) throw new Error(`CSV 文件流不完整：读取 ${bytesRead} 字节，文件标记为 ${file.size} 字节`);
    const tail = decoder ? decodeCsvUtf8(decoder) : "";
    if (tail) parser.push(tail);
    const { incompleteTail, missingFinalTerminator } = parser.finish();
    completed = true;
    postProgress(job, phase, bytesRead, file.size, rowsRead, true, true);
    return { rowsRead, bytesRead, incompleteTail, missingFinalTerminator };
  } finally {
    if (job.reader === reader) job.reader = null;
    if (reader && !completed && !job.cancelledReaders.has(reader)) {
      job.cancelledReaders.add(reader);
      try {
        const result = reader.cancel?.();
        if (result && typeof result.catch === "function") result.catch(() => {});
      } catch { /* The reader may have failed before cancellation. */ }
    }
    try { reader?.releaseLock?.(); } catch { /* A cancelled stream may already be unlocked. */ }
  }
}

function historyColumns(header) {
  const headers = header.map((value) => value.trim());
  const seen = new Set();
  const duplicates = new Set();
  for (const name of headers) {
    if (seen.has(name)) duplicates.add(name);
    seen.add(name);
  }
  if (duplicates.size) {
    throw new Error(`CSV 表头包含重复列：${[...duplicates].sort().join("、")}。请修正重复列后重试。`);
  }
  const column = (name) => headers.indexOf(name);
  const elapsed = column("elapsed_s"), value = column("value_si"), status = column("status");
  const missing = ["elapsed_s", "value_si", "status"].filter((name) => column(name) < 0);
  if (missing.length) {
    throw new Error(`CSV 缺少列：${missing.join("、")}。可接受格式至少包含 elapsed_s、value_si、status；请使用原始 measurements.csv，或补齐这些列后重试。`);
  }
  return {
    elapsed, value, status, mode: column("mode"), unit: column("unit_si"),
    raw: column("raw_reading"), sample: column("sample_index"), experiment: column("experiment_id"),
  };
}

function updateSingleValue(current, incoming, label) {
  if (!incoming) return current;
  if (current && current !== incoming) {
    if (label === "測量模式") throw new Error(`CSV 混合了不同测量模式（${current}、${incoming}），请按单一模式和单位拆分文件后重试`);
    if (label === "单位") throw new Error(`CSV 混合了不同单位（${current}、${incoming}），请按单一模式和单位拆分文件后重试`);
    throw new Error("CSV 包含多个实验标识，请按实验拆分文件");
  }
  return incoming;
}

function rowValues(row, columns, rowOrdinal, modeValue, unit) {
  const elapsedText = String(row[columns.elapsed] ?? "").trim();
  const readingText = String(row[columns.value] ?? "").trim();
  const x = elapsedText ? Number(elapsedText) : NaN;
  const y = readingText ? Number(readingText) : NaN;
  const idValue = columns.sample < 0 ? NaN : Number(row[columns.sample]);
  const rowMode = columns.mode < 0 ? modeValue : String(row[columns.mode] ?? "");
  const rowUnit = columns.unit < 0 ? unit : String(row[columns.unit] ?? "");
  return {
    x,
    y,
    status: row[columns.status] || "",
    mode: rowMode.trim() ? rowMode : "",
    unit: rowUnit.trim() ? rowUnit : "",
    raw: columns.raw < 0 ? "" : row[columns.raw] || "",
    id: Number.isFinite(idValue) && idValue !== 0 ? idValue : rowOrdinal + 1,
  };
}

function scanAccumulator() {
  return { count: 0, mean: 0, m2: 0, min: Infinity, max: -Infinity };
}

function addStatistic(accumulator, value) {
  accumulator.count++;
  accumulator.min = Math.min(accumulator.min, value);
  accumulator.max = Math.max(accumulator.max, value);
  const delta = value - accumulator.mean;
  accumulator.mean += delta / accumulator.count;
  accumulator.m2 += delta * (value - accumulator.mean);
}

function finalizeStats(stats, rowCount) {
  return {
    rowCount,
    validCount: stats.count,
    invalidCount: rowCount - stats.count,
    min: stats.count ? stats.min : null,
    max: stats.count ? stats.max : null,
    mean: stats.count ? stats.mean : null,
    sampleStdDev: stats.count > 1 ? Math.sqrt(stats.m2 / (stats.count - 1)) : (stats.count ? 0 : null),
  };
}

async function scanHistoryFile(file, modeOptions, job) {
  let columns = null, rowCount = 0, modeValue = "", unitValue = "", experimentId = "";
  let hasMissingExperimentId = false;
  const stats = scanAccumulator();
  const fileRead = await readCsvFile(file, (row) => {
    if (!columns) { columns = historyColumns(row); return; }
    rowCount++;
    if (columns.experiment >= 0) {
      const rowExperimentId = row[columns.experiment]?.trim() || "";
      if (!rowExperimentId) hasMissingExperimentId = true;
      experimentId = updateSingleValue(experimentId, rowExperimentId, "实验标识");
    }
    const values = rowValues(row, columns, rowCount - 1, "", "");
    if (values.status !== "ok" || !Number.isFinite(values.x) || !Number.isFinite(values.y)) return;
    if (columns.mode >= 0) modeValue = updateSingleValue(modeValue, row[columns.mode]?.trim() || "", "測量模式");
    if (columns.unit >= 0) unitValue = updateSingleValue(unitValue, row[columns.unit]?.trim() || "", "单位");
    addStatistic(stats, values.y);
  }, job, "validate");

  if (!columns || rowCount === 0) throw new Error("CSV 没有测量数据行");
  const mode = modeOptions.find((item) => item.value === modeValue);
  if (modeValue && !mode) {
    const supported = modeOptions.map((item) => item.value).join("、");
    throw new Error(`CSV 测量模式 ${modeValue} 不受支持；可识别模式：${supported || "尚未加载"}。`);
  }
  if (mode && unitValue && unitValue !== mode.unit) {
    throw new Error(`CSV 单位 ${unitValue} 与测量模式 ${modeValue} 的单位 ${mode.unit} 不匹配；请核对原始 CSV 表头和值。`);
  }
  if (!unitValue) unitValue = mode?.unit || "";
  if (!stats.count) {
    throw new Error("CSV 没有有效读数；检查 status、elapsed_s、value_si 列，确认至少有一行 status=ok 且数值有效");
  }
  return {
    columns, rowCount, modeValue, unit: unitValue,
    experimentId: hasMissingExperimentId ? null : experimentId || null,
    hasMissingExperimentId,
    stats: finalizeStats(stats, rowCount),
    incompleteTail: fileRead.incompleteTail,
    missingFinalTerminator: fileRead.missingFinalTerminator,
  };
}

function plotPointBudget(options) {
  let budget = DEFAULT_PLOT_POINTS;
  const width = Number(options.pixelWidth);
  if (Number.isFinite(width) && width > 0) budget = Math.floor(width * 2);
  const requested = Number(options.maxPoints);
  if (Number.isFinite(requested) && requested > 0) budget = Math.min(budget, Math.floor(requested));
  return Math.max(5, Math.min(MAX_PLOT_POINTS, budget));
}

function chooseBucketPoint(bucket, key, candidate, compare) {
  if (!bucket[key] || compare(candidate, bucket[key]) < 0) bucket[key] = candidate;
}

async function sampleHistoryFile(file, metadata, job, options) {
  const pointBudget = plotPointBudget(options);
  const bucketCount = Math.max(1, Math.floor(pointBudget / 5));
  const buckets = Array.from({ length: bucketCount }, () => ({}));
  let columns = null, rowCount = 0, previousId = null, previousValid = null, breakSerial = 0;
  await readCsvFile(file, (row) => {
    if (!columns) { columns = historyColumns(row); return; }
    const ordinal = rowCount++;
    const values = rowValues(row, metadata.columns, ordinal, metadata.modeValue, metadata.unit);
    const valid = values.status === "ok" && Number.isFinite(values.x) && Number.isFinite(values.y);
    if (previousId !== null && values.id > previousId + 1) breakSerial++;
    if (previousValid !== null && valid !== previousValid) breakSerial++;
    previousId = values.id;
    previousValid = valid;

    const point = { ...values };
    const candidate = { ordinal, point, breakSerial };
    const bucketIndex = Math.min(bucketCount - 1, Math.floor(ordinal * bucketCount / metadata.rowCount));
    const bucket = buckets[bucketIndex];
    if (Number.isFinite(values.x) && Number.isFinite(values.y)) {
      if (!bucket.first) bucket.first = candidate;
      bucket.last = candidate;
      if (valid) {
        chooseBucketPoint(bucket, "min", candidate, (left, right) => left.point.y - right.point.y);
        chooseBucketPoint(bucket, "max", candidate, (left, right) => right.point.y - left.point.y);
      } else if (!bucket.invalid) bucket.invalid = candidate;
    } else if (!bucket.invalid) {
      bucket.invalid = candidate;
    }
  }, job, "sample");

  if (rowCount !== metadata.rowCount) throw new Error("CSV 行数在分块导入期间发生变化，请重新选择文件");
  const candidates = [];
  for (const bucket of buckets) {
    const unique = new Map();
    for (const candidate of [bucket.first, bucket.min, bucket.max, bucket.invalid, bucket.last]) {
      if (candidate) unique.set(candidate.ordinal, candidate);
    }
    candidates.push(...[...unique.values()].sort((left, right) => left.ordinal - right.ordinal));
  }
  candidates.sort((left, right) => left.ordinal - right.ordinal);
  const rows = [];
  let previousBreakSerial = null;
  for (const candidate of candidates) {
    rows.push({
      ...candidate.point,
      break_before: previousBreakSerial !== null && candidate.breakSerial > previousBreakSerial,
    });
    previousBreakSerial = candidate.breakSerial;
  }
  return rows;
}

function parseEventHeader(header) {
  const headers = header.map((value) => value.trim());
  const column = (name) => headers.indexOf(name);
  const experiment = column("experiment_id"), elapsed = column("elapsed_s"), event = column("event");
  if (experiment < 0 || elapsed < 0 || event < 0) {
    throw new Error("配套 events.csv 缺少 experiment_id、elapsed_s 或 event 列；需要同一软件版本生成的实验文件");
  }
  return {
    experiment, elapsed, event,
    timestamp: column("timestamp_local"), sample: column("sample_index"),
    value: column("value_si"), unit: column("unit_si"),
  };
}

async function parseEventsFile(file, expectedExperimentId, job) {
  let columns = null;
  const events = [];
  const fileRead = await readCsvFile(file, (row) => {
    if (!columns) { columns = parseEventHeader(row); return; }
    if (!expectedExperimentId) throw new Error("测量 CSV 缺少 experiment_id，无法验证 events.csv 是否属于同一实验");
    if (row.some((value) => value.trim() !== "") && row[columns.experiment]?.trim() !== expectedExperimentId) {
      throw new Error("实验标识不一致：所选 events.csv 不是当前测量 CSV 的配套事件文件");
    }
    const elapsed = Number(row[columns.elapsed]);
    const text = row[columns.event]?.trim();
    if (!text || !Number.isFinite(elapsed)) return;
    events.push({
      experiment_id: row[columns.experiment], elapsed_s: elapsed, text,
      timestamp_local: columns.timestamp < 0 ? "" : row[columns.timestamp] || "",
      sample_index: columns.sample < 0 ? null : Number(row[columns.sample]) || null,
      value_si: columns.value < 0 || row[columns.value] === "" ? null : Number(row[columns.value]),
      unit_si: columns.unit < 0 ? "" : row[columns.unit] || "",
    });
  }, job, "events");
  if (!columns) throw new Error("事件 CSV 为空");
  if (!expectedExperimentId) throw new Error("测量 CSV 缺少 experiment_id，无法验证 events.csv 是否属于同一实验");
  const warnings = [];
  if (fileRead.incompleteTail) warnings.push("events.csv 尾部有未完成记录，已忽略该尾部；原文件未修改。");
  if (fileRead.missingFinalTerminator) {
    warnings.push("events.csv 末行没有 CR/LF 行结束符，已按 CSV 语法加载；无法判断该行是否曾被截断。");
  }
  return {
    events,
    warnings,
  };
}

function cancelJob(jobId) {
  const job = activeJobs.get(jobId);
  if (!job) return;
  job.cancelled = true;
  if (!job.reader || job.cancelledReaders.has(job.reader)) return;
  job.cancelledReaders.add(job.reader);
  try {
    const result = job.reader?.cancel?.();
    if (result && typeof result.catch === "function") result.catch(() => {});
  } catch { /* Cancellation is best effort; the parser also checks its cancellation flag. */ }
}

self.addEventListener("message", async ({ data }) => {
  if (data?.type === "cancel" || data?.action === "cancel") {
    const requestedId = data.jobId == null ? null : String(data.jobId);
    if (requestedId !== null) cancelJob(requestedId);
    else if (activeJobs.size) cancelJob(activeJobs.keys().next().value);
    return;
  }

  const request = data || {};
  const jobId = request.jobId == null ? `history-${++jobSequence}` : String(request.jobId);
  const job = { id: jobId, cancelled: false, reader: null, cancelledReaders: new Set(), reportProgress: request.reportProgress === true, lastProgressBytes: 0 };
  activeJobs.set(jobId, job);
  try {
    const { file, modes = [], kind = "measurements", experimentId } = request;
    if (!file || typeof file.size !== "number" || !Number.isFinite(file.size) || file.size < 0) throw new Error("无法读取所选 CSV 文件");
    if (kind === "events") {
      if (file.size > MAX_EVENTS_BYTES) throw new Error("事件 CSV 超过 10 MB，请先按实验区间拆分文件");
      self.postMessage({ ok: true, ...await parseEventsFile(file, experimentId, job) });
      return;
    }
    if (file.size > MAX_HISTORY_BYTES) throw new Error("历史 CSV 超过 2 GiB；请先按实验区间拆分文件");
    const modeOptions = Array.isArray(modes) ? modes : [];
    const metadata = await scanHistoryFile(file, modeOptions, job);
    throwIfCancelled(job);
    const rows = await sampleHistoryFile(file, metadata, job, request);
    throwIfCancelled(job);
    const warnings = [];
    if (metadata.incompleteTail) warnings.push("测量 CSV 尾部有未完成记录，已忽略该尾部；原文件未修改。");
    if (metadata.missingFinalTerminator) {
      warnings.push("测量 CSV 末行没有 CR/LF 行结束符，已按 CSV 语法加载；无法判断该行是否曾被截断。");
    }
    if (metadata.hasMissingExperimentId) {
      warnings.push("测量 CSV 有记录缺少 experiment_id；无法安全关联配套 events.csv。");
    }
    self.postMessage({
      ok: true,
      rows,
      modeValue: metadata.modeValue,
      unit: metadata.unit,
      experimentId: metadata.experimentId,
      stats: metadata.stats,
      rowCount: metadata.rowCount,
      plotPointCount: rows.length,
      warnings,
    });
  } catch (error) {
    self.postMessage({
      ok: false,
      ...(job.cancelled || error?.cancelled ? { cancelled: true } : {}),
      error: error?.message || "历史 CSV 解析失败",
    });
  } finally {
    if (activeJobs.get(jobId) === job) activeJobs.delete(jobId);
  }
});
