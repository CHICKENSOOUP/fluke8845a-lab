"use strict";

const MAX_HISTORY_BYTES = 50 * 1024 * 1024;
const MAX_EVENTS_BYTES = 10 * 1024 * 1024;

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

function parseHistoryFile(text, modeOptions) {
  const table = parseCsv(text.replace(/^\uFEFF/, ""));
  if (table.length < 2) throw new Error("CSV 没有测量数据行");
  const headers = table[0].map((value) => value.trim());
  const column = (name) => headers.indexOf(name);
  const elapsedColumn = column("elapsed_s");
  const valueColumn = column("value_si");
  const statusColumn = column("status");
  const modeColumn = column("mode");
  const unitColumn = column("unit_si");
  const missingColumns = ["elapsed_s", "value_si", "status"].filter((name) => column(name) < 0);
  if (missingColumns.length) {
    throw new Error(`CSV 缺少列：${missingColumns.join("、")}。可接受格式至少包含 elapsed_s、value_si、status；请使用原始 measurements.csv，或补齐这些列后重试。`);
  }
  const sourceRows = table.slice(1);
  const modes = modeColumn >= 0 ? [...new Set(sourceRows.map((row) => row[modeColumn]?.trim()).filter(Boolean))] : [];
  const units = unitColumn >= 0 ? [...new Set(sourceRows.map((row) => row[unitColumn]?.trim()).filter(Boolean))] : [];
  const mixed = [];
  if (modes.length > 1) mixed.push(`不同测量模式（${modes.join("、")}）`);
  if (units.length > 1) mixed.push(`不同单位（${units.join("、")}）`);
  if (mixed.length) throw new Error(`CSV 混合了${mixed.join("；")}，请按单一模式和单位拆分文件后重试`);
  const experimentColumn = column("experiment_id");
  const experimentIds = experimentColumn >= 0 ? [...new Set(sourceRows.map((row) => row[experimentColumn]?.trim()).filter(Boolean))] : [];
  if (experimentIds.length > 1) throw new Error("CSV 包含多个实验标识，请按实验拆分文件");
  const modeValue = modes[0] || "";
  const mode = modeOptions.find((item) => item.value === modeValue);
  if (modeValue && !mode) {
    const supported = modeOptions.map((item) => item.value).join("、");
    throw new Error(`CSV 测量模式 ${modeValue} 不受支持；可识别模式：${supported || "尚未加载"}。`);
  }
  if (mode && units.length && units[0] !== mode.unit) {
    throw new Error(`CSV 单位 ${units[0]} 与测量模式 ${modeValue} 的单位 ${mode.unit} 不匹配；请核对原始 CSV 表头和值。`);
  }
  const unit = units[0] || mode?.unit;
  const rawColumn = column("raw_reading");
  const idColumn = column("sample_index");
  const rows = [];
  for (const row of sourceRows) {
    const x = Number(row[elapsedColumn]);
    const y = row[valueColumn] === "" ? NaN : Number(row[valueColumn]);
    rows.push({ x, y, status: row[statusColumn], mode: modeValue, unit, raw: rawColumn >= 0 ? row[rawColumn] : "", id: idColumn >= 0 ? (Number(row[idColumn]) || rows.length + 1) : rows.length + 1 });
  }
  if (!rows.some((row) => row.status === "ok" && Number.isFinite(row.x) && Number.isFinite(row.y))) {
    throw new Error("CSV 没有有效读数；检查 status、elapsed_s、value_si 列，确认至少有一行 status=ok 且数值有效");
  }
  return { rows, modeValue, unit, experimentId: experimentIds[0] || null };
}

function parseEventsFile(text, expectedExperimentId) {
  const table = parseCsv(text.replace(/^\uFEFF/, ""));
  if (!table.length) throw new Error("事件 CSV 为空");
  const headers = table[0].map((value) => value.trim());
  const column = (name) => headers.indexOf(name);
  const experimentColumn = column("experiment_id"), elapsedColumn = column("elapsed_s"), eventColumn = column("event");
  if (experimentColumn < 0 || elapsedColumn < 0 || eventColumn < 0) {
    throw new Error("配套 events.csv 缺少 experiment_id、elapsed_s 或 event 列；需要同一软件版本生成的实验文件");
  }
  if (!expectedExperimentId) throw new Error("测量 CSV 缺少 experiment_id，无法验证 events.csv 是否属于同一实验");
  const rows = table.slice(1);
  const nonEmptyRows = rows.filter((row) => row.some((value) => value.trim() !== ""));
  if (nonEmptyRows.some((row) => row[experimentColumn]?.trim() !== expectedExperimentId)) {
    throw new Error("实验标识不一致：所选 events.csv 不是当前测量 CSV 的配套事件文件");
  }
  const timestampColumn = column("timestamp_local"), sampleColumn = column("sample_index");
  const valueColumn = column("value_si"), unitColumn = column("unit_si");
  const events = rows.flatMap((row) => {
    const elapsed = Number(row[elapsedColumn]);
    const text = row[eventColumn]?.trim();
    if (!text || !Number.isFinite(elapsed)) return [];
    return [{
      experiment_id: row[experimentColumn], elapsed_s: elapsed, text,
      timestamp_local: timestampColumn < 0 ? "" : row[timestampColumn] || "",
      sample_index: sampleColumn < 0 ? null : Number(row[sampleColumn]) || null,
      value_si: valueColumn < 0 || row[valueColumn] === "" ? null : Number(row[valueColumn]),
      unit_si: unitColumn < 0 ? "" : row[unitColumn] || "",
    }];
  });
  return { events };
}

self.addEventListener("message", async ({ data }) => {
  const { file, modes = [], kind = "measurements", experimentId } = data || {};
  try {
    if (!file || typeof file.size !== "number") throw new Error("无法读取所选 CSV 文件");
    if (kind === "events") {
      if (file.size > MAX_EVENTS_BYTES) throw new Error("事件 CSV 超过 10 MB，请先按实验区间拆分文件");
      self.postMessage({ ok: true, ...parseEventsFile(await file.text(), experimentId) });
      return;
    }
    if (file.size > MAX_HISTORY_BYTES) throw new Error("历史 CSV 超过 50 MB，请先按实验区间拆分文件");
    const parsed = parseHistoryFile(await file.text(), modes);
    self.postMessage({ ok: true, ...parsed });
  } catch (error) {
    self.postMessage({ ok: false, error: error?.message || "历史 CSV 解析失败" });
  }
});
