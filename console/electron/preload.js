const { contextBridge, ipcRenderer, webUtils } = require("electron");

const apiBaseUrlArg = process.argv.find((arg) =>
  arg.startsWith("--lantai-api-base-url="),
);
const apiBaseUrl = apiBaseUrlArg
  ? apiBaseUrlArg.slice("--lantai-api-base-url=".length)
  : "";

contextBridge.exposeInMainWorld("electronAPI", {
  setWindowMaterial: (options) =>
    ipcRenderer.invoke("window-material:set", options),
  onWindowMaterialChanged: (callback) => {
    const listener = (_event, mode) => callback(mode);
    ipcRenderer.on("window-material:changed", listener);
    return () =>
      ipcRenderer.removeListener("window-material:changed", listener);
  },
  platform: process.platform,
  apiBaseUrl,
  isDesktop: true,
  versions: {
    electron: process.versions.electron,
    chrome: process.versions.chrome,
    node: process.versions.node,
  },
  selectDirectory: () => ipcRenderer.invoke("lantai:select-directory"),
  selectWorkspaceGrant: (payload) =>
    ipcRenderer.invoke("lantai:select-workspace-grant", payload || {}),
  restoreWorkspace: (payload) =>
    ipcRenderer.invoke("lantai:restore-workspace", payload || {}),
  selectFile: () => ipcRenderer.invoke("lantai:select-file"),
  selectFileGrant: () =>
    ipcRenderer.invoke("lantai:browser-workbench-select-file-grant"),
  readClipboardAttachments: (payload) =>
    ipcRenderer.invoke("lantai:read-clipboard-attachments", payload || {}),
  uploadChatAttachment: (payload) =>
    ipcRenderer.invoke("lantai:upload-chat-attachment", payload || {}),
  copyImageFromUrl: (payload) =>
    ipcRenderer.invoke("lantai:copy-image-from-url", payload || {}),
  saveImageFromUrl: (payload) =>
    ipcRenderer.invoke("lantai:save-image-from-url", payload || {}),
  saveFileAs: (payload) =>
    ipcRenderer.invoke("lantai:save-file-as", payload || {}),
  openExternal: (url) => ipcRenderer.invoke("lantai:open-external", url),
  openPath: (targetPath) => ipcRenderer.invoke("lantai:open-path", targetPath),
  openHtmlWork: (targetPath, title = "") =>
    ipcRenderer.invoke("lantai:open-html-work", targetPath, title),
  createWorkThumbnail: (targetPath) =>
    ipcRenderer.invoke("lantai:create-work-thumbnail", targetPath),
  openDocxEditor: (targetPath, options = {}) =>
    ipcRenderer.invoke("lantai:docx-editor-open", {
      path: targetPath,
      source_chat_id: String(options.source_chat_id || ""),
      agent_id: String(options.agent_id || "default"),
    }),
  openOfficeEditor: (targetPath, options = {}) => {
    const path = String(targetPath || "");
    if (/\.docx$/i.test(path)) {
      return ipcRenderer.invoke("lantai:docx-editor-open", {
        path,
        source_chat_id: String(options.source_chat_id || ""),
        agent_id: String(options.agent_id || "default"),
      });
    }
    if (/\.(md|markdown)$/i.test(path)) {
      return ipcRenderer.invoke("lantai:markdown-editor-open", { path });
    }
    return ipcRenderer.invoke("lantai:office-editor-open", {
      path,
      source_chat_id: String(options.source_chat_id || ""),
      agent_id: String(options.agent_id || "default"),
    });
  },
  officeWorkbench: {
    getState: () => ipcRenderer.invoke("lantai:office-workbench-state"),
    layout: (payload) =>
      ipcRenderer.invoke("lantai:office-workbench-layout", payload || {}),
    hide: () => ipcRenderer.invoke("lantai:office-workbench-hide", {}),
    onPresent: (callback) => {
      const listener = (_event, state) => callback(state);
      ipcRenderer.on("lantai:office-workbench-present", listener);
      return () =>
        ipcRenderer.removeListener("lantai:office-workbench-present", listener);
    },
  },
  getPathForFile: (file) => {
    try {
      return webUtils.getPathForFile(file);
    } catch {
      return "";
    }
  },
  onDocxRewriteRequest: (callback) => {
    const listener = (_event, envelope) => callback(envelope);
    ipcRenderer.on("lantai:docx-editor-rewrite-request", listener);
    return () =>
      ipcRenderer.removeListener(
        "lantai:docx-editor-rewrite-request",
        listener,
      );
  },
  respondDocxRewriteRequest: (envelope) =>
    ipcRenderer.send("lantai:docx-editor-rewrite-response", envelope),
  onMarkdownChatHandoff: (callback) => {
    const listener = (_event, envelope) => callback(envelope);
    ipcRenderer.on("lantai:markdown-chat-handoff", listener);
    return () =>
      ipcRenderer.removeListener("lantai:markdown-chat-handoff", listener);
  },
  revealPath: (targetPath) =>
    ipcRenderer.invoke("lantai:reveal-path", targetPath),
  openWith: (targetPath) => ipcRenderer.invoke("lantai:open-with", targetPath),
  getAppVersion: () => ipcRenderer.invoke("lantai:app-version"),
  getComputerPermissionStatus: () =>
    ipcRenderer.invoke("lantai:computer-permissions-status"),
  requestComputerPermission: (key) =>
    ipcRenderer.invoke("lantai:computer-permissions-request", key),
  getMacUseStatus: () => ipcRenderer.invoke("lantai:mac-use-status"),
  stopMacUse: (runId) => ipcRenderer.invoke("lantai:mac-use-stop", runId),
  selectMacUseTarget: (target) =>
    ipcRenderer.invoke("lantai:mac-use-select-target", target || {}),
  requestMacUsePermission: (key) =>
    ipcRenderer.invoke("lantai:mac-use-request-permission", key),
  getSystemBehavior: () => ipcRenderer.invoke("lantai:system-behavior-get"),
  getLaunchServicesState: () =>
    ipcRenderer.invoke("lantai:launch-services-state"),
  cleanLaunchServices: (payload) =>
    ipcRenderer.invoke("lantai:launch-services-clean", payload || {}),
  getAppIcon: () => ipcRenderer.invoke("lantai:app-icon-get"),
  selectAppIcon: (id) => ipcRenderer.invoke("lantai:app-icon-select", { id }),
  importCustomAppIcon: () =>
    ipcRenderer.invoke("lantai:app-icon-import-custom"),
  updateSystemBehavior: (payload) =>
    ipcRenderer.invoke("lantai:system-behavior-update", payload || {}),
  setTaskActivity: (payload) =>
    ipcRenderer.invoke("lantai:system-behavior-task-activity", payload || {}),
  notifySystemBehavior: (payload) =>
    ipcRenderer.invoke("lantai:system-behavior-notify", payload || {}),
  onDailyBriefOpen: (callback) => {
    const listener = (_event, payload) => callback(payload || {});
    ipcRenderer.on("lantai:daily-brief-open", listener);
    return () =>
      ipcRenderer.removeListener("lantai:daily-brief-open", listener);
  },
  onReminderOpen: (callback) => {
    const listener = (_event, payload) => callback(payload || {});
    ipcRenderer.on("lantai:reminder-open", listener);
    return () => ipcRenderer.removeListener("lantai:reminder-open", listener);
  },
  browserWorkbench: {
    open: (payload) =>
      ipcRenderer.invoke("lantai:browser-workbench-open", payload || {}),
    action: (payload) =>
      ipcRenderer.invoke("lantai:browser-workbench-action", payload || {}),
    getState: () => ipcRenderer.invoke("lantai:browser-workbench-state"),
    mountSidecar: (payload) =>
      ipcRenderer.invoke(
        "lantai:browser-workbench-sidecar-mount",
        payload || {},
      ),
    layoutSidecar: (bounds) =>
      ipcRenderer.invoke(
        "lantai:browser-workbench-sidecar-layout",
        bounds || {},
      ),
    setSidecarControl: (owner) =>
      ipcRenderer.invoke("lantai:browser-workbench-sidecar-control", owner),
    captureSidecarPreview: () =>
      ipcRenderer.invoke("lantai:browser-workbench-sidecar-preview"),
    unmountSidecar: () =>
      ipcRenderer.invoke("lantai:browser-workbench-sidecar-unmount"),
    popoutSidecar: () =>
      ipcRenderer.invoke("lantai:browser-workbench-sidecar-popout"),
    focusExternalBrowser: (browserName) =>
      ipcRenderer.invoke(
        "lantai:browser-workbench-sidecar-focus-external",
        browserName || "",
      ),
    onState: (callback) => {
      const listener = (_event, state) => callback(state);
      ipcRenderer.on("lantai:browser-workbench-state", listener);
      return () =>
        ipcRenderer.removeListener("lantai:browser-workbench-state", listener);
    },
  },
});

window.addEventListener("DOMContentLoaded", () => {
  document.body.classList.add("is-electron");
  document.body.classList.add(`platform-${process.platform}`);
});
