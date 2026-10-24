const path = require("node:path");
const { fileURLToPath } = require("node:url");
const { AsyncLocalStorage } = require("node:async_hooks");
const documentOwners = new WeakMap();

function rendererPathForUrl(rawUrl) {
  try {
    const url = new URL(String(rawUrl || ""));
    if (url.protocol !== "file:" || (url.hostname && url.hostname !== "localhost")) return null;
    return path.resolve(fileURLToPath(url));
  } catch { return null; }
}

function captureRendererDocument(webContents, frame, isAllowedUrl = () => true) {
  try {
    if (!webContents || webContents.isDestroyed?.()) return null;
    const mainFrame = webContents.mainFrame;
    const url = String(webContents.getURL?.() || "");
    if (!frame || !mainFrame || frame.parent !== null || mainFrame.parent !== null || !url) return null;
    if (frame.url !== url || mainFrame.url !== url || !isAllowedUrl(url)) return null;
    if (!Number.isInteger(frame.processId) || !Number.isInteger(frame.routingId)) return null;
    if (frame.processId !== mainFrame.processId || frame.routingId !== mainFrame.routingId) return null;
    return { webContents, url, processId: frame.processId, routingId: frame.routingId };
  } catch { return null; }
}

function sameRendererDocument(left, right) {
  return Boolean(left && right && left.webContents === right.webContents && left.url === right.url &&
    left.processId === right.processId && left.routingId === right.routingId);
}

function observeRendererDocument(webContents) {
  if (!webContents) return null;
  let owner = documentOwners.get(webContents);
  if (owner) return owner;
  owner = { epoch: 0, pending: false, destroyed: false };
  documentOwners.set(webContents, owner);
  const retire = () => { owner.epoch += 1; owner.pending = true; };
  webContents.on?.("did-start-navigation", (event, _url, isInPlace, isMainFrame) => {
    const main = event?.isMainFrame ?? isMainFrame;
    const sameDocument = event?.isSameDocument ?? isInPlace;
    if (main && !sameDocument) retire();
  });
  webContents.on?.("did-frame-navigate", (_event, url, _status, _text, isMainFrame, processId, routingId) => {
    if (owner.destroyed || isMainFrame !== true) return;
    const document = captureRendererDocument(webContents, webContents.mainFrame);
    if (document && document.url === url && document.processId === processId && document.routingId === routingId) {
      // Chromium may keep both the WebFrameMain object and its process/routing
      // IDs across a reload. A matching commit admits the new document without
      // restoring the epoch of any request from the previous document.
      owner.pending = false;
    }
  });
  webContents.on?.("render-process-gone", retire);
  webContents.once?.("destroyed", () => { owner.destroyed = true; retire(); });
  return owner;
}

function captureCurrentRendererDocument(webContents, frame, isAllowedUrl) {
  const owner = observeRendererDocument(webContents);
  if (!owner || owner.pending || owner.destroyed) return null;
  return captureRendererDocument(webContents, frame, isAllowedUrl);
}

function captureRendererRequest(event, isTrustedIpcEvent) {
  try {
    if (!isTrustedIpcEvent(event)) return null;
    const document = captureCurrentRendererDocument(event?.sender, event?.senderFrame);
    if (!document) return null;
    const owner = observeRendererDocument(event.sender);
    return { document, owner, epoch: owner.epoch };
  } catch { return null; }
}

function isCurrentRendererRequest(request, event, isTrustedIpcEvent) {
  try {
    return Boolean(request && request.owner.epoch === request.epoch && isTrustedIpcEvent(event) &&
      sameRendererDocument(request.document, captureCurrentRendererDocument(event.sender, event.senderFrame)));
  } catch { return false; }
}

// A handler retains its original frame and epoch across every await. Checking
// only the WebContents URL again would give an old request a new document's rights.
function createRendererIpcGuard(isTrustedIpcEvent) {
  const invocation = new AsyncLocalStorage();
  const changed = () => Object.assign(new Error("The requesting document is no longer authorized."),
    { code: "desktop_document_changed" });
  const assertCurrent = () => {
    const scope = invocation.getStore();
    if (scope && !isCurrentRendererRequest(scope.request, scope.event, isTrustedIpcEvent)) throw changed();
  };
  const operation = (fn, receiver) => function (...args) {
    assertCurrent();
    const result = Reflect.apply(fn, receiver ?? this, args);
    if (result && typeof result.then === "function") {
      return Promise.resolve(result).then(value => { assertCurrent(); return value; });
    }
    assertCurrent();
    return result;
  };
  const operations = object => !object ? object : new Proxy(object, {
    get(target, key) {
      const value = Reflect.get(target, key, target);
      return typeof value === "function" ? operation(value, target) : value;
    },
  });
  const failure = channel => {
    if (["lantai:select-file", "lantai:select-directory"].includes(channel)) return null;
    return { success: false, ok: false, error: changed().message,
      error_code: "desktop_document_changed",
      ...(channel === "lantai:read-clipboard-attachments" ? { attachments: [] } : {}) };
  };
  return {
    assertCurrent, operation, operations,
    isTrusted(event) {
      const scope = invocation.getStore();
      return scope?.event === event
        ? isCurrentRendererRequest(scope.request, event, isTrustedIpcEvent)
        : Boolean(captureRendererRequest(event, isTrustedIpcEvent));
    },
    ipcMain(raw) {
      return { handle(channel, listener) {
        raw.handle(channel, (event, ...args) => {
          const request = captureRendererRequest(event, isTrustedIpcEvent);
          return invocation.run({ request, event }, async () => {
            try {
              const result = await listener(event, ...args);
              // Preserve each handler's existing rejection shape for callers
              // that were never admitted; stale admitted requests cannot return data.
              return request && !isCurrentRendererRequest(request, event, isTrustedIpcEvent)
                ? failure(channel) : result;
            } catch (error) {
              if (error?.code === "desktop_document_changed") return failure(channel);
              throw error;
            }
          });
        });
      } };
    },
  };
}

module.exports = { captureRendererDocument, captureCurrentRendererDocument, observeRendererDocument,
  sameRendererDocument, rendererPathForUrl,
  captureRendererRequest, isCurrentRendererRequest, createRendererIpcGuard };
