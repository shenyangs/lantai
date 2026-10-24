const path = require("node:path");
const { app, ipcMain, Menu } = require("electron");

const projectRoot = path.resolve(__dirname, "..");
const vendorRoot = path.join(projectRoot, "vendor", "lantai-docx-editor");
const fixturePath = path.resolve(
  process.argv[2] || path.join(vendorRoot, "fixtures", "generated", "compatibility-edit.xlsx"),
);
const outputPath = path.resolve(
  process.argv[3] || path.join(projectRoot, ".artifacts", "sheets-content-smoke.png"),
);
app.setPath("userData", path.join(projectRoot, ".artifacts", "sheets-content-smoke-user-data"));

async function main() {
  ipcMain.handle("ai:get-settings", () => ({
    provider: "custom",
    providers: { custom: { apiKey: "lantai-smoke", model: "沈行知" } },
  }));
  const runtime = require(path.join(vendorRoot, "apps", "sheets", "out", "main", "lantai.js"));
  runtime.configureSheetsRuntime({
    preloadPath: path.join(vendorRoot, "apps", "sheets", "out", "preload", "index.js"),
    rendererFile: path.join(vendorRoot, "apps", "sheets", "out", "renderer", "index.html"),
    sidecarPath: path.join(
      vendorRoot,
      "apps",
      "sheets",
      "native-bin",
      `${process.platform}-${process.arch}`,
      "xlsx-engine",
    ),
  });
  runtime.registerProjectIpc();
  runtime.setForcedWorkbookPath(fixturePath);

  const errors = [];
  const win = await runtime.createSheetsWindow({ includeAiHandlers: false });
  win.webContents.on("console-message", (details) => {
    if (details.level === "error") errors.push(details.message);
  });
  await new Promise((resolve) => setTimeout(resolve, 8_000));

  const diagnostics = await win.webContents.executeJavaScript(`(() => ({
    title: document.title,
    bodyText: document.body.innerText,
    inputValues: Array.from(document.querySelectorAll('input, textarea'))
      .map((element) => element.value)
      .filter(Boolean),
    queued: window.desktopApi?.hasQueuedWorkbook ? 'bridge-ready' : 'bridge-missing',
    firstInteractiveLeft: document.querySelector('.file-tab, button')?.getBoundingClientRect().left ?? 0
  }))()`);
  runtime.installSheetsMenu?.();
  const fileMenu = Menu.getApplicationMenu()?.items.find((item) => item.label === "文件");
  const fileMenuLabels = (fileMenu?.submenu?.items || [])
    .map((item) => String(item.label || "").trim())
    .filter(Boolean);
  const image = await win.webContents.capturePage();
  const bitmap = image.toBitmap();
  const size = image.getSize();
  // The workbook opens with A1 selected. Sample the interior of A1 rather
  // than DOM text: Univer paints cell glyphs on canvas, so `innerText` cannot
  // distinguish a populated workbook from the user-reported blank grid.
  const cellRegion = {
    x0: Math.floor(size.width * 0.033),
    x1: Math.floor(size.width * 0.092),
    y0: Math.floor(size.height * 0.218),
    y1: Math.floor(size.height * 0.244),
  };
  let darkCellPixels = 0;
  for (let y = cellRegion.y0; y < cellRegion.y1; y += 1) {
    for (let x = cellRegion.x0; x < cellRegion.x1; x += 1) {
      const offset = (y * size.width + x) * 4;
      if (
        bitmap[offset] < 90 &&
        bitmap[offset + 1] < 90 &&
        bitmap[offset + 2] < 90
      ) {
        darkCellPixels += 1;
      }
    }
  }
  require("node:fs").mkdirSync(path.dirname(outputPath), { recursive: true });
  require("node:fs").writeFileSync(outputPath, image.toPNG());
  const hasExpectedContent = darkCellPixels >= 20;
  const hasAgentEntry = diagnostics.bodyText.includes("沈行知");
  const fileMenuComplete =
    fileMenuLabels.some((label) => /^保存$/.test(label)) &&
    fileMenuLabels.some((label) => /另存为/.test(label)) &&
    fileMenuLabels.some((label) => /导出/.test(label)) &&
    fileMenuLabels.some((label) => /打印/.test(label));
  process.stdout.write(`${JSON.stringify({ fixturePath, outputPath, diagnostics, errors, cellRegion, darkCellPixels, hasExpectedContent, hasAgentEntry, fileMenuLabels, fileMenuComplete }, null, 2)}\n`);
  if (!hasExpectedContent) {
    throw new Error("Sheets content smoke failed: A1 was not rendered as Hello.");
  }
  if (!hasAgentEntry) {
    throw new Error("Sheets Agent smoke failed: the 沈行知 entry is missing.");
  }
  if (diagnostics.firstInteractiveLeft < 78) {
    throw new Error("Sheets title controls overlap the macOS traffic lights.");
  }
  if (!fileMenuComplete) {
    throw new Error(`Sheets File menu is incomplete: ${JSON.stringify(fileMenuLabels)}`);
  }
  if (errors.length > 0) {
    throw new Error(`Sheets content smoke reported renderer errors: ${errors.join(" | ")}`);
  }
  win.destroy();
  runtime.stopSheetsSidecar?.();
  app.quit();
}

app.whenReady().then(main).catch((error) => {
  console.error(error?.stack || error);
  app.exit(1);
});
