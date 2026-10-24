const path = require("path");
const { captureCurrentRendererDocument, observeRendererDocument } = require("./rendererDocumentTrust.cjs");
const { normalizeConsoleMessage } = require("./consoleMessageDetails.cjs");
const { redactDiagnosticText } = require("./logRedaction.cjs");
const {
  getWindowStatePath: resolveWindowStatePath,
} = require("./runtimePaths.cjs");
const {
  MIN_WINDOW_BOUNDS,
  WINDOW_STATE_FILE_NAME,
  loadWindowBoundsFromFile,
  saveWindowBoundsToFile,
} = require("./windowState.cjs");

function createAppWindowController({
  app,
  BrowserWindow,
  screen,
  isDev,
  electronDir,
  backendLifecycle,
  appendBackendLog,
  scheduleBackendRestart,
  isQuitting = () => false,
  allowCloseWhenQuitting = false,
  processObject = process,
  devServerUrl = "http://localhost:5173",
}) {
  let mainWindow = null;
  let saveWindowBoundsTimer = null;
  let firstAppLoadPending = true;

  function withProcessColdStartIntent(rawUrl) {
    if (!firstAppLoadPending) {
      return rawUrl;
    }
    const url = new URL(rawUrl);
    url.hash = "/chat?lantaiColdStart=1";
    firstAppLoadPending = false;
    return url.toString();
  }

  function loadMainAppUrl(rawUrl) {
    const window = getMainWindow();
    if (!window) {
      return false;
    }
    window.loadURL(withProcessColdStartIntent(rawUrl));
    return true;
  }

  function escapeHtml(value) {
    return String(value || "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  function buildStartupStatusUrl({ message, detail }) {
    const logPath = path.join(app.getPath("userData"), "backend.log");
    const html = `<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>澜台启动中</title>
  <style>
    :root { color-scheme: light dark; }
    body {
      margin: 0;
      min-height: 100vh;
      display: grid;
      place-items: center;
      background: #f7f8fa;
      color: #1f2329;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }
    .panel {
      width: min(560px, calc(100vw - 48px));
      border: 1px solid #dfe3e8;
      border-radius: 12px;
      background: rgba(255, 255, 255, 0.92);
      box-shadow: 0 18px 60px rgba(15, 23, 42, 0.10);
      padding: 28px;
    }
    h1 {
      margin: 0 0 12px;
      font-size: 22px;
      line-height: 1.35;
      letter-spacing: 0;
    }
    p {
      margin: 0 0 14px;
      color: #5f6673;
      font-size: 14px;
      line-height: 1.75;
    }
    code {
      display: block;
      overflow-wrap: anywhere;
      padding: 10px 12px;
      border-radius: 8px;
      background: #f1f3f5;
      color: #3c4655;
      font-size: 12px;
      line-height: 1.6;
    }
    .muted {
      margin-top: 12px;
      font-size: 12px;
      color: #8a93a3;
    }
    @media (prefers-color-scheme: dark) {
      body { background: #171a1f; color: #f5f7fa; }
      .panel { background: rgba(28, 31, 36, 0.94); border-color: #343941; }
      p { color: #a8b0bd; }
      code { background: #242932; color: #cbd3df; }
      .muted { color: #8e97a5; }
    }
  </style>
</head>
<body>
  <main class="panel">
    <h1>${escapeHtml(message || "澜台正在启动")}</h1>
    <p>${escapeHtml(detail || "后台服务暂时没有响应，澜台会自动重试连接。")}</p>
    <code>${escapeHtml(logPath)}</code>
    <p class="muted">后台服务恢复后会自动重新连接。</p>
  </main>
</body>
</html>`;
    return `data:text/html;charset=utf-8,${encodeURIComponent(html)}`;
  }

  function getWindowStatePath() {
    return resolveWindowStatePath(
      app.getPath("userData"),
      WINDOW_STATE_FILE_NAME,
    );
  }

  function loadWindowBounds() {
    return loadWindowBoundsFromFile({
      filePath: getWindowStatePath(),
      displays: screen.getAllDisplays(),
      onError: (err) => {
        appendBackendLog(
          `[Electron] Failed to read window state: ${err.message}`,
        );
      },
    });
  }

  function getMainWindow() {
    if (mainWindow && mainWindow.isDestroyed()) {
      return null;
    }
    return mainWindow;
  }

  function saveWindowBounds() {
    const window = getMainWindow();
    if (!window) {
      return;
    }
    if (window.isMinimized() || window.isFullScreen()) {
      return;
    }
    saveWindowBoundsToFile({
      filePath: getWindowStatePath(),
      bounds: window.getNormalBounds(),
      onError: (err) => {
        appendBackendLog(
          `[Electron] Failed to write window state: ${err.message}`,
        );
      },
    });
  }

  function scheduleSaveWindowBounds() {
    if (saveWindowBoundsTimer) {
      clearTimeout(saveWindowBoundsTimer);
    }
    saveWindowBoundsTimer = setTimeout(() => {
      saveWindowBoundsTimer = null;
      saveWindowBounds();
    }, 300);
  }

  function clearSaveWindowBoundsTimer() {
    if (saveWindowBoundsTimer) {
      clearTimeout(saveWindowBoundsTimer);
      saveWindowBoundsTimer = null;
    }
  }

  function getWindowMaterialOptions() {
    if (processObject.platform !== "darwin") {
      return {};
    }
    return {
      backgroundColor: "#00000000",
      vibrancy: "sidebar",
      visualEffectState: "active",
    };
  }

  function originOf(rawUrl) {
    try {
      return new URL(String(rawUrl || "")).origin;
    } catch {
      return "";
    }
  }

  function getTrustedOrigins() {
    const origins = new Set();
    const backendOrigin = originOf(backendLifecycle.getApiBaseUrl());
    if (backendOrigin) {
      origins.add(backendOrigin);
    }
    if (isDev) {
      const devOrigin = originOf(devServerUrl);
      if (devOrigin) {
        origins.add(devOrigin);
      }
    }
    return Array.from(origins);
  }

  function isTrustedAppUrl(rawUrl) {
    try {
      const url = new URL(String(rawUrl || ""));
      return getTrustedOrigins().includes(url.origin) &&
        ["/", "/index.html", "/console", "/console/"].includes(url.pathname) &&
        !url.username && !url.password;
    } catch { return false; }
  }

  function preventUntrustedNavigation(event, rawUrl, source) {
    if (isTrustedAppUrl(rawUrl)) {
      return;
    }
    event.preventDefault();
    appendBackendLog(
      `[Electron] Blocked untrusted ${source} navigation: ${rawUrl || "unknown"}`,
    );
  }

  function installNavigationGuards(window) {
    const { webContents } = window;
    observeRendererDocument(webContents);
    webContents.on("will-navigate", (event, rawUrl) => {
      preventUntrustedNavigation(event, rawUrl ?? event.url, "top-level");
    });
    webContents.on("will-redirect", (event, rawUrl) => {
      preventUntrustedNavigation(event, rawUrl ?? event.url, "redirect");
    });
    webContents.on("will-frame-navigate", (event, detailsOrUrl) => {
      const rawUrl =
        detailsOrUrl && typeof detailsOrUrl === "object"
          ? detailsOrUrl.url
          : detailsOrUrl ?? event.url;
      preventUntrustedNavigation(event, rawUrl, "frame");
    });
    if (typeof webContents.setWindowOpenHandler === "function") {
      webContents.setWindowOpenHandler((details = {}) => {
        appendBackendLog(
          `[Electron] Blocked new window navigation: ${details.url || "unknown"}`,
        );
        return { action: "deny" };
      });
    }
  }

  function currentAppDocument(webContents, frame) {
    return captureCurrentRendererDocument(webContents, frame, isTrustedAppUrl);
  }

  function isTrustedIpcEvent(event) {
    const window = getMainWindow();
    return Boolean(window && event?.sender === window.webContents &&
      currentAppDocument(window.webContents, event.senderFrame));
  }

  function isTrustedBackendRequest(details) {
    const window = getMainWindow();
    const webContents = window?.webContents;
    if (!webContents || details?.webContents !== webContents || details.webContentsId !== webContents.id) return false;
    return Boolean(currentAppDocument(webContents, details.frame));
  }

  function createWindow(options = {}) {
    const existingWindow = getMainWindow();
    if (existingWindow) {
      existingWindow.show();
      existingWindow.focus();
      return existingWindow;
    }

    const savedBounds = loadWindowBounds();
    const appTitle = app.getName();
    mainWindow = new BrowserWindow({
      ...savedBounds,
      minWidth: MIN_WINDOW_BOUNDS.width,
      minHeight: MIN_WINDOW_BOUNDS.height,
      title: appTitle,
      titleBarStyle: "hiddenInset",
      show: false,
      ...getWindowMaterialOptions(),
      webPreferences: {
        preload: path.join(electronDir, "preload.js"),
        additionalArguments: [
          `--lantai-api-base-url=${backendLifecycle.getApiBaseUrl()}`,
        ],
        contextIsolation: true,
        nodeIntegration: false,
        sandbox: true,
        webSecurity: !isDev,
      },
    });

    installNavigationGuards(mainWindow);

    mainWindow.webContents.on(
      "did-fail-load",
      (_event, errorCode, errorDescription, validatedURL) => {
        appendBackendLog(
          `[Electron] Window failed to load ${validatedURL}: ${errorCode} ${errorDescription}`,
        );
        const backendUrl = backendLifecycle.getApiBaseUrl();
        if (!isDev && validatedURL && validatedURL.startsWith(backendUrl)) {
          if (validatedURL.includes("lantaiColdStart=1")) {
            firstAppLoadPending = true;
          }
          scheduleBackendRestart(`window failed to load ${validatedURL}`);
          mainWindow.loadURL(
            buildStartupStatusUrl({
              message: "澜台后台服务暂时不可用",
              detail: `${errorCode} ${errorDescription || "连接失败"}`,
            }),
          );
        }
      },
    );

    mainWindow.webContents.on("page-title-updated", (event) => {
      event.preventDefault();
    });

    mainWindow.webContents.on("render-process-gone", (_event, details) => {
      appendBackendLog(
        `[Electron] Renderer process gone: ${JSON.stringify(details)}`,
      );
    });

    mainWindow.webContents.on(
      "console-message",
      (event, legacyLevel, legacyMessage, legacyLine, legacySourceId) => {
        const { level, message, line, sourceId } = normalizeConsoleMessage(
          event,
          legacyLevel,
          legacyMessage,
          legacyLine,
          legacySourceId,
        );
        const redactedMessage = redactDiagnosticText(message, { limit: 500 });
        const redactedSource = redactDiagnosticText(sourceId || "unknown", {
          limit: 200,
        });
        appendBackendLog(
          `[Renderer:${level}] ${redactedMessage} (${redactedSource}:${line || 0})`,
        );
      },
    );

    if (options.deferInitialLoad) {
      // An early in-app document request needs a BrowserWindow object as the
      // Office shell, but must not probe the backend before it has started.
      // The normal boot path loads the main URL once readiness is known.
    } else if (isDev) {
      loadMainAppUrl(devServerUrl);
      mainWindow.webContents.openDevTools();
    } else if (options.startupErrorMessage) {
      mainWindow.loadURL(
        buildStartupStatusUrl({
          message: "澜台后台服务启动较慢",
          detail: options.startupErrorMessage,
        }),
      );
    } else {
      loadMainAppUrl(backendLifecycle.getApiBaseUrl());
    }

    mainWindow.once("ready-to-show", () => {
      const window = getMainWindow();
      if (window && options.showOnReady !== false) {
        window.show();
        window.focus();
      }
    });

    mainWindow.on("move", scheduleSaveWindowBounds);
    mainWindow.on("resize", scheduleSaveWindowBounds);

    mainWindow.on("close", () => {
      saveWindowBounds();
      if (allowCloseWhenQuitting && isQuitting()) {
        return;
      }
    });

    mainWindow.on("closed", () => {
      clearSaveWindowBoundsTimer();
      mainWindow = null;
    });

    return mainWindow;
  }

  function showMainWindow({ focus = false } = {}) {
    const window = getMainWindow();
    if (window) {
      window.show();
      if (focus) {
        window.focus();
      }
      return window;
    }
    return createWindow();
  }

  return {
    createWindow,
    getMainWindow,
    getTrustedOrigins,
    isTrustedIpcEvent,
    isTrustedBackendRequest,
    loadMainAppUrl,
    showMainWindow,
    saveWindowBounds,
    clearSaveWindowBoundsTimer,
  };
}

module.exports = {
  createAppWindowController,
};
