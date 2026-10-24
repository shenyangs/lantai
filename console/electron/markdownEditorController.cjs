const fs = require("node:fs");
const path = require("node:path");

const MARKDOWN_EDITOR_CHANNEL = "lantai:markdown-editor-open";
const MARKDOWN_MAX_BYTES = 100 * 1024 * 1024;
const MARKDOWN_HANDOFF_MAX_CHARS = 20_000;
const CHANNELS = Object.freeze({
  consume: "lantai:markdown-consume",
  chooseOpen: "lantai:markdown-choose-open",
  save: "lantai:markdown-save",
  dirty: "lantai:markdown-dirty",
  command: "lantai:markdown-command",
  closeSaved: "lantai:markdown-close-saved",
  handoff: "lantai:markdown-handoff-to-chat",
});

function resultError(error, errorCode) {
  return { success: false, error, error_code: errorCode };
}

function createMarkdownEditorController({
  BrowserWindow,
  Menu,
  dialog,
  ipcMain,
  screen,
  electronDir = __dirname,
  fsImpl = fs,
  getMainWindow = () => null,
  showMainWindow = () => {},
  isTrustedIpcEvent = () => false,
}) {
  const pathsByContents = new Map();
  const dirtyContents = new Set();

  function validateTarget(targetPath) {
    if (typeof targetPath !== "string" || !targetPath.trim()) {
      return resultError("Markdown 文件路径为空", "markdown_path_empty");
    }
    if (!/\.(md|markdown)$/i.test(targetPath.trim())) {
      return resultError("仅支持 MD 和 Markdown 文件", "markdown_type_unsupported");
    }
    try {
      const canonicalPath = fsImpl.realpathSync(targetPath.trim());
      const stat = fsImpl.statSync(canonicalPath);
      if (!stat.isFile()) return resultError("Markdown 路径不是文件", "markdown_not_file");
      if (stat.size > MARKDOWN_MAX_BYTES) {
        return resultError("Markdown 文件不能超过 100 MB", "markdown_too_large");
      }
      return { success: true, path: canonicalPath };
    } catch {
      return resultError("Markdown 文件不存在或无法读取", "markdown_unreadable");
    }
  }

  function focusedMarkdownContents() {
    const contents = BrowserWindow.getFocusedWindow()?.webContents;
    return contents && !contents.isDestroyed?.() && pathsByContents.has(contents.id) ? contents : null;
  }

  function safeSend(contents, channel, ...args) {
    if (!contents || contents.isDestroyed?.()) return false;
    try {
      contents.send(channel, ...args);
      return true;
    } catch {
      return false;
    }
  }

  function safeDestroy(window) {
    if (!window || window.isDestroyed?.()) return false;
    try {
      window.destroy();
      return true;
    } catch {
      return false;
    }
  }

  function installMarkdownMenu() {
    const send = (command) => safeSend(focusedMarkdownContents(), CHANNELS.command, command);
    Menu.setApplicationMenu(
      Menu.buildFromTemplate([
        ...(process.platform === "darwin" ? [{ role: "appMenu" }] : []),
        {
          label: "文件",
          submenu: [
            { label: "打开…", accelerator: "CmdOrCtrl+O", click: () => send("open") },
            { type: "separator" },
            { label: "保存", accelerator: "CmdOrCtrl+S", click: () => send("save") },
            { label: "另存为…", accelerator: "Shift+CmdOrCtrl+S", click: () => send("save-as") },
            { type: "separator" },
            { role: "close" },
          ],
        },
        { role: "editMenu" },
        {
          label: "视图",
          submenu: [
            { label: "编辑", click: () => send("view-edit") },
            { label: "预览", click: () => send("view-preview") },
            { label: "分栏", click: () => send("view-split") },
            { type: "separator" },
            { role: "togglefullscreen" },
          ],
        },
        { role: "windowMenu" },
      ]),
    );
  }

  async function confirmClose(contents, parent) {
    const contentsId = contents?.id;
    if (!contentsId || !dirtyContents.has(contentsId)) return true;
    if (contents.isDestroyed?.() || parent?.isDestroyed?.()) return true;
    const { response } = await dialog.showMessageBox(parent, {
      type: "warning",
      message: "此 Markdown 文件有未保存的更改。",
      detail: "关闭前是否保存？",
      buttons: ["保存", "不保存", "取消"],
      defaultId: 0,
      cancelId: 2,
      noLink: true,
    });
    if (response === 2) return false;
    if (response === 1) return true;
    safeSend(contents, CHANNELS.command, "save-before-close");
    return false;
  }

  function fitWindow(window) {
    window.setAlwaysOnTop(false);
    const display = screen?.getDisplayMatching?.(window.getBounds());
    const area = display?.workArea;
    if (!area) return;
    const margin = 16;
    const width = Math.min(1280, area.width - margin * 2);
    const height = Math.min(840, area.height - margin * 2);
    window.setBounds({
      x: Math.round(area.x + (area.width - width) / 2),
      y: Math.max(area.y + margin, Math.round(area.y + (area.height - height) / 2)),
      width,
      height,
    });
  }

  function createWindow(targetPath) {
    const window = new BrowserWindow({
      width: 1280,
      height: 840,
      minWidth: 760,
      minHeight: 520,
      show: false,
      title: "澜台 Markdown",
      ...(process.platform === "darwin" ? { titleBarStyle: "hiddenInset" } : {}),
      webPreferences: {
        preload: path.join(electronDir, "markdown-editor", "preload.cjs"),
        contextIsolation: true,
        nodeIntegration: false,
        sandbox: true,
      },
    });
    // Electron destroys webContents before BrowserWindow emits `closed`.
    // Keep the stable id now so teardown never dereferences a destroyed object.
    const contents = window.webContents;
    const contentsId = contents.id;
    pathsByContents.set(contentsId, targetPath);
    contents.setWindowOpenHandler(() => ({ action: "deny" }));
    contents.on("will-navigate", (event) => event.preventDefault());
    window.on("focus", installMarkdownMenu);
    window.once("ready-to-show", () => window.show());
    let allowDestroy = false;
    window.on("close", (event) => {
      if (allowDestroy || !dirtyContents.has(contentsId)) return;
      event.preventDefault();
      void confirmClose(contents, window)
        .then((proceed) => {
          if (proceed && !window.isDestroyed()) {
            allowDestroy = true;
            safeDestroy(window);
          }
        })
        .catch(() => {
          // A close race must never escape into Electron's main process. If
          // either object disappeared while the native dialog was open there
          // is nothing left to confirm.
          if (!window.isDestroyed()) return;
          dirtyContents.delete(contentsId);
          pathsByContents.delete(contentsId);
        });
    });
    window.on("closed", () => {
      pathsByContents.delete(contentsId);
      dirtyContents.delete(contentsId);
    });
    void window.loadFile(path.join(electronDir, "markdown-editor", "index.html"));
    fitWindow(window);
    installMarkdownMenu();
    return window;
  }

  async function openLocalMarkdown(targetInput) {
    const targetPath = typeof targetInput === "string" ? targetInput : targetInput?.path;
    const target = validateTarget(targetPath);
    if (!target.success) return target;
    try {
      createWindow(target.path);
      return { success: true };
    } catch (reason) {
      return resultError(reason instanceof Error ? reason.message : "Markdown 编辑器打开失败", "markdown_open_failed");
    }
  }

  async function openFromIpc(event, targetInput) {
    if (!isTrustedIpcEvent(event)) return resultError("未授权的 Markdown 编辑请求", "markdown_untrusted_sender");
    return await openLocalMarkdown(targetInput);
  }

  function installIpcHandlers() {
    for (const channel of [
      MARKDOWN_EDITOR_CHANNEL,
      CHANNELS.consume,
      CHANNELS.chooseOpen,
      CHANNELS.save,
      CHANNELS.handoff,
    ]) {
      ipcMain.removeHandler(channel);
    }
    ipcMain.handle(MARKDOWN_EDITOR_CHANNEL, openFromIpc);
    ipcMain.handle(CHANNELS.consume, (event) => {
      const targetPath = pathsByContents.get(event.sender.id);
      if (!targetPath) throw new Error("Markdown 路径未授权");
      return { path: targetPath, text: fsImpl.readFileSync(targetPath, "utf8") };
    });
    ipcMain.handle(CHANNELS.chooseOpen, async (event) => {
      if (!pathsByContents.has(event.sender.id)) throw new Error("Markdown 窗口未授权");
      const parent = BrowserWindow.fromWebContents(event.sender);
      const picked = await dialog.showOpenDialog(parent, {
        title: "打开 Markdown",
        filters: [{ name: "Markdown", extensions: ["md", "markdown"] }],
        properties: ["openFile"],
      });
      const selected = picked.filePaths[0];
      if (picked.canceled || !selected) return null;
      const target = validateTarget(selected);
      if (!target.success) throw new Error(target.error);
      pathsByContents.set(event.sender.id, target.path);
      dirtyContents.delete(event.sender.id);
      return { path: target.path, text: fsImpl.readFileSync(target.path, "utf8") };
    });
    ipcMain.handle(CHANNELS.save, async (event, payload = {}) => {
      const currentPath = pathsByContents.get(event.sender.id);
      if (!currentPath) return resultError("Markdown 路径未授权", "markdown_untrusted_sender");
      const text = typeof payload.text === "string" ? payload.text : "";
      if (Buffer.byteLength(text, "utf8") > MARKDOWN_MAX_BYTES) {
        return resultError("Markdown 文件不能超过 100 MB", "markdown_too_large");
      }
      let targetPath = currentPath;
      if (payload.saveAs === true) {
        const parent = BrowserWindow.fromWebContents(event.sender);
        const picked = await dialog.showSaveDialog(parent, {
          title: "Markdown 另存为",
          defaultPath: currentPath,
          filters: [{ name: "Markdown", extensions: ["md", "markdown"] }],
        });
        if (picked.canceled || !picked.filePath) return { success: false, canceled: true };
        targetPath = picked.filePath;
      }
      const temporaryPath = `${targetPath}.lantai-${process.pid}.tmp`;
      fsImpl.writeFileSync(temporaryPath, text, "utf8");
      fsImpl.renameSync(temporaryPath, targetPath);
      pathsByContents.set(event.sender.id, targetPath);
      dirtyContents.delete(event.sender.id);
      return { success: true, path: targetPath };
    });
    ipcMain.handle(CHANNELS.handoff, async (event, payload = {}) => {
      const currentPath = pathsByContents.get(event.sender.id);
      if (!currentPath) {
        return resultError("Markdown 窗口未授权", "markdown_untrusted_sender");
      }
      const mode = ["ask", "suggest", "execute"].includes(payload.mode)
        ? payload.mode
        : "suggest";
      const text = typeof payload.text === "string" ? payload.text.trim() : "";
      if (!text) {
        return resultError("请先输入或选中需要处理的内容", "markdown_handoff_empty");
      }
      if (text.length > MARKDOWN_HANDOFF_MAX_CHARS) {
        return resultError(
          `移交内容不能超过 ${MARKDOWN_HANDOFF_MAX_CHARS.toLocaleString("zh-CN")} 字，请先选中一段`,
          "markdown_handoff_too_large",
        );
      }
      const mainWindow = getMainWindow();
      if (!mainWindow || mainWindow.isDestroyed?.()) {
        return resultError("澜台主窗口尚未就绪", "markdown_main_window_unavailable");
      }
      const envelope = {
        protocol_version: "2.0",
        mode,
        path: currentPath,
        document_name: path.basename(currentPath),
        text,
        selection_only: payload.selectionOnly === true,
      };
      if (!safeSend(mainWindow.webContents, "lantai:markdown-chat-handoff", envelope)) {
        return resultError("无法将 Markdown 任务移交给主对话", "markdown_handoff_failed");
      }
      showMainWindow({ focus: true });
      return { success: true };
    });
    ipcMain.removeAllListeners(CHANNELS.dirty);
    ipcMain.on(CHANNELS.dirty, (event, dirty) => {
      if (!pathsByContents.has(event.sender.id)) return;
      if (dirty === true) dirtyContents.add(event.sender.id);
      else dirtyContents.delete(event.sender.id);
    });
    ipcMain.removeAllListeners(CHANNELS.closeSaved);
    ipcMain.on(CHANNELS.closeSaved, (event, saved) => {
      if (saved !== true || !pathsByContents.has(event.sender.id)) return;
      const window = BrowserWindow.fromWebContents(event.sender);
      safeDestroy(window);
    });
  }

  return { installIpcHandlers, installMarkdownMenu, openFromIpc, openLocalMarkdown, validateTarget };
}

module.exports = {
  CHANNELS,
  MARKDOWN_EDITOR_CHANNEL,
  MARKDOWN_HANDOFF_MAX_CHARS,
  MARKDOWN_MAX_BYTES,
  createMarkdownEditorController,
};
