#!/usr/bin/env node

import { createHash } from "node:crypto";
import fs from "node:fs/promises";
import { createRequire } from "node:module";
import path from "node:path";
import { pathToFileURL } from "node:url";

function parseArgs(argv) {
  const values = {};
  for (let index = 0; index < argv.length; index += 1) {
    const token = argv[index];
    if (!token.startsWith("--")) throw new Error(`Unexpected argument: ${token}`);
    const key = token.slice(2).replaceAll("-", "_");
    const value = argv[index + 1];
    if (!value || value.startsWith("--")) throw new Error(`Missing value for ${token}`);
    values[key] = value;
    index += 1;
  }
  return values;
}

function required(values, name) {
  const value = values[name];
  if (!value) throw new Error(`--${name.replaceAll("_", "-")} is required`);
  return value;
}

async function importArtifactTool() {
  const nodeModules = process.env.RUNTIME_NODE_MODULES;
  if (!nodeModules || !path.isAbsolute(nodeModules)) {
    throw new Error("RUNTIME_NODE_MODULES must be an absolute path");
  }
  const requireFromRuntime = createRequire(path.join(nodeModules, "__runtime__.cjs"));
  const entrypoint = requireFromRuntime.resolve("@oai/artifact-tool");
  return import(pathToFileURL(entrypoint).href);
}

async function writeBlob(output, blob) {
  await fs.writeFile(output, new Uint8Array(await blob.arrayBuffer()));
}

function parseInspect(ndjson) {
  return ndjson
    .split("\n")
    .filter(Boolean)
    .map((line) => JSON.parse(line));
}

async function inspectAll(presentation) {
  const inspected = await presentation.inspect({
    kind: "slide,textbox,shape,image,table,chart",
    maxChars: 250000,
  });
  return parseInspect(inspected.ndjson);
}

function firstRecord(records, kind) {
  return records.find((record) => record.kind === kind);
}

async function sha256(file) {
  return createHash("sha256").update(await fs.readFile(file)).digest("hex");
}

async function saveSlideEvidence(presentation, slideId, prefix) {
  const slide = presentation.resolve(slideId);
  await writeBlob(`${prefix}.png`, await presentation.export({ slide, format: "png", scale: 1 }));
  await writeBlob(`${prefix}.layout.json`, await slide.export({ format: "layout" }));
}

function findSlideId(records, slideNumber) {
  const record = records.find((item) => item.kind === "slide" && item.slide === slideNumber);
  if (!record) throw new Error(`Could not resolve slide ${slideNumber}`);
  return record.id;
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  const input = path.resolve(required(args, "input"));
  const output = path.resolve(required(args, "output"));
  const evidenceDir = path.resolve(required(args, "evidence_dir"));
  const textSearch = required(args, "text_search");
  const textReplacement = required(args, "text_replacement");
  const mutateTable = args.mutate_table === "true";
  const mutateChart = args.mutate_chart === "true";
  const tableMarker = args.table_marker ?? "EDIT-CHECK";
  const chartDelta = Number(args.chart_delta ?? "1");
  if (!Number.isFinite(chartDelta) || chartDelta === 0) {
    throw new Error("--chart-delta must be a finite non-zero number");
  }

  await fs.mkdir(path.dirname(output), { recursive: true });
  await fs.mkdir(evidenceDir, { recursive: true });
  const { FileBlob, PresentationFile } = await importArtifactTool();
  const source = await PresentationFile.importPptx(await FileBlob.load(input));
  const beforeRecords = await inspectAll(source);
  const textSearchRecords = parseInspect((await source.inspect({
    kind: "slide,textbox",
    search: textSearch,
    maxChars: 12000,
  })).ndjson);
  const textRecord = [...textSearchRecords, ...beforeRecords].find(
    (record) => record.kind === "textbox" && record.text?.includes(textSearch),
  );
  if (!textRecord) throw new Error(`Could not find editable text: ${textSearch}`);

  const tableRecord = firstRecord(beforeRecords, "table");
  const chartRecord = firstRecord(beforeRecords, "chart");
  if (mutateTable && !tableRecord) throw new Error("Requested table mutation but no native table was imported");
  if (mutateChart && !chartRecord) throw new Error("Requested chart mutation but no native chart was imported");

  const touchedSlides = new Set([textRecord.slide]);
  if (mutateTable) touchedSlides.add(tableRecord.slide);
  if (mutateChart) touchedSlides.add(chartRecord.slide);
  for (const slideNumber of touchedSlides) {
    await saveSlideEvidence(
      source,
      findSlideId(beforeRecords, slideNumber),
      path.join(evidenceDir, `slide-${slideNumber}-before`),
    );
  }

  const textTarget = source.resolve(textRecord.id);
  textTarget.text.replace(textSearch, textReplacement);

  let tableEvidence = null;
  if (mutateTable) {
    const table = source.resolve(tableRecord.id);
    if (table.rowCount < 2 || table.columnCount < 2) {
      throw new Error("Native table is too small for a non-header edit probe");
    }
    const cell = table.getCell(1, 1);
    tableEvidence = { id: tableRecord.id, row: 1, column: 1, before: String(cell.value), after: tableMarker };
    cell.value = tableMarker;
  }

  let chartEvidence = null;
  if (mutateChart) {
    const chart = source.resolve(chartRecord.id);
    const series = chart.series.getItemAt(0);
    const values = Array.from(series.values ?? []);
    if (!values.length || !Number.isFinite(Number(values[0]))) {
      throw new Error("Native chart does not expose a numeric first series value");
    }
    const updated = Number(values[0]) + chartDelta;
    chartEvidence = {
      id: chartRecord.id,
      series: 0,
      point: 0,
      before: Number(values[0]),
      after: updated,
    };
    series.values = [updated, ...values.slice(1)];
  }

  for (const slideNumber of touchedSlides) {
    await saveSlideEvidence(
      source,
      findSlideId(beforeRecords, slideNumber),
      path.join(evidenceDir, `slide-${slideNumber}-after`),
    );
  }
  await writeBlob(
    path.join(evidenceDir, "deck-after.webp"),
    await source.export({ format: "webp", montage: true, scale: 1 }),
  );

  const exported = await PresentationFile.exportPptx(source);
  await exported.save(output);
  const reopened = await PresentationFile.importPptx(await FileBlob.load(output));
  const afterRecords = await inspectAll(reopened);
  const reopenedTextRecords = parseInspect((await reopened.inspect({
    kind: "slide,textbox",
    search: textReplacement,
    maxChars: 12000,
  })).ndjson);
  const reopenedText = [...reopenedTextRecords, ...afterRecords].find(
    (record) => record.kind === "textbox" && record.text?.includes(textReplacement),
  );
  if (!reopenedText) throw new Error("Text mutation did not survive PPTX export and re-import");

  if (tableEvidence) {
    const reopenedTableRecord = firstRecord(afterRecords, "table");
    if (!reopenedTableRecord) throw new Error("Native table was lost after PPTX export and re-import");
    const reopenedTable = reopened.resolve(reopenedTableRecord.id);
    const value = String(reopenedTable.getCell(tableEvidence.row, tableEvidence.column).value);
    if (value !== tableEvidence.after) {
      throw new Error(`Table mutation did not survive re-import: ${value}`);
    }
  }

  if (chartEvidence) {
    const reopenedChartRecord = firstRecord(afterRecords, "chart");
    if (!reopenedChartRecord) throw new Error("Native chart was lost after PPTX export and re-import");
    const value = Number(reopened.resolve(reopenedChartRecord.id).series.getItemAt(0).values[0]);
    if (value !== chartEvidence.after) {
      throw new Error(`Chart mutation did not survive re-import: ${value}`);
    }
  }

  const report = {
    schemaVersion: 1,
    input,
    inputSha256: await sha256(input),
    output,
    outputSha256: await sha256(output),
    slideCount: afterRecords.filter((record) => record.kind === "slide").length,
    text: {
      id: textRecord.id,
      slide: textRecord.slide,
      before: textSearch,
      after: textReplacement,
      persisted: true,
    },
    table: tableEvidence ? { ...tableEvidence, persisted: true, nativeAfterReopen: true } : null,
    chart: chartEvidence ? { ...chartEvidence, persisted: true, nativeAfterReopen: true } : null,
    evidenceDir,
    passed: true,
  };
  await fs.writeFile(path.join(evidenceDir, "artifact-editability-report.json"), `${JSON.stringify(report, null, 2)}\n`);
  process.stdout.write(`${JSON.stringify(report, null, 2)}\n`);
}

main().catch((error) => {
  console.error(error.stack || error.message || String(error));
  process.exitCode = 1;
});
