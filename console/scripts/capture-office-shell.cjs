const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { app, BrowserWindow, ipcMain } = require("electron");
const { resolveOfficeEditorRuntimePaths } = require("../electron/officeEditorController.cjs");

const editor = process.argv[2];
const outputPath = process.argv[3];
const documentPath = process.argv[4] ? path.resolve(process.argv[4]) : undefined;
const supported = new Set(["docs", "sheets", "slides", "pdf"]);
const expectedCapabilities = documentPath
  ? { docs: 4, sheets: 2, slides: 3, pdf: 2 }[editor]
  : 0;

if (!supported.has(editor) || !outputPath) {
  console.error(
    "Usage: electron scripts/capture-office-shell.cjs <docs|sheets|slides|pdf> <output.png> [document]",
  );
  app.exit(2);
}

const modulePath = path.resolve(
  __dirname,
  "..",
  "vendor",
  "lantai-docx-editor",
  "apps",
  editor,
  "out",
  "main",
  "lantai.js",
);
const runtime = require(modulePath);
const editorRoot = path.dirname(path.dirname(path.dirname(modulePath)));
const config = {
  docs: {
    register: "registerDocsIpc",
    menu: "installDocsMenu",
    create: "createDocsWindow",
  },
  sheets: { menu: "installSheetsMenu", create: "createSheetsWindow" },
  slides: {
    register: "registerSlidesIpc",
    menu: "installSlidesMenu",
    create: "createSlidesWindow",
  },
  pdf: { menu: "installPdfMenu", create: "createPdfWindow" },
}[editor];

app.setPath("userData", path.join(os.tmpdir(), `lantai-office-shell-${editor}-${process.pid}`));

const timeout = setTimeout(() => {
  console.error(`Timed out while capturing ${editor}`);
  app.exit(1);
}, 20_000);

app.whenReady().then(async () => {
  const providers = Object.fromEntries(
    ["genspark", "anthropic", "gemini", "deepseek", "openai", "custom"].map((id) => [
      id,
      { apiKey: "", model: "" },
    ]),
  );
  ipcMain.handle("ai:get-settings", () => ({ provider: "genspark", providers }));
  ipcMain.handle("ai:gsk-status", () => ({ loggedIn: true }));
  ipcMain.handle("project:resolveChat", () => ({ projectId: "default", chatId: "visual-smoke" }));
  ipcMain.handle("project:loadChat", () => []);
  ipcMain.handle("project:appendChat", () => null);
  ipcMain.handle("project:rebindChat", () => ({ projectId: "default", chatId: "visual-smoke" }));

  const paths = {
    preloadPath: path.join(editorRoot, "out", "preload", "index.js"),
    rendererFile: path.join(editorRoot, "out", "renderer", "index.html"),
  };
  if (editor === "docs") runtime.configureDocsRuntime(paths);
  else if (editor === "sheets") {
    const { sidecarPath } = resolveOfficeEditorRuntimePaths({
      isDev: true,
      electronDir: path.resolve(__dirname, "..", "electron"),
      resourcesPath: process.resourcesPath,
      platform: process.platform,
      arch: process.arch,
    }).sheets;
    fs.accessSync(sidecarPath, fs.constants.X_OK);
    runtime.configureSheetsRuntime({
      ...paths,
      sidecarPath,
    });
  }
  else if (editor === "slides") {
    runtime.configureSlidesRuntime({
      preloadPath: paths.preloadPath,
      rendererFilePath: paths.rendererFile,
    });
  } else {
    runtime.configurePdfRuntime({
      ...paths,
      visionOcrPath: path.join(
        editorRoot,
        "native-bin",
        `${process.platform}-${process.arch}`,
        "lantai-pdf-vision",
      ),
    });
  }

  if (config.register) runtime[config.register]();
  runtime[config.menu]?.();
  if (editor === "sheets" && documentPath) runtime.setForcedWorkbookPath(documentPath);
  const created =
    editor === "sheets" ? runtime[config.create]() : runtime[config.create](documentPath);
  const win =
    created && typeof created.then !== "function"
      ? created
      : await new Promise((resolve) => {
          const poll = () => {
            const candidate = BrowserWindow.getAllWindows()[0];
            if (candidate) resolve(candidate);
            else setTimeout(poll, 25);
          };
          poll();
        });
  if (created && typeof created.then === "function") created.catch(() => undefined);

  win.webContents.on("console-message", (_event, level, message, line, sourceId) => {
    if (level >= 2) console.error(`[${editor}:renderer:${level}] ${message} (${sourceId}:${line})`);
  });
  win.webContents.on("did-fail-load", (_event, code, description, url) => {
    console.error(`[${editor}:did-fail-load] ${code} ${description} ${url}`);
  });
  win.webContents.on("render-process-gone", (_event, details) => {
    console.error(`[${editor}:render-process-gone] ${JSON.stringify(details)}`);
  });

  // Visual comparisons are only meaningful when every editor is captured at
  // the same CSS viewport. Native editor defaults intentionally differ.
  win.setContentSize(1440, 900);

  const capture = async () => {
    try {
      await new Promise((resolve) => setTimeout(resolve, 900));
      if (editor === "pdf" && documentPath) {
        const deadline = Date.now() + 10_000;
        while (Date.now() < deadline) {
          const ready = await win.webContents.executeJavaScript(
            `document.querySelectorAll('.pdf-page').length >= 2 && document.querySelectorAll('.pdf-thumb').length >= 2`,
          );
          if (ready) break;
          await new Promise((resolve) => setTimeout(resolve, 160));
        }
      }
      await win.webContents.executeJavaScript(
        `(() => {
          const entry = document.querySelector('.lt-office-agent-entry');
          if (entry?.getAttribute('aria-pressed') !== 'true') entry?.click();
        })()`,
      );
      const capabilityDeadline = Date.now() + 5_000;
      while (Date.now() < capabilityDeadline) {
        const ready = await win.webContents.executeJavaScript(
          `document.querySelectorAll('.lt-office-agent-capability').length === ${expectedCapabilities}`,
        );
        if (ready) break;
        await new Promise((resolve) => setTimeout(resolve, 120));
      }
      const panelDeadline = Date.now() + 5_000;
      while (Date.now() < panelDeadline) {
        const ready = await win.webContents.executeJavaScript(`(() => {
          const dock = document.querySelector('.ai-dock');
          const panel = dock
            ? (dock.classList.contains('collapsed')
                ? null
                : dock.querySelector(':scope > .ai-panel, :scope > .copilot'))
            : document.querySelector('.app-shell:not(.copilot-collapsed) .copilot:not(.collapsed)');
          const rect = panel?.getBoundingClientRect();
          const width = rect
            ? Math.max(0, Math.min(rect.right, window.innerWidth) - Math.max(rect.left, 0))
            : 0;
          return document.querySelector('.lt-office-agent-entry')?.getAttribute('aria-pressed') === 'true'
            && width >= 280;
        })()`);
        if (ready) break;
        await new Promise((resolve) => setTimeout(resolve, 120));
      }
      await win.webContents.executeJavaScript(
        `(() => {
          const textarea = document.querySelector('.ai-input-box textarea');
          textarea?.focus();
          return new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)));
        })()`,
      );
      await new Promise((resolve) => setTimeout(resolve, 250));
      const metrics = await win.webContents.executeJavaScript(`({
        shell: document.documentElement.dataset.lantaiOfficeShell,
        app: document.documentElement.dataset.officeApp,
        panel: (() => {
          const dock = document.querySelector('.ai-dock');
          if (dock) return !dock.classList.contains('collapsed') && !!dock.querySelector(':scope > .ai-panel, :scope > .copilot');
          return !!document.querySelector('.app-shell:not(.copilot-collapsed) .copilot:not(.collapsed)');
        })(),
        focusRuleReady: CSS.supports('selector(:focus-visible)'),
        agentEntries: document.querySelectorAll('.lt-office-agent-entry').length,
        agentEntryPressed: document.querySelector('.lt-office-agent-entry')?.getAttribute('aria-pressed'),
        agentAvatars: document.querySelectorAll('.lt-office-agent-avatar').length,
        agentCapabilities: document.querySelectorAll('.lt-office-agent-capability').length,
        fileTitle: document.querySelector('.lt-office-file-title')?.textContent?.trim(),
        entryRect: (() => {
          const rect = document.querySelector('.lt-office-agent-entry')?.getBoundingClientRect();
          return rect ? { left: rect.left, right: rect.right, width: rect.width } : null;
        })(),
        ribbonRect: (() => {
          const rect = document.querySelector('.ribbon-tabs')?.getBoundingClientRect();
          return rect ? { left: rect.left, right: rect.right, width: rect.width } : null;
        })(),
        ribbonMetrics: (() => {
          const tabs = document.querySelector('.ribbon-tabs');
          const command = document.documentElement.dataset.officeApp === 'sheets'
            ? document.querySelector('.excel-header > .ribbon')
            : document.querySelector('.ribbon-body');
          const tabLabel = tabs?.querySelector(
            '.ribbon-tab:not(.ribbon-tab-file), .ribbon-tab-file, button:not(.qa-btn):not(.lt-office-agent-entry)',
          );
          const commandLabel = command?.querySelector('.rb-big, .rb-small, .ribbon-tool');
          const groupLabel = command?.querySelector('.ribbon-group-label');
          const rect = (node) => node?.getBoundingClientRect();
          const style = (node) => node ? getComputedStyle(node) : null;
          return {
            tabRowHeight: rect(tabs)?.height ?? 0,
            commandHeight: rect(command)?.height ?? 0,
            tabFontSize: style(tabLabel)?.fontSize ?? null,
            tabLineHeight: style(tabLabel)?.lineHeight ?? null,
            commandFontSize: style(commandLabel)?.fontSize ?? null,
            commandLineHeight: style(commandLabel)?.lineHeight ?? null,
            groupFontSize: style(groupLabel)?.fontSize ?? null,
          };
        })(),
        visibleLegacyAiEntries: [...document.querySelectorAll('.ai-entry')].filter((node) => node.getClientRects().length > 0).length,
        visiblePanelWidth: (() => {
          const dock = document.querySelector('.ai-dock');
          const panel = dock
            ? (dock.classList.contains('collapsed')
                ? null
                : dock.querySelector(':scope > .ai-panel, :scope > .copilot'))
            : document.querySelector('.app-shell:not(.copilot-collapsed) .copilot:not(.collapsed)');
          if (!panel) return 0;
          const rect = panel.getBoundingClientRect();
          return Math.max(0, Math.min(rect.right, window.innerWidth) - Math.max(rect.left, 0));
        })(),
        panelRect: (() => {
          const dock = document.querySelector('.ai-dock');
          const panel = dock
            ? (dock.classList.contains('collapsed')
                ? null
                : dock.querySelector(':scope > .ai-panel, :scope > .copilot'))
            : document.querySelector('.app-shell:not(.copilot-collapsed) .copilot:not(.collapsed)');
          const rect = panel?.getBoundingClientRect();
          return rect ? { left: rect.left, right: rect.right, width: rect.width } : null;
        })(),
        panelAvatarRect: (() => {
          const rect = document.querySelector('.ai-panel-header .lt-office-agent-avatar')?.getBoundingClientRect();
          return rect ? { left: rect.left, right: rect.right, width: rect.width } : null;
        })(),
        panelStyle: (() => {
          const dock = document.querySelector('.ai-dock');
          const panel = dock
            ? (dock.classList.contains('collapsed')
                ? null
                : dock.querySelector(':scope > .ai-panel, :scope > .copilot'))
            : document.querySelector('.app-shell:not(.copilot-collapsed) .copilot:not(.collapsed)');
          if (!panel) return null;
          const style = getComputedStyle(panel);
          return {
            display: style.display,
            visibility: style.visibility,
            opacity: style.opacity,
            transform: style.transform,
          };
        })(),
        dockRect: (() => {
          const rect = document.querySelector('.ai-dock')?.getBoundingClientRect();
          return rect ? { left: rect.left, right: rect.right, width: rect.width } : null;
        })(),
        viewportWidth: window.innerWidth,
        viewportHeight: window.innerHeight,
        composerFocus: (() => {
          const textarea = document.querySelector('.ai-input-box textarea');
          const inputBox = document.querySelector('.ai-input-box');
          const composer = document.querySelector('.ai-composer');
          const status = document.querySelector('.status-bar');
          const rect = (node) => {
            const value = node?.getBoundingClientRect();
            return value ? { top: value.top, bottom: value.bottom, height: value.height } : null;
          };
          const textareaStyle = textarea ? getComputedStyle(textarea) : null;
          const boxStyle = inputBox ? getComputedStyle(inputBox) : null;
          return {
            active: document.activeElement === textarea,
            textareaOutlineWidth: textareaStyle?.outlineWidth ?? null,
            textareaOutlineStyle: textareaStyle?.outlineStyle ?? null,
            inputBoxShadow: boxStyle?.boxShadow ?? null,
            inputBox: rect(inputBox),
            composer: rect(composer),
            status: rect(status),
          };
        })(),
        pdfPages: document.querySelectorAll('.pdf-page').length,
        pdfCanvases: document.querySelectorAll('.pdf-page canvas').length,
        pdfThumbnails: document.querySelectorAll('.pdf-thumb').length
      })`);
      if (
        metrics.agentEntries !== 1 ||
        metrics.agentEntryPressed !== "true" ||
        metrics.agentAvatars < 2 ||
        metrics.visibleLegacyAiEntries !== 0
      ) {
        throw new Error(`Office entry contract failed: ${JSON.stringify(metrics)}`);
      }
      if (metrics.visiblePanelWidth < 280) {
        throw new Error(`Office panel is not visibly open: ${JSON.stringify(metrics)}`);
      }
      if (
        !metrics.panelRect ||
        !metrics.panelAvatarRect ||
        metrics.panelAvatarRect.left < metrics.panelRect.left + 10 ||
        metrics.panelAvatarRect.right > metrics.panelRect.right
      ) {
        throw new Error(`Office panel avatar is clipped: ${JSON.stringify(metrics)}`);
      }
      if (!metrics.fileTitle) {
        throw new Error(`Office file title is missing: ${JSON.stringify(metrics)}`);
      }
      if (!metrics.entryRect || metrics.viewportWidth - metrics.entryRect.right > 24) {
        throw new Error(`Office Agent entry is not at the window's right edge: ${JSON.stringify(metrics)}`);
      }
      if (metrics.viewportWidth !== 1440 || metrics.viewportHeight !== 900) {
        throw new Error(`Office capture viewport is not standardised: ${JSON.stringify(metrics)}`);
      }
      if (
        Math.abs(metrics.ribbonMetrics.tabRowHeight - 35) > 0.5 ||
        Math.abs(metrics.ribbonMetrics.commandHeight - 80) > 0.5 ||
        metrics.ribbonMetrics.tabFontSize !== '13px' ||
        (editor === 'pdf'
          ? metrics.ribbonMetrics.commandFontSize !== '0px'
          : metrics.ribbonMetrics.commandFontSize !== '12px')
      ) {
        throw new Error(`Office Ribbon metrics are inconsistent: ${JSON.stringify(metrics)}`);
      }
      if (metrics.agentCapabilities !== expectedCapabilities) {
        throw new Error(`Office capability contract failed: ${JSON.stringify(metrics)}`);
      }
      if (
        !metrics.composerFocus.active ||
        (metrics.composerFocus.textareaOutlineStyle !== "none" &&
          metrics.composerFocus.textareaOutlineWidth !== "0px") ||
        !metrics.composerFocus.inputBoxShadow ||
        metrics.composerFocus.inputBoxShadow === "none"
      ) {
        throw new Error(`Office composer focus contract failed: ${JSON.stringify(metrics)}`);
      }
      if (
        metrics.composerFocus.status &&
        metrics.composerFocus.composer &&
        metrics.composerFocus.composer.bottom > metrics.composerFocus.status.top + 0.5
      ) {
        throw new Error(`Office composer overlaps the status bar: ${JSON.stringify(metrics)}`);
      }
      if (
        !metrics.panelStyle ||
        metrics.panelStyle.display === 'none' ||
        metrics.panelStyle.visibility === 'hidden' ||
        Number(metrics.panelStyle.opacity) < 0.9
      ) {
        throw new Error(`Office panel has a hidden computed style: ${JSON.stringify(metrics)}`);
      }
      if (
        editor === "pdf" &&
        documentPath &&
        (!metrics.panel ||
          metrics.pdfPages < 2 ||
          metrics.pdfCanvases < 1 ||
          metrics.pdfThumbnails < 2)
      ) {
        throw new Error(`PDF document health check failed: ${JSON.stringify(metrics)}`);
      }
      const image = await win.webContents.capturePage();
      fs.mkdirSync(path.dirname(path.resolve(outputPath)), { recursive: true });
      fs.writeFileSync(path.resolve(outputPath), image.toPNG());
      console.log(`OFFICE_SHELL_CAPTURE=${JSON.stringify(metrics)} ${path.resolve(outputPath)}`);
      clearTimeout(timeout);
      app.quit();
    } catch (error) {
      console.error(error?.stack || error);
      clearTimeout(timeout);
      app.exit(1);
    }
  };

  if (win.webContents.isLoading()) win.webContents.once("did-finish-load", capture);
  else void capture();
}).catch((error) => {
  console.error(error?.stack || error);
  clearTimeout(timeout);
  app.exit(1);
});
