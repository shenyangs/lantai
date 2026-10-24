import {
  accessSync,
  constants,
  readdirSync,
  readFileSync,
  statSync,
} from "node:fs";
import { createHash } from "node:crypto";
import { builtinModules } from "node:module";
import { resolve } from "node:path";

import { parseGenOfficeSourcePin } from "./sourcePin.mjs";

const root = resolve(import.meta.dirname, "..");
const vendorRoot = resolve(root, "vendor/lantai-docx-editor");
const requiredFiles = [
  "main/lantai.js",
  "preload/index.js",
  "renderer/index.html",
];

const editors = [
  { app: "docs", label: "DOCX", identity: "澜台文档" },
  { app: "sheets", label: "Sheets", identity: "澜台表格" },
  { app: "slides", label: "Slides", identity: "澜台演示" },
  { app: "pdf", label: "PDF", identity: "澜台 PDF" },
];

const allowedRuntimeRequires = new Set([
  "electron",
  ...builtinModules,
  ...builtinModules.map((name) => `node:${name}`),
]);

const forbiddenEmbeddedRuntimePatterns = [
  {
    label: "upstream account directory",
    pattern: /\.genspark-tool-cli|\.genoffice[\\/]/iu,
  },
  {
    label: "upstream account implementation",
    pattern: /genofficeAuthPath|ensureGenofficeLogin/iu,
  },
  { label: "upstream CLI package", pattern: /@genspark\/cli/iu },
  { label: "upstream tool endpoint", pattern: /genspark\.ai\/api\/tool_cli/iu },
  {
    label: "upstream login prompt",
    pattern: /Not logged in to Genspark|Genspark account/iu,
  },
  { label: "upstream traffic attribution", pattern: /X-Agent-Type/iu },
  { label: "upstream cloud-slide switch", pattern: /GENOFFICE_CLOUD_SLIDE/iu },
  { label: "upstream clipboard namespace", pattern: /io\.genoffice/iu },
  {
    label: "upstream temporary-data namespace",
    pattern: /genoffice-(?:pasted|imports|cloud-pages|filtered-copy)/iu,
  },
  {
    label: "upstream test export switch",
    pattern: /GENOFFICE_TEST_EXPORT_DIR/iu,
  },
];

function collectJavaScriptFiles(directory) {
  return readdirSync(directory, { withFileTypes: true }).flatMap((entry) => {
    const target = resolve(directory, entry.name);
    if (entry.isDirectory()) return collectJavaScriptFiles(target);
    return entry.isFile() && entry.name.endsWith(".js") ? [target] : [];
  });
}

function collectTextFiles(directory, extensions) {
  return readdirSync(directory, { withFileTypes: true }).flatMap((entry) => {
    const target = resolve(directory, entry.name);
    if (entry.isDirectory()) return collectTextFiles(target, extensions);
    return entry.isFile() &&
      extensions.some((extension) => entry.name.endsWith(extension))
      ? [target]
      : [];
  });
}

const source = readFileSync(resolve(vendorRoot, "SOURCE.md"), "utf8");
const sourcePin = parseGenOfficeSourcePin(source);
if (!sourcePin) {
  throw new Error(
    "DOCX editor SOURCE.md is missing an exact 40-character source pin",
  );
}

for (const editor of editors) {
  const outputRoot = resolve(vendorRoot, `apps/${editor.app}/out`);
  for (const relativePath of requiredFiles) {
    const target = resolve(outputRoot, relativePath);
    if (!statSync(target).isFile()) {
      throw new Error(
        `${editor.label} editor runtime is incomplete: ${relativePath}`,
      );
    }
  }
  const productBytes = requiredFiles
    .map((relativePath) =>
      readFileSync(resolve(outputRoot, relativePath), "utf8"),
    )
    .join("\n");
  const runtimeJavaScript = ["main", "preload"]
    .flatMap((folder) => collectJavaScriptFiles(resolve(outputRoot, folder)))
    .map((target) => readFileSync(target, "utf8"))
    .join("\n");
  const rendererProduct = collectTextFiles(resolve(outputRoot, "renderer"), [
    ".html",
    ".js",
    ".css",
  ])
    .map((target) => readFileSync(target, "utf8"))
    .join("\n");
  const embeddedRuntimeProduct = `${runtimeJavaScript}\n${rendererProduct}`;
  const externalRequires = [
    ...runtimeJavaScript.matchAll(/require\(["']([^"']+)["']\)/gu),
  ]
    .map((match) => match[1])
    .filter(
      (specifier) =>
        !specifier.startsWith(".") && !allowedRuntimeRequires.has(specifier),
    );
  if (externalRequires.length > 0) {
    throw new Error(
      `${editor.label} editor runtime depends on unpackaged modules: ${[
        ...new Set(externalRequires),
      ].join(", ")}`,
    );
  }
  if (/require\(["']electron-updater["']\)/.test(productBytes)) {
    throw new Error(
      `${editor.label} editor runtime contains a standalone auto-updater`,
    );
  }
  if (/GenOffice (Docs|Sheets|Slides|PDF)/i.test(productBytes)) {
    throw new Error(
      `${editor.label} editor product bundle still contains upstream product branding`,
    );
  }
  for (const forbidden of forbiddenEmbeddedRuntimePatterns) {
    if (forbidden.pattern.test(embeddedRuntimeProduct)) {
      throw new Error(
        `${editor.label} editor runtime still contains ${forbidden.label}`,
      );
    }
  }
  if (!productBytes.includes(editor.identity)) {
    throw new Error(
      `${editor.label} editor product bundle is missing ${editor.identity}`,
    );
  }
  if (
    rendererProduct.includes('className:"ai-rail"') ||
    rendererProduct.includes('className:"expand-copilot"') ||
    rendererProduct.includes("沈行知正在准备")
  ) {
    throw new Error(
      `${editor.label} editor renderer still contains the retired duplicate Agent rail`,
    );
  }
  if (
    !rendererProduct.includes("交给沈行知") ||
    !rendererProduct.includes("收起沈行知")
  ) {
    throw new Error(
      `${editor.label} editor renderer is missing the single title-row Agent toggle contract`,
    );
  }
}

const sidecarPath = resolve(
  vendorRoot,
  "apps/sheets/native-bin/darwin-arm64/xlsx-engine",
);
const metadataPath = resolve(
  vendorRoot,
  "apps/sheets/native-bin/darwin-arm64/METADATA.txt",
);
if (!statSync(sidecarPath).isFile() || !statSync(metadataPath).isFile()) {
  throw new Error(
    "Sheets native xlsx-engine or provenance metadata is missing",
  );
}
accessSync(sidecarPath, constants.X_OK);
const metadata = readFileSync(metadataPath, "utf8");
const expectedSha = metadata.match(/^sha256=([0-9a-f]{64})$/mu)?.[1];
const actualSha = createHash("sha256")
  .update(readFileSync(sidecarPath))
  .digest("hex");
if (!expectedSha || expectedSha !== actualSha) {
  throw new Error(
    "Sheets native xlsx-engine checksum does not match METADATA.txt",
  );
}

const pdfVisionPath = resolve(
  vendorRoot,
  process.platform === "darwin"
    ? "apps/pdf/generated-native/darwin-arm64/lantai-pdf-vision"
    : "apps/pdf/native-bin/darwin-arm64/lantai-pdf-vision",
);
if (!statSync(pdfVisionPath).isFile()) {
  throw new Error("PDF native Vision OCR helper is missing");
}
accessSync(pdfVisionPath, constants.X_OK);
const pdfVisionBytes = readFileSync(pdfVisionPath);
if (pdfVisionBytes.subarray(0, 4).toString("hex") !== "cffaedfe") {
  throw new Error(
    "PDF native Vision OCR helper is not an arm64 Mach-O executable",
  );
}
const pdfVisionSha = createHash("sha256").update(pdfVisionBytes).digest("hex");

console.log(
  `Office editor runtimes verified (${sourcePin.slice(0, 12)} source pin, four Lantai identities, xlsx-engine ${actualSha.slice(0, 12)}, PDF Vision ${pdfVisionSha.slice(0, 12)}).`,
);
