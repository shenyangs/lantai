const {
  app,
  BrowserWindow,
  BrowserView,
  Tray,
  Menu,
  MenuItem,
  nativeImage,
  nativeTheme,
  clipboard,
  session,
  dialog,
  safeStorage,
  ipcMain,
  screen,
  shell,
  powerSaveBlocker,
  Notification,
  systemPreferences,
  desktopCapturer,
  webContents,
} = require("electron");
const path = require("path");
const fs = require("fs");
const {
  createBackendLifecycleController,
  installAppTerminationSignalHandlers,
} = require("./backendLifecycleController.cjs");
const { getDesktopDistributionChannel } = require("./backendProcess.cjs");
const { buildPresentationRuntimeEnv } = require("./runtimePaths.cjs");
const {
  createDesktopSecurityHeaders,
} = require("./desktopSecurityHeaders.cjs");
const { installDesktopIpcHandlers } = require("./desktopIpcController.cjs");
const {
  createWindowMaterialController,
} = require("./windowMaterialController.cjs");
const { createAppWindowController } = require("./appWindowController.cjs");
const {
  createBrowserWorkbenchController,
} = require("./browserWorkbenchController.cjs");
const { createTrayController } = require("./trayController.cjs");
const { createDockIconController } = require("./dockIconController.cjs");
const {
  createWorkspaceGrantBookmarkStore,
} = require("./workspaceGrantBookmarkStore.cjs");
const {
  createSystemBehaviorController,
} = require("./systemBehaviorController.cjs");
const { createDocxEditorController } = require("./docxEditorController.cjs");
const {
  createOfficeEditorController,
} = require("./officeEditorController.cjs");
const {
  createOfficeWorkbenchController,
} = require("./officeWorkbenchController.cjs");
const {
  createMarkdownEditorController,
} = require("./markdownEditorController.cjs");
const {
  createDocumentEntryController,
} = require("./documentEntryController.cjs");
const {
  createLaunchServicesController,
} = require("./launchServicesController.cjs");
const {
  createCustomizationRecoveryController,
} = require("./customizationRecoveryController.cjs");
const { createMacUseController } = require("./macUseController.cjs");

let isQuitting = false;
let windowController = null;
let securityHeaders = null;
let browserWorkbenchController = null;
let workspaceGrantBookmarkStore = null;
let desktopBootFinished = false;
let quitCleanupStarted = false;
let quitCleanupFinished = false;
let systemBehaviorController = null;
let docxEditorController = null;
let officeEditorController = null;
let officeWorkbenchController = null;
let markdownEditorController = null;
let documentEntryController = null;
let launchServicesController = null;
let documentHostPrepared = false;
let customizationRecoveryController = null;
let macUseController = null;

const isDev = !app.isPackaged;
const desktopLifecycleSmokeFile =
  process.env.LANTAI_DESKTOP_LIFECYCLE_SMOKE_FILE || "";
const desktopHeadlessSmoke = process.env.LANTAI_DESKTOP_HEADLESS_SMOKE === "1";
const masBookmarkSmokeFile = process.env.LANTAI_MAS_BOOKMARK_SMOKE_FILE || "";
const macUsePackagedSmokeFile =
  process.env.LANTAI_MAC_USE_PACKAGED_SMOKE_FILE || "";
let desktopLifecycleSmokeSequence = 0;

if (
  (desktopLifecycleSmokeFile ||
    masBookmarkSmokeFile ||
    macUsePackagedSmokeFile) &&
  process.env.LANTAI_DESKTOP_USER_DATA_DIR
) {
  app.setPath(
    "userData",
    path.resolve(process.env.LANTAI_DESKTOP_USER_DATA_DIR),
  );
}

async function runPackagedMacUseSmoke() {
  if (!macUsePackagedSmokeFile) return false;
  const status = await macUseController.execute({
    action: "status",
    run_id: "packaged-mac-use-smoke",
    action_id: "packaged-mac-use-status",
    effect_id: "packaged-mac-use-status",
  });
  const windows = await macUseController.execute({
    action: "list_windows",
    run_id: "packaged-mac-use-smoke",
    action_id: "packaged-mac-use-windows",
    effect_id: "packaged-mac-use-windows",
  });
  const payload = {
    schema: "com.lantai.mac-use-packaged-smoke.v1",
    ok: status.ok === true && windows.ok === true,
    protocol_version: status.protocol_version,
    status: {
      ok: status.ok,
      dispatch_status: status.dispatch_status,
      effect_status: status.effect_status,
      error_code: status.error_code || "",
    },
    windows: {
      ok: windows.ok,
      dispatch_status: windows.dispatch_status,
      effect_status: windows.effect_status,
      error_code: windows.error_code || "",
      count: Array.isArray(windows.data?.windows)
        ? windows.data.windows.length
        : 0,
    },
    helper_path: macUseController.getHelperPath(),
    generated_at: new Date().toISOString(),
  };
  fs.mkdirSync(path.dirname(macUsePackagedSmokeFile), { recursive: true });
  const temporary = `${macUsePackagedSmokeFile}.tmp-${process.pid}`;
  fs.writeFileSync(temporary, `${JSON.stringify(payload, null, 2)}\n`, {
    mode: 0o600,
  });
  fs.renameSync(temporary, macUsePackagedSmokeFile);
  await macUseController.stopBridgeServer();
  desktopBootFinished = true;
  app.exit(payload.ok ? 0 : 1);
  return true;
}

const gotSingleInstanceLock = app.requestSingleInstanceLock();
if (!gotSingleInstanceLock) {
  app.quit();
  process.exit(0);
}

function appendBackendLog(message) {
  try {
    const logPath = path.join(app.getPath("userData"), "backend.log");
    fs.appendFileSync(logPath, `${new Date().toISOString()} ${message}\n`);
  } catch (err) {
    console.error(`[Electron] Failed to write backend log: ${err.message}`);
  }
}

function writeDesktopLifecycleSmokeState(state, eventName = "backend_ready") {
  if (!desktopLifecycleSmokeFile) {
    return;
  }

  try {
    desktopLifecycleSmokeSequence += 1;
    const payload = {
      event: eventName,
      sequence: desktopLifecycleSmokeSequence,
      written_at: new Date().toISOString(),
      is_packaged: app.isPackaged,
      distribution_channel: getDesktopDistributionChannel() || "official",
      api_base_url: state.apiBaseUrl,
      backend_port: state.backendPort,
      backend_pid: state.backendPid,
      backend_restart_count: state.backendRestartCount,
      desktop_token: state.desktopToken,
      desktop_instance_nonce: state.desktopInstanceNonce,
      has_backend_process: state.hasBackendProcess,
    };
    fs.mkdirSync(path.dirname(desktopLifecycleSmokeFile), { recursive: true });
    const tmpPath = `${desktopLifecycleSmokeFile}.tmp`;
    fs.writeFileSync(tmpPath, `${JSON.stringify(payload, null, 2)}\n`, {
      mode: 0o600,
    });
    fs.renameSync(tmpPath, desktopLifecycleSmokeFile);
  } catch (err) {
    appendBackendLog(
      `[Electron] Failed to write desktop lifecycle smoke state: ${err.message}`,
    );
  }
}

function getMainWindow() {
  return windowController ? windowController.getMainWindow() : null;
}

function createWindow(options = {}) {
  const window = windowController.createWindow(options);
  const markReadyAfterRealPage = () => {
    const loadedUrl = window.webContents.getURL?.() || "";
    if (/^https?:\/\//.test(loadedUrl)) {
      customizationRecoveryController?.markRendererReady();
      try {
        const origin = new URL(loadedUrl).origin;
        void fetch(`${origin}/api/customization/boot-health/accept`, {
          method: "POST",
          headers: {
            "X-Lantai-Desktop-Token": backendLifecycle.getDesktopToken(),
          },
        }).catch(() => {});
      } catch {
        // Boot acceptance is best effort; incomplete state remains recoverable.
      }
      window.webContents.removeListener?.(
        "did-finish-load",
        markReadyAfterRealPage,
      );
    }
  };
  window?.webContents?.on?.("did-finish-load", markReadyAfterRealPage);
  return window;
}

customizationRecoveryController = createCustomizationRecoveryController({
  app,
  dialog,
  MenuItem,
  getUserDataPath: () => app.getPath("userData"),
  getMainWindow,
  appendLog: appendBackendLog,
});
customizationRecoveryController.beginBoot();

const backendLifecycle = createBackendLifecycleController({
  isDev,
  electronDir: __dirname,
  getResourcesPath: () => process.resourcesPath,
  getUserDataPath: () => app.getPath("userData"),
  getBaseEnv: () => ({
    ...process.env,
    ...buildPresentationRuntimeEnv({
      isDev,
      electronDir: __dirname,
      resourcesPath: process.resourcesPath,
      executablePath: process.execPath,
    }),
    ...customizationRecoveryController.buildBackendEnv(),
    LANTAI_BROWSER_BRIDGE_URL:
      browserWorkbenchController?.getBridgeBaseUrl() || "",
    LANTAI_BROWSER_BRIDGE_TOKEN:
      browserWorkbenchController?.getBridgeToken() || "",
    LANTAI_MAC_USE_BRIDGE_URL: macUseController?.getBridgeBaseUrl() || "",
    LANTAI_MAC_USE_BRIDGE_TOKEN: macUseController?.getBridgeToken() || "",
  }),
  appendBackendLog,
  consoleObject: console,
  isQuitting: () => isQuitting,
  onBackendReady: (state) => {
    customizationRecoveryController.markBackendReady();
    writeDesktopLifecycleSmokeState(state);
  },
  ensureDesktopApiHeaderInjection: () => {
    if (securityHeaders) {
      securityHeaders.install();
    }
  },
  reloadWindowAfterBackendRestart: (url) => {
    const mainWindow = getMainWindow();
    if (mainWindow && !mainWindow.isDestroyed()) {
      appendBackendLog(
        `[Electron] Reloading window after backend restart: ${url}`,
      );
      windowController.loadMainAppUrl(url);
    }
  },
});

securityHeaders = createDesktopSecurityHeaders({
  session,
  backendLifecycle,
  isTrustedBackendRequest: (details) => {
    const sender = details.webContents || webContents.fromId(details.webContentsId);
    if (!sender || sender.session !== session.defaultSession) return false;
    const request = { ...details, webContents: sender };
    return Boolean(windowController?.isTrustedBackendRequest(request) ||
      officeEditorController?.isTrustedBackendRequest(request));
  },
});

windowController = createAppWindowController({
  app,
  BrowserWindow,
  screen,
  isDev,
  electronDir: __dirname,
  backendLifecycle,
  appendBackendLog,
  scheduleBackendRestart: (reason) => backendLifecycle.scheduleRestart(reason),
  isQuitting: () => isQuitting,
  allowCloseWhenQuitting: true,
});

const windowMaterialController = createWindowMaterialController({
  ipcMain,
  nativeTheme,
  getMainWindow,
  isTrustedIpcEvent: (event) => windowController.isTrustedIpcEvent(event),
  appendLog: appendBackendLog,
});

officeWorkbenchController = createOfficeWorkbenchController({
  ipcMain,
  getMainWindow,
  isTrustedIpcEvent: (event) => windowController.isTrustedIpcEvent(event),
});

docxEditorController = createDocxEditorController({
  ipcMain,
  isDev,
  electronDir: __dirname,
  resourcesPath: process.resourcesPath,
  appendLog: appendBackendLog,
  getMainWindow,
  getApiBaseUrl: () => backendLifecycle.getApiBaseUrl(),
  installOfficeAiHandlers: () =>
    officeEditorController?.installOfficeAiHandlers(),
  registerOfficeAiEditorWindow: (editorWindow, context) =>
    officeEditorController?.registerEditorWindow(editorWindow, context),
  resolveOfficeModelBinding: (agentId) =>
    officeEditorController?.resolveModelBinding(agentId),
  presentOfficeView: (options) => officeWorkbenchController.open(options),
  isTrustedIpcEvent: (event) => windowController.isTrustedIpcEvent(event),
});

officeEditorController = createOfficeEditorController({
  ipcMain,
  screen,
  isDev,
  electronDir: __dirname,
  resourcesPath: process.resourcesPath,
  appendLog: appendBackendLog,
  getMainWindow,
  getApiBaseUrl: () => backendLifecycle.getApiBaseUrl(),
  getDesktopToken: () => backendLifecycle.getDesktopToken(),
  presentOfficeView: (options) => officeWorkbenchController.open(options),
  isTrustedIpcEvent: (event) => windowController.isTrustedIpcEvent(event),
});

macUseController = createMacUseController({
  ipcMain,
  isDev,
  electronDir: __dirname,
  resourcesPath: process.resourcesPath,
  userDataPath: app.getPath("userData"),
  appendLog: appendBackendLog,
  desktopCapturer,
  getScreenRecordingStatus: () =>
    systemPreferences.getMediaAccessStatus("screen"),
  isTrustedIpcEvent: (event) => windowController.isTrustedIpcEvent(event),
});

markdownEditorController = createMarkdownEditorController({
  BrowserWindow,
  Menu,
  dialog,
  ipcMain,
  screen,
  electronDir: __dirname,
  getMainWindow,
  showMainWindow: (options) => windowController.showMainWindow(options),
  isTrustedIpcEvent: (event) => windowController.isTrustedIpcEvent(event),
});

documentEntryController = createDocumentEntryController({
  app,
  Menu,
  dialog,
  getMainWindow,
  showMainWindow: (options) => {
    const mainWindow = getMainWindow();
    if (mainWindow && !mainWindow.isDestroyed()) {
      windowController.showMainWindow(options);
    } else if (desktopBootFinished) {
      createWindow();
    }
  },
  openDocument: (target) =>
    /\.docx$/i.test(String(target?.path || ""))
      ? docxEditorController.openLocalDocx(target)
      : /\.(md|markdown)$/i.test(String(target?.path || ""))
        ? markdownEditorController.openLocalMarkdown(target)
        : officeEditorController.openLocalOffice(target),
  ensureDocumentHost: () => {
    const existing = getMainWindow();
    if (existing && !existing.isDestroyed()) return existing;
    documentHostPrepared = true;
    return createWindow({ showOnReady: false, deferInitialLoad: true });
  },
  appendLog: appendBackendLog,
  showOpenError: async ({ path: targetPath, error }) => {
    const mainWindow = getMainWindow();
    const options = {
      type: "error",
      title: "无法打开文档",
      message: `澜台无法打开“${path.basename(targetPath)}”`,
      detail: String(error || "本地编辑器打开失败"),
      buttons: ["好"],
    };
    if (mainWindow && !mainWindow.isDestroyed()) {
      await dialog.showMessageBox(mainWindow, options);
    } else {
      await dialog.showMessageBox(options);
    }
  },
});
documentEntryController.installEventHandlers(process.argv);
app.on("browser-window-focus", (_event, focusedWindow) => {
  if (focusedWindow === getMainWindow()) {
    documentEntryController.installApplicationMenu();
    customizationRecoveryController.decorateApplicationMenu(Menu);
  }
});

browserWorkbenchController = createBrowserWorkbenchController({
  app,
  BrowserWindow,
  BrowserView,
  ipcMain,
  session,
  dialog,
  safeStorage,
  electronDir: __dirname,
  appendBackendLog,
  isTrustedIpcEvent: (event) => windowController.isTrustedIpcEvent(event),
  getMainWindow,
  distributionChannel: getDesktopDistributionChannel(),
});
workspaceGrantBookmarkStore = createWorkspaceGrantBookmarkStore({
  app,
  safeStorage,
  appendLog: appendBackendLog,
});

const trayController = createTrayController({
  app,
  Tray,
  Menu,
  nativeImage,
  isDev,
  electronDir: __dirname,
  getResourcesPath: () => process.resourcesPath,
  getMainWindow,
  createWindow,
  getRecoveryMenuItems: () => [
    { type: "separator" },
    ...customizationRecoveryController.recoveryTemplateItems(),
  ],
});
const dockIconController = createDockIconController({
  app,
  nativeImage,
  isDev,
  electronDir: __dirname,
  getResourcesPath: () => process.resourcesPath,
  appendLog: appendBackendLog,
});

app.whenReady().then(() => {
  const boot = async () => {
    dockIconController.installIpcHandlers({
      ipcMain,
      dialog,
      getMainWindow,
      isTrustedIpcEvent: (event) => windowController.isTrustedIpcEvent(event),
    });
    dockIconController.applyDockIcon();
    documentEntryController.installApplicationMenu();
    customizationRecoveryController.decorateApplicationMenu(Menu);
    const bookmarkRestore = workspaceGrantBookmarkStore.restoreAll();
    if (!bookmarkRestore.ok && bookmarkRestore.stale) {
      appendBackendLog(
        `[Workspace Grants] ${bookmarkRestore.stale} bookmark(s) require reauthorization.`,
      );
    }
    systemBehaviorController = createSystemBehaviorController({
      app,
      powerSaveBlocker,
      Notification,
      getMainWindow,
    });
    launchServicesController = createLaunchServicesController({
      app,
      ipcMain,
      isTrustedIpcEvent: (event) => windowController.isTrustedIpcEvent(event),
    });
    launchServicesController.installIpcHandlers();
    installDesktopIpcHandlers({
      app,
      ipcMain,
      dialog,
      shell,
      clipboard,
      nativeImage,
      getMainWindow,
      getAllowedOrigins: () => windowController.getTrustedOrigins(),
      isTrustedIpcEvent: (event) => windowController.isTrustedIpcEvent(event),
      distributionChannel: getDesktopDistributionChannel() || "official",
      getApiBaseUrl: () => backendLifecycle.getApiBaseUrl(),
      getDesktopToken: () => backendLifecycle.getDesktopToken(),
      getBackendWorkingDir: () => backendLifecycle.getBackendWorkingDir(),
      workspaceBookmarkStore: workspaceGrantBookmarkStore,
      systemBehaviorController,
      systemPreferences,
      BrowserWindow,
    });
    macUseController.installIpcHandlers();
    docxEditorController.installIpcHandlers();
    officeEditorController.installIpcHandlers();
    officeWorkbenchController.installIpcHandlers();
    windowMaterialController.installIpcHandlers();
    markdownEditorController.installIpcHandlers();
    browserWorkbenchController.installIpcHandlers();
    // Opening a local Office/Markdown file from inside Lantai does not depend
    // on the Python API. Mark the in-app document picker ready as soon as the
    // editor IPC contracts are installed; AI actions can connect when the
    // backend becomes ready.
    await documentEntryController.markReady();
    await browserWorkbenchController.startBridgeServer();
    await macUseController.startBridgeServer();
    if (await runPackagedMacUseSmoke()) return;
    await backendLifecycle.start();
    trayController.createTray();

    const ready = await backendLifecycle.waitForReady(isDev ? 5000 : 30000);
    if (!ready) {
      console.log(
        "[Electron] Backend health check timed out, showing window anyway",
      );
      appendBackendLog(
        "[Electron] Backend health check timed out, showing window anyway",
      );
    }
    const preparedDocumentHost = documentHostPrepared ? getMainWindow() : null;
    if (preparedDocumentHost && !preparedDocumentHost.isDestroyed()) {
      // The hidden shell let an early in-app open request start the editor
      // without waiting for the backend. Hydrate that same main window now.
      windowController.loadMainAppUrl(backendLifecycle.getApiBaseUrl());
      if (!desktopHeadlessSmoke) {
        windowController.showMainWindow({ focus: false });
      }
      documentHostPrepared = false;
    } else {
      createWindow({
        ...(ready
          ? {}
          : {
              startupErrorMessage:
                "后台服务仍在启动，首次运行可能需要更长时间；完成后会自动进入。",
            }),
        ...(desktopHeadlessSmoke ? { showOnReady: false } : {}),
      });
    }
    if (!ready) {
      void backendLifecycle.handleReadinessTimeout(
        "initial backend readiness timed out",
      );
    }
    desktopBootFinished = true;
  };
  boot().catch((err) => {
    const message = `[Electron] Boot failed: ${err.message}`;
    console.error(message);
    appendBackendLog(message);
    desktopBootFinished = true;
    createWindow({ startupErrorMessage: message });
    void documentEntryController.markReady();
  });

  app.on("activate", () => {
    if (getMainWindow()) {
      windowController.showMainWindow();
    } else if (desktopBootFinished) {
      createWindow();
    }
  });
});

const removeProcessTerminationSignalHandlers =
  installAppTerminationSignalHandlers({
    requestQuit: () => app.quit(),
  });
app.once("will-quit", removeProcessTerminationSignalHandlers);

app.on("window-all-closed", () => {
  if (process.platform !== "darwin") {
    isQuitting = true;
    app.quit();
  }
});

app.on("before-quit", (event) => {
  isQuitting = true;
  customizationRecoveryController?.markIntentionalExit();
  backendLifecycle.clearRestartTimer();
  const mainWindow = getMainWindow();
  if (mainWindow) {
    windowController.saveWindowBounds();
    mainWindow.removeAllListeners("close");
  }
  if (quitCleanupFinished) {
    return;
  }

  event.preventDefault();
  if (quitCleanupStarted) {
    return;
  }
  quitCleanupStarted = true;
  const reportUnsettledQuit = () => {
    quitCleanupStarted = false;
    appendBackendLog("[Electron] Quit cleanup did not settle; keeping the application open for retry");
    dialog.showErrorBox(
      "澜台暂时无法退出",
      "部分后台任务尚未确认结束。请稍后再次退出；本次退出没有完成。",
    );
  };
  Promise.allSettled([
    Promise.resolve().then(() => backendLifecycle.stopBackendProcess()),
    Promise.resolve().then(() => browserWorkbenchController?.stopBridgeServer()),
    Promise.resolve().then(() => macUseController?.stopBridgeServer()),
  ]).then((results) => {
    if (results.some((result) => result.status === "rejected")) {
      reportUnsettledQuit();
      return;
    }
    officeWorkbenchController?.dispose();
    systemBehaviorController?.dispose();
    workspaceGrantBookmarkStore?.closeAll();
    quitCleanupFinished = true;
    app.quit();
  }).catch(reportUnsettledQuit);
});

process.on("uncaughtException", (err) => {
  const message = `[Electron] Uncaught exception: ${err?.stack || err?.message || String(err)}`;
  console.error(message);
  appendBackendLog(message);
});

process.on("unhandledRejection", (reason) => {
  const message = `[Electron] Unhandled rejection: ${
    reason instanceof Error ? reason.stack || reason.message : String(reason)
  }`;
  console.error(message);
  appendBackendLog(message);
});
