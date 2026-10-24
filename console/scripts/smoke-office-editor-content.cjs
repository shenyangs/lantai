const fs = require("node:fs");
const path = require("node:path");
const { app, ipcMain, Menu } = require("electron");
const { resolveOfficeEditorRuntimePaths } = require("../electron/officeEditorController.cjs");

const projectRoot = path.resolve(__dirname, "..");
const vendorRoot = path.join(projectRoot, "vendor", "lantai-docx-editor");
const artifactRoot = path.join(projectRoot, ".artifacts", "office-content-smoke");
const editor = String(process.argv[2] || "").toLowerCase();
app.setPath("userData", path.join(artifactRoot, `user-data-${editor || "unknown"}`));

function runtimePaths(kind) {
  const root = path.join(vendorRoot, "apps", kind, "out");
  let sidecarPath;
  if (kind === "sheets") {
    sidecarPath = resolveOfficeEditorRuntimePaths({
      isDev: true,
      electronDir: path.join(projectRoot, "electron"),
      resourcesPath: process.resourcesPath,
      platform: process.platform,
      arch: process.arch,
    }).sheets.sidecarPath;
    fs.accessSync(sidecarPath, fs.constants.X_OK);
  }
  return {
    preloadPath: path.join(root, "preload", "index.js"),
    rendererFile: path.join(root, "renderer", "index.html"),
    rendererFilePath: path.join(root, "renderer", "index.html"),
    ...(kind === "sheets" ? { sidecarPath } : {}),
  };
}

async function makeDocx() {
  const JSZip = require(path.join(vendorRoot, "node_modules", "jszip"));
  const zip = new JSZip();
  zip.file(
    "[Content_Types].xml",
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>' +
      '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">' +
      '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>' +
      '<Default Extension="xml" ContentType="application/xml"/>' +
      '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>' +
      '</Types>',
  );
  zip.file(
    "_rels/.rels",
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>' +
      '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">' +
      '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>' +
      '</Relationships>',
  );
  zip.file(
    "word/document.xml",
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>' +
      '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>' +
      '<w:p><w:r><w:t>Lantai DOCX smoke content</w:t></w:r></w:p>' +
      '<w:p><w:r><w:t>澜台文档真实内容验收</w:t></w:r></w:p>' +
      '<w:sectPr><w:pgSz w:w="11906" w:h="16838"/><w:pgMar w:top="1440" w:right="1440" w:bottom="1440" w:left="1440"/></w:sectPr>' +
      '</w:body></w:document>',
  );
  const target = path.join(artifactRoot, "content-smoke.docx");
  fs.writeFileSync(target, await zip.generateAsync({ type: "nodebuffer", compression: "DEFLATE" }));
  return target;
}

async function makePdf() {
  const { PDFDocument, StandardFonts, rgb } = require(path.join(
    vendorRoot,
    "node_modules",
    "pdf-lib",
  ));
  const doc = await PDFDocument.create();
  const page = doc.addPage([612, 792]);
  const font = await doc.embedFont(StandardFonts.Helvetica);
  page.drawText("Lantai PDF smoke content", {
    x: 72,
    y: 700,
    size: 28,
    font,
    color: rgb(0.12, 0.2, 0.32),
  });
  const secondPage = doc.addPage([612, 792]);
  secondPage.drawText("Lantai PDF second-page smoke", {
    x: 72,
    y: 700,
    size: 24,
    font,
    color: rgb(0.12, 0.2, 0.32),
  });
  const target = path.join(artifactRoot, "content-smoke.pdf");
  fs.writeFileSync(target, await doc.save());
  return target;
}

async function prepareEditor() {
  if (editor === "docs") {
    const fixturePath = await makeDocx();
    const runtime = require(path.join(vendorRoot, "apps", "docs", "out", "main", "lantai.js"));
    const paths = runtimePaths("docs");
    runtime.configureDocsRuntime(paths);
    runtime.registerProjectIpc();
    runtime.registerDocsIpc();
    return { fixturePath, runtime, win: runtime.createDocsWindow(fixturePath) };
  }
  if (editor === "sheets") {
    const fixturePath = path.join(
      vendorRoot,
      "fixtures",
      "generated",
      "compatibility-basic.xlsx",
    );
    const runtime = require(path.join(vendorRoot, "apps", "sheets", "out", "main", "lantai.js"));
    const paths = runtimePaths("sheets");
    runtime.configureSheetsRuntime(paths);
    runtime.registerProjectIpc();
    runtime.setForcedWorkbookPath(fixturePath);
    return {
      fixturePath,
      runtime,
      win: await runtime.createSheetsWindow({ includeAiHandlers: false }),
    };
  }
  if (editor === "slides") {
    const fixturePath = path.join(
      vendorRoot,
      "packages",
      "pptx-engine",
      "tests",
      "fixtures",
      "05_unicode_cjk_emoji.pptx",
    );
    const runtime = require(path.join(vendorRoot, "apps", "slides", "out", "main", "lantai.js"));
    const paths = runtimePaths("slides");
    runtime.configureSlidesRuntime(paths);
    runtime.registerSlidesIpc();
    runtime.registerProjectIpc();
    return { fixturePath, runtime, win: runtime.createSlidesWindow(fixturePath) };
  }
  if (editor === "pdf") {
    const fixturePath = await makePdf();
    const runtime = require(path.join(vendorRoot, "apps", "pdf", "out", "main", "lantai.js"));
    const paths = runtimePaths("pdf");
    runtime.configurePdfRuntime(paths);
    return { fixturePath, runtime, win: runtime.createPdfWindow(fixturePath) };
  }
  throw new Error("Usage: electron smoke-office-editor-content.cjs <docs|sheets|slides|pdf>");
}

async function inspect(win) {
  await new Promise((resolve, reject) => {
    const timer = setTimeout(resolve, 10_000);
    win.webContents.once("did-finish-load", () => {
      clearTimeout(timer);
      setTimeout(resolve, 6_000);
    });
    win.webContents.once("did-fail-load", (_event, code, description) => {
      clearTimeout(timer);
      reject(new Error(`did-fail-load ${code}: ${description}`));
    });
  });
  const diagnostics = await win.webContents.executeJavaScript(`(async () => {
    const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
    const ribbon = document.querySelector('.ribbon-body');
    const ribbonButtons = ribbon ? Array.from(ribbon.querySelectorAll('button')) : [];
    const lastButton = ribbonButtons.at(-1) || null;
    if (ribbon) {
      ribbon.scrollLeft = ribbon.scrollWidth;
      await wait(50);
    }
    const ribbonRect = ribbon?.getBoundingClientRect() || null;
    const lastRect = lastButton?.getBoundingClientRect() || null;
    const firstInteractive = document.querySelector('.ribbon-tab-file, .file-tab, button');
    const firstRect = firstInteractive?.getBoundingClientRect() || null;
    return {
      title: document.title,
      bodyText: document.body.innerText,
      pageCount: document.querySelectorAll('.doc-page, .pdf-page').length,
      thumbCount: document.querySelectorAll('.thumb, .slide-thumb, .thumbnail').length,
      canvasCount: document.querySelectorAll('canvas').length,
      inputValues: Array.from(document.querySelectorAll('input, textarea'))
        .map((element) => String(element.value || '').trim())
        .filter(Boolean),
      hasLantaiAgent: document.body.innerText.includes('沈行知'),
      hasLegacyVisibleBrand: /GenOffice|Genspark/i.test(document.body.innerText),
      trafficLightClearance: firstRect ? firstRect.left >= 78 : false,
      ribbon: ribbonRect && lastRect ? {
        clientWidth: ribbon.clientWidth,
        scrollWidth: ribbon.scrollWidth,
        scrollLeft: ribbon.scrollLeft,
        lastTitle: lastButton?.getAttribute('title') || '',
        lastReachable: lastRect.left >= ribbonRect.left - 1 && lastRect.right <= ribbonRect.right + 1,
      } : null,
      pdfCompactCommandFontSize: document.documentElement.dataset.officeApp === 'pdf'
        ? getComputedStyle(document.querySelector('.ribbon-body .rb-big')).fontSize
        : null,
    };
  })()`);
  const image = await win.webContents.capturePage();
  const bitmap = image.toBitmap();
  let nonWhitePixels = 0;
  for (let i = 0; i < bitmap.length; i += 16) {
    if (bitmap[i] < 245 || bitmap[i + 1] < 245 || bitmap[i + 2] < 245) nonWhitePixels += 1;
  }
  return { diagnostics, image, nonWhitePixels };
}

function inspectFileMenu(runtime) {
  const installMenu =
    editor === "docs"
      ? runtime.installDocsMenu
      : editor === "sheets"
        ? runtime.installSheetsMenu
      : editor === "slides"
        ? runtime.installSlidesMenu
        : runtime.installPdfMenu;
  installMenu?.();
  const menu = Menu.getApplicationMenu();
  const fileMenu = menu?.items.find((item) => item.label === "文件");
  const labels = (fileMenu?.submenu?.items || [])
    .map((item) => String(item.label || "").trim())
    .filter(Boolean);
  return {
    labels,
    hasSave: labels.some((label) => /^保存$/.test(label)),
    hasSaveAs: labels.some((label) => /另存为/.test(label)),
    hasExport: labels.some((label) => /导出/.test(label)),
    hasPrint: labels.some((label) => /打印/.test(label)),
  };
}

async function main() {
  fs.mkdirSync(artifactRoot, { recursive: true });
  // The Lantai host owns these handlers in production. Install deterministic
  // stand-ins so the editor smoke exercises the same host contract instead of
  // reporting a misleading missing-handler error.
  ipcMain.handle("ai:get-settings", () => ({
    provider: "custom",
    providers: { custom: { apiKey: "lantai-smoke", model: "沈行知" } },
  }));
  ipcMain.handle("ai:gsk-status", () => ({ loggedIn: false }));
  ipcMain.handle("app:get-theme", () => "light");
  const errors = [];
  const { fixturePath, runtime, win } = await prepareEditor();
  win.webContents.on("console-message", (details) => {
    if (details.level === "error") errors.push(details.message);
  });
  const { diagnostics, image, nonWhitePixels } = await inspect(win);
  const fileMenu = inspectFileMenu(runtime);
  const outputPath = path.join(artifactRoot, `${editor}.png`);
  fs.writeFileSync(outputPath, image.toPNG());
  const expectedContent =
    editor === "docs"
      ? diagnostics.bodyText.includes("Lantai DOCX smoke content")
      : editor === "sheets"
        ? diagnostics.canvasCount > 0 &&
          diagnostics.bodyText.includes("compatibility-basic.xlsx") &&
          diagnostics.bodyText.includes("1 行可用")
      : editor === "slides"
        ? diagnostics.thumbCount > 0 && diagnostics.canvasCount > 0
        : diagnostics.pageCount > 0 && diagnostics.canvasCount > 0;
  const result = {
    editor,
    fixturePath,
    outputPath,
    diagnostics,
    fileMenu,
    errors,
    nonWhitePixels,
    expectedContent,
  };
  process.stdout.write(`${JSON.stringify(result, null, 2)}\n`);
  if (!expectedContent) throw new Error(`${editor} content did not render`);
  if (diagnostics.bodyText.includes("No handler registered")) {
    throw new Error(`${editor} host contract is incomplete`);
  }
  if (/Error invoking remote method|ENOENT/.test(diagnostics.bodyText)) {
    throw new Error(`${editor} failed to load its real document: ${diagnostics.bodyText}`);
  }
  if (!diagnostics.hasLantaiAgent) throw new Error(`${editor} is missing the 沈行知 entry`);
  if (diagnostics.hasLegacyVisibleBrand) throw new Error(`${editor} still shows upstream branding`);
  if (!diagnostics.trafficLightClearance) throw new Error(`${editor} title controls overlap macOS traffic lights`);
  if (!fileMenu.hasSave || !fileMenu.hasSaveAs || !fileMenu.hasExport || !fileMenu.hasPrint) {
    throw new Error(`${editor} File menu is incomplete: ${JSON.stringify(fileMenu)}`);
  }
  if (diagnostics.ribbon && !diagnostics.ribbon.lastReachable) {
    throw new Error(`${editor} ribbon's last action cannot be reached`);
  }
  if (editor === "pdf" && diagnostics.pdfCompactCommandFontSize !== "0px") {
    throw new Error(`pdf compact ribbon labels are still visible: ${diagnostics.pdfCompactCommandFontSize}`);
  }
  if (nonWhitePixels < 500) throw new Error(`${editor} renderer is effectively blank`);
  if (errors.length > 0) throw new Error(`${editor} renderer errors: ${errors.join(" | ")}`);
  win.destroy();
  runtime.stopSheetsSidecar?.();
  app.quit();
}

app.whenReady().then(main).catch((error) => {
  console.error(error?.stack || error);
  app.exit(1);
});
