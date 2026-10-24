const crypto = require("node:crypto");
const path = require("node:path");

const ROUTE = "/office-workbench";
const PRESENT_CHANNEL = "lantai:office-workbench-present";
const STATE_CHANNEL = "lantai:office-workbench-state";
const LAYOUT_CHANNEL = "lantai:office-workbench-layout";
const HIDE_CHANNEL = "lantai:office-workbench-hide";
const MIN_VIEW_WIDTH = 320;
const MIN_VIEW_HEIGHT = 200;

function createOfficeWorkbenchController({
  ipcMain,
  isTrustedIpcEvent,
  getMainWindow,
}) {
  const entries = new Map();
  let activeEntry = null;
  let attachedView = null;
  let observedWindow = null;
  let windowReplay = null;

  function failure(error, errorCode) {
    return { success: false, error, error_code: errorCode };
  }

  function isViewAlive(view) {
    if (!view) return false;
    if (typeof view.isDestroyed === "function" && view.isDestroyed())
      return false;
    return !view.webContents?.isDestroyed?.();
  }

  function publicState(entry = activeEntry) {
    if (!entry) {
      return { active: false, route: ROUTE, visible: false };
    }
    const bounds =
      attachedView === entry.view && typeof entry.view.getBounds === "function"
        ? entry.view.getBounds()
        : null;
    return {
      active: true,
      workbench_id: entry.id,
      kind: entry.kind,
      display_name: entry.displayName,
      source_chat_id: entry.sourceChatId || "",
      route: ROUTE,
      visible: attachedView === entry.view && entry.visible === true,
      bounds,
    };
  }

  function removeAttachedView() {
    if (!attachedView) return;
    const mainWindow = getMainWindow();
    if (mainWindow && !mainWindow.isDestroyed?.()) {
      try {
        mainWindow.contentView?.removeChildView(attachedView);
      } catch {
        // The view may already have been detached during window teardown.
      }
    }
    attachedView = null;
    if (activeEntry) activeEntry.visible = false;
  }

  function usableBounds(bounds) {
    return Boolean(
      bounds &&
      Number.isFinite(bounds.width) &&
      Number.isFinite(bounds.height) &&
      bounds.width >= MIN_VIEW_WIDTH &&
      bounds.height >= MIN_VIEW_HEIGHT,
    );
  }

  function setViewVisible(entry, visible) {
    entry.visible = visible;
    if (typeof entry.view.setVisible === "function") {
      entry.view.setVisible(visible);
    }
  }

  function announce(entry) {
    const mainWindow = getMainWindow();
    if (
      mainWindow?.isDestroyed?.() ||
      mainWindow?.webContents?.isDestroyed?.()
    ) {
      return;
    }
    mainWindow?.webContents?.send(PRESENT_CHANNEL, publicState(entry));
  }

  async function open({
    path: inputPath,
    kind,
    source_chat_id: sourceChatId = "",
    createView,
    onActivate,
  } = {}) {
    const canonicalPath = path.resolve(String(inputPath || ""));
    const normalizedKind = String(kind || "")
      .trim()
      .toLowerCase();
    if (!canonicalPath || !normalizedKind || typeof createView !== "function") {
      return failure(
        "Office 工作台参数不完整",
        "office_workbench_invalid_request",
      );
    }

    const key = `${normalizedKind}:${canonicalPath}`;
    let entry = entries.get(key);
    let reused = Boolean(entry && isViewAlive(entry.view));
    if (!reused) {
      if (entry) entries.delete(key);
      const view = await createView();
      if (!isViewAlive(view)) {
        return failure(
          "本地编辑器未能创建",
          "office_workbench_view_unavailable",
        );
      }
      entry = {
        id: crypto.createHash("sha256").update(key).digest("hex").slice(0, 20),
        key,
        kind: normalizedKind,
        canonicalPath,
        displayName: path.basename(canonicalPath),
        sourceChatId: String(sourceChatId || "")
          .trim()
          .slice(0, 200),
        view,
        visible: false,
        lastValidBounds: null,
      };
      view.webContents?.on?.("did-finish-load", () => {
        if (activeEntry === entry) replayLastValidBounds("view-load");
      });
      entries.set(key, entry);
      reused = false;
    } else if (sourceChatId) {
      entry.sourceChatId = String(sourceChatId).trim().slice(0, 200);
    }

    if (attachedView && attachedView !== entry.view) removeAttachedView();
    activeEntry = entry;
    if (typeof onActivate === "function") {
      await onActivate(entry.view, { reused });
    }
    announce(entry);
    return {
      success: true,
      workbench_id: entry.id,
      route: ROUTE,
      reused,
    };
  }

  function trusted(event) {
    if (isTrustedIpcEvent(event)) return null;
    return failure(
      "Office 工作台请求来源不受信任",
      "office_workbench_untrusted_sender",
    );
  }

  function clampBounds(rawBounds, mainWindow) {
    const content = mainWindow.getContentBounds?.() || {
      width: Number.MAX_SAFE_INTEGER,
      height: Number.MAX_SAFE_INTEGER,
    };
    const maxWidth = Math.max(1, Math.trunc(Number(content.width) || 1));
    const maxHeight = Math.max(1, Math.trunc(Number(content.height) || 1));
    const x = Math.min(
      maxWidth - 1,
      Math.max(0, Math.trunc(Number(rawBounds?.x) || 0)),
    );
    const y = Math.min(
      maxHeight - 1,
      Math.max(0, Math.trunc(Number(rawBounds?.y) || 0)),
    );
    const width = Math.max(
      1,
      Math.min(maxWidth - x, Math.trunc(Number(rawBounds?.width) || 1)),
    );
    const height = Math.max(
      1,
      Math.min(maxHeight - y, Math.trunc(Number(rawBounds?.height) || 1)),
    );
    return { x, y, width, height };
  }

  function detachWindowReplay() {
    if (!observedWindow || !windowReplay) return;
    observedWindow.removeListener?.("show", windowReplay);
    observedWindow.removeListener?.("resize", windowReplay);
    observedWindow = null;
    windowReplay = null;
  }

  function bindWindowReplay(mainWindow) {
    if (observedWindow === mainWindow) return;
    detachWindowReplay();
    observedWindow = mainWindow;
    windowReplay = () => replayLastValidBounds("window-event");
    mainWindow.on?.("show", windowReplay);
    mainWindow.on?.("resize", windowReplay);
  }

  function applyBounds(entry, rawBounds, mainWindow) {
    if (!usableBounds(rawBounds)) {
      if (attachedView === entry.view) removeAttachedView();
      else setViewVisible(entry, false);
      return { success: true, deferred: true, visible: false, bounds: null };
    }
    const bounds = clampBounds(rawBounds, mainWindow);
    if (!usableBounds(bounds)) {
      if (attachedView === entry.view) removeAttachedView();
      else setViewVisible(entry, false);
      return { success: true, deferred: true, visible: false, bounds: null };
    }
    entry.lastValidBounds = bounds;
    bindWindowReplay(mainWindow);
    if (mainWindow.isVisible?.() === false) {
      setViewVisible(entry, false);
      return { success: true, deferred: true, visible: false, bounds };
    }
    if (attachedView !== entry.view) {
      removeAttachedView();
      mainWindow.contentView?.addChildView(entry.view);
      attachedView = entry.view;
    }
    entry.view.setBounds(bounds);
    const actual =
      typeof entry.view.getBounds === "function"
        ? entry.view.getBounds()
        : bounds;
    if (!usableBounds(actual)) {
      removeAttachedView();
      return { success: true, deferred: true, visible: false, bounds: actual };
    }
    setViewVisible(entry, true);
    return { success: true, deferred: false, visible: true, bounds: actual };
  }

  function replayLastValidBounds(_reason) {
    const entry = activeEntry;
    const mainWindow = getMainWindow();
    if (
      !entry ||
      !entry.lastValidBounds ||
      !isViewAlive(entry.view) ||
      !mainWindow ||
      mainWindow.isDestroyed?.()
    ) {
      return;
    }
    applyBounds(entry, entry.lastValidBounds, mainWindow);
  }

  async function layout(event, payload = {}) {
    const error = trusted(event);
    if (error) return error;
    if (
      !activeEntry ||
      payload.workbench_id !== activeEntry.id ||
      !isViewAlive(activeEntry.view)
    ) {
      return failure(
        "Office 工作台会话已失效",
        "office_workbench_session_stale",
      );
    }
    const mainWindow = getMainWindow();
    if (!mainWindow || mainWindow.isDestroyed?.()) {
      return failure("主窗口不可用", "office_workbench_window_unavailable");
    }
    bindWindowReplay(mainWindow);
    return applyBounds(activeEntry, payload.bounds, mainWindow);
  }

  async function hide(event) {
    const error = trusted(event);
    if (error) return error;
    removeAttachedView();
    activeEntry = null;
    announce(null);
    return { success: true };
  }

  async function getState(event) {
    const error = trusted(event);
    return error || publicState();
  }

  function installIpcHandlers() {
    for (const channel of [STATE_CHANNEL, LAYOUT_CHANNEL, HIDE_CHANNEL]) {
      ipcMain.removeHandler?.(channel);
    }
    ipcMain.handle(STATE_CHANNEL, getState);
    ipcMain.handle(LAYOUT_CHANNEL, layout);
    ipcMain.handle(HIDE_CHANNEL, hide);
  }

  function dispose() {
    detachWindowReplay();
    removeAttachedView();
    for (const entry of entries.values()) {
      try {
        if (typeof entry.view.destroy === "function") entry.view.destroy();
        else if (!entry.view.webContents?.isDestroyed?.())
          entry.view.webContents?.close?.();
      } catch {
        // Best-effort teardown during app quit.
      }
    }
    entries.clear();
    activeEntry = null;
  }

  return {
    dispose,
    getState,
    hide,
    installIpcHandlers,
    layout,
    open,
  };
}

module.exports = {
  HIDE_CHANNEL,
  LAYOUT_CHANNEL,
  PRESENT_CHANNEL,
  ROUTE,
  STATE_CHANNEL,
  MIN_VIEW_HEIGHT,
  MIN_VIEW_WIDTH,
  createOfficeWorkbenchController,
};
