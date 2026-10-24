const path = require("node:path");

function isDocxPath(value) {
  return typeof value === "string" && /\.docx$/i.test(value.trim());
}

function isEditableDocumentPath(value) {
  return (
    typeof value === "string" &&
    /\.(docx|xlsx|xls|csv|pptx|pdf|md|markdown)$/i.test(value.trim())
  );
}

function createDocumentEntryController({
  app,
  Menu,
  dialog,
  platform = process.platform,
  openDocx,
  openDocument = openDocx,
  ensureDocumentHost = async () => {},
  showMainWindow = () => {},
  getMainWindow = () => null,
  appendLog = () => {},
  showOpenError = async () => {},
}) {
  let ready = false;
  const pendingPaths = [];

  async function openPath(targetPath) {
    const normalized = String(targetPath || "").trim();
    if (!isEditableDocumentPath(normalized)) return false;
    if (!ready) {
      if (!pendingPaths.includes(normalized)) pendingPaths.push(normalized);
      return true;
    }
    try {
      // Finder can deliver `open-file` before the regular chat window exists.
      // Office runtimes use that window as their non-modal shell/AI bridge, so
      // prepare it before consuming the queued path. The desktop entry point
      // supplies a hidden, deferred-load host to keep document opening fast.
      await ensureDocumentHost({ path: normalized });
      const result = await openDocument({ path: normalized, source_chat_id: "" });
      const success = result?.success !== false;
      if (!success) {
        throw Object.assign(
          new Error(result?.error || "澜台本地编辑器打开失败"),
          { code: result?.error_code || "document_open_failed" },
        );
      }
      return true;
    } catch (error) {
      const extension = path.extname(normalized).toLowerCase();
      const errorCode = error?.code || "document_open_failed";
      const message = error instanceof Error ? error.message : String(error);
      appendLog(
        `[DocumentEntry] open failed extension=${extension} code=${errorCode} message=${message}`,
      );
      showMainWindow({ focus: true });
      await showOpenError({ path: normalized, error: message, error_code: errorCode });
      return false;
    }
  }

  function captureArguments(argv) {
    const target = Array.from(argv || []).find(isEditableDocumentPath);
    if (target) void openPath(target);
  }

  function installEventHandlers(initialArgv = []) {
    app.on("open-file", (event, targetPath) => {
      event?.preventDefault?.();
      void openPath(targetPath);
    });
    app.on("second-instance", (_event, commandLine) => {
      const target = Array.from(commandLine || []).find(isEditableDocumentPath);
      if (target) {
        void openPath(target);
        return;
      }
      showMainWindow({ focus: true });
    });
    captureArguments(initialArgv);
  }

  async function markReady() {
    ready = true;
    const queued = pendingPaths.splice(0);
    for (const targetPath of queued) {
      await openPath(targetPath);
    }
  }

  async function chooseDocument() {
    const mainWindow = getMainWindow();
    const options = {
      title: "打开本地文档",
      buttonLabel: "在澜台中编辑",
      properties: ["openFile"],
      filters: [
        {
          name: "澜台支持的文档",
          extensions: ["docx", "xlsx", "xls", "csv", "pptx", "pdf", "md", "markdown"],
        },
        { name: "Word 文档", extensions: ["docx"] },
        { name: "Excel 表格", extensions: ["xlsx", "xls", "csv"] },
        { name: "PowerPoint 演示", extensions: ["pptx"] },
        { name: "PDF 文档", extensions: ["pdf"] },
        { name: "Markdown 文档", extensions: ["md", "markdown"] },
      ],
    };
    const result =
      mainWindow && !mainWindow.isDestroyed?.()
        ? await dialog.showOpenDialog(mainWindow, options)
        : await dialog.showOpenDialog(options);
    if (result?.canceled || !result?.filePaths?.[0]) return false;
    return await openPath(result.filePaths[0]);
  }

  function installApplicationMenu() {
    const editMenu = {
      label: "编辑",
      submenu: [
        { role: "undo" },
        { role: "redo" },
        { type: "separator" },
        { role: "cut" },
        { role: "copy" },
        { role: "paste" },
        { role: "selectAll" },
      ],
    };
    const template = [
      ...(platform === "darwin"
        ? [
            {
              label: app.name || "澜台",
              submenu: [
                { role: "about" },
                { type: "separator" },
                { role: "services" },
                { type: "separator" },
                { role: "hide" },
                { role: "hideOthers" },
                { role: "unhide" },
                { type: "separator" },
                { role: "quit" },
              ],
            },
          ]
        : []),
      {
        label: "文件",
        submenu: [
          {
            label: "打开文档…",
            accelerator: "CmdOrCtrl+O",
            click: () => chooseDocument(),
          },
          { type: "separator" },
          platform === "darwin" ? { role: "close" } : { role: "quit" },
        ],
      },
      editMenu,
      {
        label: "视图",
        submenu: [
          { role: "reload" },
          { role: "forceReload" },
          { role: "toggleDevTools" },
          { type: "separator" },
          { role: "resetZoom" },
          { role: "zoomIn" },
          { role: "zoomOut" },
          { type: "separator" },
          { role: "togglefullscreen" },
        ],
      },
      {
        label: "窗口",
        submenu: [
          { role: "minimize" },
          { role: "zoom" },
          ...(platform === "darwin"
            ? [{ type: "separator" }, { role: "front" }]
            : [{ role: "close" }]),
        ],
      },
    ];
    Menu.setApplicationMenu(Menu.buildFromTemplate(template));
  }

  return {
    installApplicationMenu,
    installEventHandlers,
    markReady,
    openPath,
  };
}

module.exports = {
  createDocumentEntryController,
  isEditableDocumentPath,
  isDocxPath,
};
