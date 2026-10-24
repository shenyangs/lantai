const fs = require("node:fs");
const path = require("node:path");
const { app, BrowserWindow, Menu, dialog, ipcMain, screen } = require("electron");

const consoleRoot = path.resolve(__dirname, "..");
const artifactRoot = path.join(consoleRoot, ".artifacts", "markdown-editor-smoke");
const fixturePath = path.join(artifactRoot, "markdown-smoke.md");
const expectedInitial = "# 澜台 Markdown 验收\n\n- 本地读取\n- 即时预览\n";
const expectedSaved = `${expectedInitial}\n## 保存回写\n\n**沈行知** 与用户共同创作。\n`;

app.setPath("userData", path.join(artifactRoot, "user-data"));

async function waitFor(predicate, timeoutMs = 8_000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (await predicate()) return;
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
  throw new Error("Markdown renderer did not reach the expected state in time");
}

async function main() {
  fs.mkdirSync(artifactRoot, { recursive: true });
  fs.writeFileSync(fixturePath, expectedInitial, "utf8");

  const { createMarkdownEditorController } = require(path.join(
    consoleRoot,
    "electron",
    "markdownEditorController.cjs",
  ));
  const controller = createMarkdownEditorController({
    BrowserWindow,
    Menu,
    dialog,
    ipcMain,
    screen,
    electronDir: path.join(consoleRoot, "electron"),
    isTrustedIpcEvent: () => true,
  });
  controller.installIpcHandlers();
  const opened = await controller.openLocalMarkdown(fixturePath);
  if (!opened.success) throw new Error(`Markdown open failed: ${JSON.stringify(opened)}`);

  const win = BrowserWindow.getAllWindows()[0];
  if (!win) throw new Error("Markdown window was not created");
  const rendererErrors = [];
  win.webContents.on("console-message", (details) => {
    if (details.level === "error") rendererErrors.push(details.message);
  });
  await waitFor(() =>
    win.webContents.executeJavaScript(
      `document.querySelector('#editor')?.value.includes('澜台 Markdown 验收') === true`,
    ),
  );

  const initial = await win.webContents.executeJavaScript(`(() => {
    const fileButton = document.querySelector('#file-button');
    const rect = fileButton?.getBoundingClientRect();
    return {
      editorText: document.querySelector('#editor')?.value || '',
      previewHeading: document.querySelector('#preview h1')?.textContent || '',
      previewItems: Array.from(document.querySelectorAll('#preview li')).map((node) => node.textContent),
      fileButtonLeft: rect?.left ?? 0,
      title: document.title,
    };
  })()`);

  await win.webContents.executeJavaScript(`(() => {
    const editor = document.querySelector('#editor');
    editor.value = ${JSON.stringify(expectedSaved)};
    editor.dispatchEvent(new Event('input', { bubbles: true }));
    document.querySelector('#save-button').click();
  })()`);
  await waitFor(() => fs.readFileSync(fixturePath, "utf8") === expectedSaved);
  const saved = await win.webContents.executeJavaScript(`(() => ({
    status: document.querySelector('#status')?.textContent || '',
    previewHeading: document.querySelector('#preview h2')?.textContent || '',
    previewStrong: document.querySelector('#preview strong')?.textContent || '',
    count: document.querySelector('#count')?.textContent || '',
  }))()`);

  controller.installMarkdownMenu();
  const fileMenu = Menu.getApplicationMenu()?.items.find((item) => item.label === "文件");
  const fileMenuLabels = (fileMenu?.submenu?.items || [])
    .map((item) => String(item.label || "").trim())
    .filter(Boolean);
  const image = await win.webContents.capturePage();
  const screenshotPath = path.join(artifactRoot, "markdown.png");
  fs.writeFileSync(screenshotPath, image.toPNG());

  const result = {
    fixturePath,
    screenshotPath,
    initial,
    saved,
    fileMenuLabels,
    rendererErrors,
  };
  process.stdout.write(`${JSON.stringify(result, null, 2)}\n`);

  if (initial.editorText !== expectedInitial) throw new Error("Markdown source was not loaded exactly");
  if (initial.previewHeading !== "澜台 Markdown 验收" || initial.previewItems.length !== 2) {
    throw new Error("Markdown preview did not render the initial document");
  }
  if (initial.fileButtonLeft < 78) throw new Error("Markdown toolbar overlaps macOS traffic lights");
  if (saved.previewHeading !== "保存回写" || saved.previewStrong !== "沈行知") {
    throw new Error("Markdown preview did not update after editing");
  }
  if (!fileMenuLabels.some((label) => /保存/.test(label)) || !fileMenuLabels.some((label) => /另存为/.test(label))) {
    throw new Error(`Markdown File menu is incomplete: ${JSON.stringify(fileMenuLabels)}`);
  }
  if (rendererErrors.length > 0) throw new Error(rendererErrors.join(" | "));

  win.destroy();
  app.quit();
}

app.whenReady().then(main).catch((error) => {
  console.error(error?.stack || error);
  app.exit(1);
});
