const path = require("node:path");
const { app } = require("electron");

const runtimeRoot = path.resolve(
  process.argv[2] || path.join(__dirname, "..", "vendor", "lantai-docx-editor", "apps"),
);
const editors = [
  {
    name: "docs",
    modulePath: path.join(runtimeRoot, "docs", "out", "main", "lantai.js"),
    exports: ["configureDocsRuntime", "createDocsWindow"],
  },
  {
    name: "sheets",
    modulePath: path.join(runtimeRoot, "sheets", "out", "main", "lantai.js"),
    exports: ["configureSheetsRuntime", "createSheetsWindow"],
  },
  {
    name: "slides",
    modulePath: path.join(runtimeRoot, "slides", "out", "main", "lantai.js"),
    exports: ["configureSlidesRuntime", "createSlidesWindow"],
  },
  {
    name: "pdf",
    modulePath: path.join(runtimeRoot, "pdf", "out", "main", "lantai.js"),
    exports: ["configurePdfRuntime", "createPdfWindow"],
  },
];

try {
  for (const editor of editors) {
    const loaded = require(editor.modulePath);
    const missing = editor.exports.filter(
      (exportName) => typeof loaded?.[exportName] !== "function",
    );
    if (missing.length > 0) {
      throw new Error(`${editor.name} runtime is missing: ${missing.join(", ")}`);
    }
  }
  console.log("Office runtime load smoke passed: docs, sheets, slides, pdf");
  app.quit();
} catch (error) {
  console.error(error?.stack || error);
  app.exit(1);
}
