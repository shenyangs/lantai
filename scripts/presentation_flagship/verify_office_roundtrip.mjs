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
    const value = argv[index + 1];
    if (!token?.startsWith("--") || !value || value.startsWith("--")) {
      throw new Error(`Invalid argument near ${token ?? "<end>"}`);
    }
    values[token.slice(2).replaceAll("-", "_")] = value;
    index += 1;
  }
  return values;
}

function required(values, name) {
  const value = values[name];
  if (!value) throw new Error(`--${name.replaceAll("_", "-")} is required`);
  return path.resolve(value);
}

async function importArtifactTool() {
  const nodeModules = process.env.RUNTIME_NODE_MODULES;
  if (!nodeModules || !path.isAbsolute(nodeModules)) {
    throw new Error("RUNTIME_NODE_MODULES must be an absolute path");
  }
  const requireFromRuntime = createRequire(path.join(nodeModules, "__runtime__.cjs"));
  return import(pathToFileURL(requireFromRuntime.resolve("@oai/artifact-tool")).href);
}

function parseInspect(ndjson) {
  return ndjson.split("\n").filter(Boolean).map((line) => JSON.parse(line));
}

async function sha256(file) {
  return createHash("sha256").update(await fs.readFile(file)).digest("hex");
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  const input = required(args, "input");
  const expectedReportPath = required(args, "expected_report");
  const outputReport = required(args, "output_report");
  const expected = JSON.parse(await fs.readFile(expectedReportPath, "utf8"));
  const { FileBlob, PresentationFile } = await importArtifactTool();
  const presentation = await PresentationFile.importPptx(await FileBlob.load(input));
  const inventory = parseInspect((await presentation.inspect({
    kind: "slide,table,chart",
    maxChars: 100000,
  })).ndjson);
  const textRecords = parseInspect((await presentation.inspect({
    kind: "slide,textbox",
    search: expected.text.after,
    maxChars: 12000,
  })).ndjson);
  const textRecord = textRecords.find(
    (record) => record.kind === "textbox" && record.text?.includes(expected.text.after),
  );
  if (!textRecord) throw new Error("Edited text was not preserved by the office round-trip");

  const tables = inventory.filter((record) => record.kind === "table");
  const charts = inventory.filter((record) => record.kind === "chart");
  let tableValue = null;
  if (expected.table) {
    if (!tables.length) throw new Error("Native table was lost by the office round-trip");
    tableValue = String(
      presentation.resolve(tables[0].id).getCell(expected.table.row, expected.table.column).value,
    );
    if (tableValue !== expected.table.after) {
      throw new Error(`Edited native table value changed after office save: ${tableValue}`);
    }
  }

  let chartValue = null;
  if (expected.chart) {
    if (!charts.length) throw new Error("Native chart was lost by the office round-trip");
    chartValue = Number(presentation.resolve(charts[0].id).series.getItemAt(0).values[0]);
    if (chartValue !== expected.chart.after) {
      throw new Error(`Edited native chart value changed after office save: ${chartValue}`);
    }
  }

  const report = {
    schemaVersion: 1,
    officeApplication: "LibreOffice Impress",
    operation: "open-and-save-as-pptx",
    input,
    inputSha256: await sha256(input),
    sourceEditabilityReport: expectedReportPath,
    slideCount: inventory.filter((record) => record.kind === "slide").length,
    nativeTableCount: tables.length,
    nativeChartCount: charts.length,
    text: { expected: expected.text.after, actual: textRecord.text, persisted: true },
    table: expected.table ? { expected: expected.table.after, actual: tableValue, persisted: true } : null,
    chart: expected.chart ? { expected: expected.chart.after, actual: chartValue, persisted: true } : null,
    passed: true,
  };
  await fs.mkdir(path.dirname(outputReport), { recursive: true });
  await fs.writeFile(outputReport, `${JSON.stringify(report, null, 2)}\n`);
  process.stdout.write(`${JSON.stringify(report, null, 2)}\n`);
}

main().catch((error) => {
  console.error(error.stack || error.message || String(error));
  process.exitCode = 1;
});
