const crypto = require("node:crypto");
const childProcess = require("node:child_process");
const fs = require("node:fs");
const path = require("node:path");
const { fileURLToPath, pathToFileURL } = require("node:url");
const { createRendererIpcGuard } = require("./rendererDocumentTrust.cjs");

const MAX_CLIPBOARD_ATTACHMENT_BYTES = 150 * 1024 * 1024;
const MAX_CLIPBOARD_ATTACHMENTS = 20;
const MAX_CHAT_ATTACHMENT_BYTES = 150 * 1024 * 1024;
const CHAT_ATTACHMENT_UPLOAD_TIMEOUT_MS = 3 * 60 * 1000;
const MAX_GENERATED_IMAGE_BYTES = 50 * 1024 * 1024;
const CLIPBOARD_MIME_BY_EXTENSION = {
  bmp: "image/bmp",
  gif: "image/gif",
  heic: "image/heic",
  heif: "image/heif",
  jpeg: "image/jpeg",
  jpg: "image/jpeg",
  png: "image/png",
  svg: "image/svg+xml",
  tif: "image/tiff",
  tiff: "image/tiff",
  webp: "image/webp",
};
const GENERATED_IMAGE_EXTENSION_BY_MIME = {
  "image/bmp": "bmp",
  "image/gif": "gif",
  "image/jpeg": "jpg",
  "image/png": "png",
  "image/tiff": "tiff",
  "image/webp": "webp",
};
const GENERATED_IMAGE_FILTER_EXTENSIONS = {
  "image/jpeg": ["jpg", "jpeg"],
  "image/tiff": ["tif", "tiff"],
};

function mapNativeMediaPermissionStatus(status) {
  if (status === "granted") return "granted";
  if (status === "denied" || status === "restricted") return "denied";
  return "unknown";
}

function findMacAppBundlePath(executablePath, pathImpl = path) {
  let current = pathImpl.resolve(String(executablePath || ""));
  while (current && current !== pathImpl.dirname(current)) {
    if (pathImpl.extname(current).toLowerCase() === ".app") {
      return current;
    }
    current = pathImpl.dirname(current);
  }
  return null;
}

function getNativeComputerPermissionStatus({
  app = null,
  systemPreferences = null,
  platform = process.platform,
  pathImpl = path,
} = {}) {
  const unsupported = platform !== "darwin";
  let appPath = null;
  try {
    appPath =
      app?.isPackaged === false
        ? null
        : findMacAppBundlePath(app?.getPath?.("exe"), pathImpl);
  } catch {
    appPath = null;
  }

  const accessibilityCheck =
    !unsupported &&
    typeof systemPreferences?.isTrustedAccessibilityClient === "function";
  let accessibilityStatus = unsupported ? "unsupported" : "unknown";
  if (accessibilityCheck) {
    try {
      accessibilityStatus = systemPreferences.isTrustedAccessibilityClient(
        false,
      )
        ? "granted"
        : "denied";
    } catch {
      accessibilityStatus = "unknown";
    }
  }

  const mediaCheck =
    !unsupported &&
    typeof systemPreferences?.getMediaAccessStatus === "function";
  const mediaStatus = (kind) => {
    if (unsupported) return "unsupported";
    if (!mediaCheck) return "unknown";
    try {
      return mapNativeMediaPermissionStatus(
        systemPreferences.getMediaAccessStatus(kind),
      );
    } catch {
      return "unknown";
    }
  };

  return {
    ok: true,
    platform,
    app_path: appPath,
    permissions: [
      {
        key: "accessibility",
        status: accessibilityStatus,
        can_check: accessibilityCheck,
      },
      {
        key: "screen_recording",
        status: mediaStatus("screen"),
        can_check: mediaCheck,
      },
    ],
  };
}

function clipboardMimeType(filePath, fallback = "") {
  const extension = path
    .extname(String(filePath || ""))
    .slice(1)
    .toLowerCase();
  return CLIPBOARD_MIME_BY_EXTENSION[extension] || fallback;
}

function safeClipboardName(value, fallback = "clipboard-image.png") {
  const candidate = path.basename(String(value || "").trim());
  if (
    !candidate ||
    candidate === "." ||
    candidate === path.sep ||
    candidate.length > 255
  ) {
    return fallback;
  }
  return candidate.replace(/[\u0000-\u001f]/g, "") || fallback;
}

function chatAttachmentBuffer(value) {
  if (Buffer.isBuffer(value)) {
    return value;
  }
  if (value instanceof ArrayBuffer) {
    return Buffer.from(value);
  }
  if (ArrayBuffer.isView(value)) {
    return Buffer.from(value.buffer, value.byteOffset, value.byteLength);
  }
  return null;
}

function safeChatAttachmentHeader(value, maxLength = 512) {
  const candidate = String(value || "").trim();
  if (
    !candidate ||
    candidate.length > maxLength ||
    /[\u0000\r\n]/.test(candidate)
  ) {
    return "";
  }
  return candidate;
}

async function uploadChatAttachmentThroughBackend({
  payload = {},
  fetchImpl = globalThis.fetch,
  getApiBaseUrl = () => "",
  getDesktopToken = () => "",
  timeoutMs = CHAT_ATTACHMENT_UPLOAD_TIMEOUT_MS,
}) {
  const bytes = chatAttachmentBuffer(payload.bytes);
  if (!bytes) {
    return {
      ok: false,
      error: "Attachment bytes are unavailable.",
      error_code: "invalid_attachment_bytes",
    };
  }
  if (bytes.byteLength > MAX_CHAT_ATTACHMENT_BYTES) {
    return {
      ok: false,
      error: "File too large (max 150 MB).",
      error_code: "file_too_large",
    };
  }
  if (typeof fetchImpl !== "function") {
    return {
      ok: false,
      error: "Desktop upload transport is unavailable.",
      error_code: "upload_transport_unavailable",
    };
  }

  const apiBaseUrl = String(getApiBaseUrl() || "").replace(/\/$/, "");
  const desktopToken = safeChatAttachmentHeader(getDesktopToken(), 2048);
  if (!apiBaseUrl || !desktopToken) {
    return {
      ok: false,
      error: "Lantai backend is not ready for uploads.",
      error_code: "backend_unavailable",
    };
  }

  const fileName = safeClipboardName(payload.fileName, "attachment.bin");
  const mimeType =
    safeChatAttachmentHeader(payload.mimeType, 255) ||
    "application/octet-stream";
  const headers = {
    "X-Lantai-Desktop-Token": desktopToken,
  };
  const agentId = safeChatAttachmentHeader(payload.agentId);
  const workspaceGrantId = safeChatAttachmentHeader(payload.workspaceGrantId);
  if (agentId) headers["X-Agent-Id"] = agentId;
  if (workspaceGrantId) {
    headers["X-Workspace-Grant-Id"] = workspaceGrantId;
  }

  const controller = new AbortController();
  const timeoutId = setTimeout(
    () => {
      controller.abort(
        new DOMException("Attachment upload timed out.", "TimeoutError"),
      );
    },
    Math.max(1, Number(timeoutMs) || CHAT_ATTACHMENT_UPLOAD_TIMEOUT_MS),
  );

  try {
    const formData = new FormData();
    formData.append("file", new Blob([bytes], { type: mimeType }), fileName);
    const response = await fetchImpl(`${apiBaseUrl}/api/console/upload`, {
      method: "POST",
      headers,
      body: formData,
      signal: controller.signal,
    });
    if (!response.ok) {
      const detail = await response.text().catch(() => "");
      return {
        ok: false,
        error: `Upload failed: ${response.status}${detail ? ` - ${detail}` : ""}`,
        error_code: "upload_failed",
        status: response.status,
      };
    }
    const body = await response.json();
    const url = String(body?.url || "");
    if (!url) {
      return {
        ok: false,
        error: "Upload completed without a file URL.",
        error_code: "invalid_upload_response",
      };
    }
    return {
      ok: true,
      url,
      file_name: String(body?.file_name || fileName),
      stored_name: body?.stored_name ? String(body.stored_name) : undefined,
      size: Number(body?.size ?? bytes.byteLength),
      digest: body?.digest ? String(body.digest) : undefined,
      version: Number(body?.version || 0),
      upload_receipt: body?.upload_receipt
        ? String(body.upload_receipt)
        : undefined,
    };
  } catch (error) {
    const timedOut = controller.signal.aborted;
    return {
      ok: false,
      error: timedOut
        ? "Attachment upload timed out."
        : error?.message || "Attachment upload failed.",
      error_code: timedOut ? "upload_timeout" : "upload_failed",
    };
  } finally {
    clearTimeout(timeoutId);
  }
}

function collectClipboardFilePaths(clipboard, pathImpl = path) {
  const formats =
    typeof clipboard?.availableFormats === "function"
      ? clipboard.availableFormats("clipboard")
      : [];
  const fileFormats = formats.filter((format) =>
    /file.?url|uri.?list|filenames/i.test(String(format || "")),
  );
  const paths = [];
  const seen = new Set();
  const appendPath = (value) => {
    const candidate = String(value || "").trim();
    if (!candidate || candidate.startsWith("#")) return;
    let filePath = "";
    try {
      if (/^file:/i.test(candidate)) {
        filePath = fileURLToPath(candidate);
      } else if (pathImpl.isAbsolute(candidate)) {
        filePath = candidate;
      }
    } catch {
      return;
    }
    if (!filePath || seen.has(filePath)) return;
    seen.add(filePath);
    paths.push(filePath);
  };
  const appendRaw = (raw) => {
    const value = String(raw || "").replace(/\u0000/g, "\n");
    value
      .split(/\r?\n/)
      .map((line) => line.trim())
      .forEach(appendPath);
    for (const match of value.matchAll(/file:\/\/[^\r\n\u0000]+/gi)) {
      appendPath(match[0].trim());
    }
  };

  for (const format of fileFormats) {
    try {
      if (typeof clipboard.read === "function") {
        appendRaw(clipboard.read(format));
      }
    } catch {
      // Some native pasteboard formats cannot be decoded as text.
    }
    try {
      if (typeof clipboard.readBuffer === "function") {
        appendRaw(clipboard.readBuffer(format).toString("utf8"));
      }
    } catch {
      // Best effort: another declared file format may still be readable.
    }
  }
  return paths.slice(0, MAX_CLIPBOARD_ATTACHMENTS);
}

async function readNativeClipboardAttachments({
  clipboard,
  payload = {},
  fsImpl = fs,
  pathImpl = path,
}) {
  if (!clipboard) {
    return {
      attachments: [],
      error: "Native clipboard access is unavailable.",
      error_code: "clipboard_unavailable",
    };
  }

  const attachments = [];
  const filePaths = collectClipboardFilePaths(clipboard, pathImpl);
  for (const filePath of filePaths) {
    try {
      const stat = await fsImpl.promises.stat(filePath);
      if (!stat.isFile()) continue;
      if (stat.size > MAX_CLIPBOARD_ATTACHMENT_BYTES) {
        return {
          attachments,
          error: "Clipboard file exceeds the 150 MB attachment limit.",
          error_code: "clipboard_file_too_large",
        };
      }
      const content = await fsImpl.promises.readFile(filePath);
      attachments.push({
        name: safeClipboardName(pathImpl.basename(filePath)),
        type: clipboardMimeType(filePath),
        size: content.length,
        lastModified: Math.round(stat.mtimeMs || 0),
        dataBase64: content.toString("base64"),
      });
    } catch {
      // A stale Finder pasteboard entry should not block other attachments.
    }
  }
  if (attachments.length > 0) {
    return { attachments };
  }

  try {
    const image = clipboard.readImage("clipboard");
    if (image && typeof image.isEmpty === "function" && !image.isEmpty()) {
      const content = image.toPNG();
      if (content.length > MAX_CLIPBOARD_ATTACHMENT_BYTES) {
        return {
          attachments: [],
          error: "Clipboard image exceeds the 150 MB attachment limit.",
          error_code: "clipboard_image_too_large",
        };
      }
      const preferredName = safeClipboardName(
        payload.preferredName,
        "clipboard-image.png",
      );
      const preferredExtension = path.extname(preferredName).toLowerCase();
      const name =
        preferredExtension === ".png"
          ? preferredName
          : preferredName === "clipboard-image.png"
            ? preferredName
            : `${path.basename(preferredName, preferredExtension)}.png`;
      return {
        attachments: [
          {
            name,
            type: "image/png",
            size: content.length,
            lastModified: 0,
            dataBase64: content.toString("base64"),
          },
        ],
      };
    }
  } catch {
    // Text-only clipboards legitimately have no native image representation.
  }

  return { attachments: [] };
}

async function fetchGeneratedImage({
  url,
  fetchImpl = globalThis.fetch,
  getApiBaseUrl = () => "",
  getDesktopToken = () => "",
}) {
  let parsedUrl;
  try {
    parsedUrl = new URL(String(url || "").trim());
  } catch {
    throw new Error("Invalid image URL.");
  }
  if (!["http:", "https:"].includes(parsedUrl.protocol)) {
    throw new Error("Only HTTP image URLs are supported.");
  }
  if (typeof fetchImpl !== "function") {
    throw new Error("Image download is unavailable.");
  }
  let backendOrigin = "";
  try {
    backendOrigin = new URL(String(getApiBaseUrl() || "")).origin;
  } catch {
    // Remote image URLs do not depend on the local backend being available.
  }
  const desktopToken = safeChatAttachmentHeader(getDesktopToken(), 2048);
  const isCurrentBackendImage =
    Boolean(backendOrigin) && parsedUrl.origin === backendOrigin;
  const response = await fetchImpl(parsedUrl.href, {
    redirect: isCurrentBackendImage ? "error" : "follow",
    ...(isCurrentBackendImage && desktopToken
      ? { headers: { "X-Lantai-Desktop-Token": desktopToken } }
      : {}),
  });
  if (!response?.ok) {
    throw new Error(`Image request failed (${response?.status || "unknown"}).`);
  }
  const declaredLength = Number(response.headers?.get?.("content-length") || 0);
  if (
    Number.isFinite(declaredLength) &&
    declaredLength > MAX_GENERATED_IMAGE_BYTES
  ) {
    throw new Error("Generated image exceeds the 50 MB limit.");
  }
  const bytes = Buffer.from(await response.arrayBuffer());
  if (!bytes.length) {
    throw new Error("Generated image is empty.");
  }
  if (bytes.length > MAX_GENERATED_IMAGE_BYTES) {
    throw new Error("Generated image exceeds the 50 MB limit.");
  }
  return {
    bytes,
    mimeType: String(response.headers?.get?.("content-type") || "")
      .split(";")[0]
      .trim()
      .toLowerCase(),
    sourceUrl: parsedUrl,
  };
}

async function loadGeneratedImage({
  localPaths,
  localPath,
  url,
  app,
  fsImpl = fs,
  pathImpl = path,
  fetchImpl = globalThis.fetch,
  getApiBaseUrl = () => "",
  getDesktopToken = () => "",
  getBackendWorkingDir = () => "",
}) {
  const requestedPaths = [
    ...(Array.isArray(localPaths) ? localPaths : []),
    localPath,
  ]
    .map((candidate) => String(candidate || "").trim())
    .filter(
      (candidate, index, candidates) =>
        candidate && candidates.indexOf(candidate) === index,
    );
  if (requestedPaths.length && typeof app?.getPath === "function") {
    const configuredRoots = [
      getBackendWorkingDir(),
      pathImpl.join(app.getPath("userData"), "working"),
    ]
      .map((candidate) => String(candidate || "").trim())
      .filter(
        (candidate, index, candidates) =>
          candidate && candidates.indexOf(candidate) === index,
      );
    const workingRoots = [];
    for (const configuredRoot of configuredRoots) {
      try {
        workingRoots.push(await fsImpl.promises.realpath(configuredRoot));
      } catch {
        // The backend may not have created this configured root yet.
      }
    }
    for (const requestedPath of requestedPaths) {
      if (!workingRoots.length || !pathImpl.isAbsolute(requestedPath)) continue;
      try {
        const resolvedPath = await fsImpl.promises.realpath(requestedPath);
        const insideWorkingRoot = workingRoots.some((workingRoot) => {
          const relative = pathImpl.relative(workingRoot, resolvedPath);
          return (
            relative === "" ||
            (relative !== ".." &&
              !relative.startsWith(`..${pathImpl.sep}`) &&
              !pathImpl.isAbsolute(relative))
          );
        });
        if (!insideWorkingRoot) continue;
        const stat = await fsImpl.promises.stat(resolvedPath);
        if (!stat.isFile()) {
          throw new Error("Generated image path is not a file.");
        }
        if (stat.size > MAX_GENERATED_IMAGE_BYTES) {
          throw new Error("Generated image exceeds the 50 MB limit.");
        }
        const bytes = await fsImpl.promises.readFile(resolvedPath);
        if (!bytes.length) {
          throw new Error("Generated image is empty.");
        }
        return {
          bytes,
          mimeType:
            CLIPBOARD_MIME_BY_EXTENSION[
              pathImpl.extname(resolvedPath).slice(1).toLowerCase()
            ] || "",
          sourceUrl: pathToFileURL(resolvedPath),
        };
      } catch {
        // This candidate may have expired; continue through all local copies.
      }
    }
  }
  return fetchGeneratedImage({
    url,
    fetchImpl,
    getApiBaseUrl,
    getDesktopToken,
  });
}

function generatedImageFileName(sourceUrl, mimeType) {
  let candidate = "";
  try {
    candidate = decodeURIComponent(path.basename(sourceUrl.pathname));
  } catch {
    candidate = "";
  }
  const extension = path.extname(candidate);
  if (candidate && extension) {
    return safeClipboardName(candidate, "lantai-generated-image.png");
  }
  const fallbackExtension =
    GENERATED_IMAGE_EXTENSION_BY_MIME[mimeType] || "png";
  const baseName = safeClipboardName(candidate, "lantai-generated-image");
  return `${baseName}.${fallbackExtension}`;
}

function installDesktopIpcHandlers({
  app,
  ipcMain,
  dialog,
  shell,
  clipboard = null,
  nativeImage = null,
  fetchImpl = globalThis.fetch,
  getMainWindow,
  getAllowedOrigins = () => [],
  isTrustedIpcEvent = null,
  distributionChannel = "official",
  getApiBaseUrl = () => "",
  getDesktopToken = () => "",
  getBackendWorkingDir = () => "",
  workspaceBookmarkStore = null,
  fsImpl = fs,
  pathImpl = path,
  randomId = () => crypto.randomBytes(8).toString("hex"),
  now = () => new Date(),
  execFileImpl = childProcess.execFile,
  platform = process.platform,
  systemBehaviorController = null,
  systemPreferences = null,
  BrowserWindow = null,
}) {
  function originOf(rawUrl) {
    try {
      return new URL(String(rawUrl || "")).origin;
    } catch {
      return "";
    }
  }

  function isMainFrame(frame) {
    if (!frame) {
      return true;
    }
    if ("parent" in frame) {
      return frame.parent === null;
    }
    if ("top" in frame) {
      return frame.top === frame;
    }
    return true;
  }

  function defaultTrustedIpcEvent(event) {
    const mainWindow = getMainWindow();
    if (
      !mainWindow ||
      mainWindow.isDestroyed() ||
      event?.sender !== mainWindow.webContents
    ) {
      return false;
    }
    if (!isMainFrame(event.senderFrame)) {
      return false;
    }
    const origin = originOf(
      event.senderFrame?.url || event.sender?.getURL?.() || "",
    );
    return Boolean(origin && getAllowedOrigins().includes(origin));
  }

  function trusted(event) {
    return rendererGuard.isTrusted(event);
  }

  const rendererGuard = createRendererIpcGuard(isTrustedIpcEvent || defaultTrustedIpcEvent);
  ipcMain = rendererGuard.ipcMain(ipcMain);
  dialog = rendererGuard.operations(dialog);
  shell = rendererGuard.operations(shell);
  clipboard = rendererGuard.operations(clipboard);
  nativeImage = rendererGuard.operations(nativeImage);
  app = rendererGuard.operations(app);
  workspaceBookmarkStore = rendererGuard.operations(workspaceBookmarkStore);
  systemBehaviorController = rendererGuard.operations(systemBehaviorController);
  systemPreferences = rendererGuard.operations(systemPreferences);
  fetchImpl = rendererGuard.operation(fetchImpl);
  execFileImpl = rendererGuard.operation(execFileImpl);
  const cleanupFs = fsImpl;
  const guardedFsPromises = rendererGuard.operations(fsImpl.promises);
  const guardedFs = rendererGuard.operations(fsImpl);
  fsImpl = new Proxy(guardedFs, { get(target, key) {
    return key === "promises" ? guardedFsPromises : Reflect.get(target, key);
  } });

  function untrustedResult(shape) {
    if (shape === "nullable") {
      return null;
    }
    if (shape === "success") {
      return { success: false, error: "untrusted_ipc_sender" };
    }
    return { ok: false, error: "untrusted_ipc_sender" };
  }

  ipcMain.handle(
    "lantai:upload-chat-attachment",
    async (event, payload = {}) => {
      if (!trusted(event)) {
        return untrustedResult("ok");
      }
      return uploadChatAttachmentThroughBackend({
        payload,
        fetchImpl,
        getApiBaseUrl,
        getDesktopToken,
        getBackendWorkingDir,
      });
    },
  );

  ipcMain.handle("lantai:select-directory", async (event) => {
    if (!trusted(event)) {
      return untrustedResult("nullable");
    }
    const mainWindow = getMainWindow();
    if (!mainWindow || mainWindow.isDestroyed()) {
      return null;
    }
    const result = await dialog.showOpenDialog(mainWindow, {
      properties: ["openDirectory", "createDirectory"],
    });
    if (result.canceled || result.filePaths.length === 0) {
      return null;
    }
    return result.filePaths[0];
  });

  ipcMain.handle(
    "lantai:select-workspace-grant",
    async (event, payload = {}) => {
      if (!trusted(event)) {
        return untrustedResult("ok");
      }
      const mainWindow = getMainWindow();
      if (!mainWindow || mainWindow.isDestroyed()) {
        return { ok: false, error: "Main window is unavailable." };
      }
      const agentId = String(payload.agentId || "").trim();
      const accessMode = String(payload.accessMode || "read_write");
      const trustState = String(payload.trustState || "trusted");
      if (!/^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/.test(agentId)) {
        return { ok: false, error: "Invalid agent ID." };
      }
      if (!["read_only", "read_write"].includes(accessMode)) {
        return { ok: false, error: "Invalid workspace access mode." };
      }
      if (!["trusted", "restricted"].includes(trustState)) {
        return { ok: false, error: "Invalid workspace trust state." };
      }
      const isMas = ["app_store", "mas"].includes(distributionChannel);
      const result = await dialog.showOpenDialog(mainWindow, {
        properties: ["openDirectory", "createDirectory"],
        securityScopedBookmarks: isMas ? true : undefined,
      });
      if (result.canceled || !result.filePaths?.length) {
        return null;
      }
      const desktopToken = String(getDesktopToken() || "");
      if (!desktopToken) {
        return {
          ok: false,
          error: "Desktop authentication is unavailable.",
          error_code: "desktop_auth_unavailable",
        };
      }
      const projectRoot = pathImpl.resolve(result.filePaths[0]);
      let securityScopeRef = null;
      if (isMas) {
        const bookmark = String(result.bookmarks?.[0] || "");
        if (!bookmark) {
          return {
            ok: false,
            error: "The selected folder did not provide a security bookmark.",
            error_code: "mas_security_scoped_bookmark_missing",
          };
        }
        if (
          !workspaceBookmarkStore ||
          typeof workspaceBookmarkStore.authorizeSelection !== "function"
        ) {
          return {
            ok: false,
            error: "Persistent workspace authorization is unavailable.",
            error_code: "mas_security_scope_secure_store_unavailable",
          };
        }
        const authorization = workspaceBookmarkStore.authorizeSelection({
          agentId,
          projectRoot,
          bookmark,
        });
        if (!authorization?.ok || !authorization.securityScopeRef) {
          return {
            ok: false,
            error:
              authorization?.error ||
              "The selected folder could not be authorized.",
            error_code:
              authorization?.error_code ||
              "mas_security_scope_secure_store_failed",
          };
        }
        securityScopeRef = authorization.securityScopeRef;
      }
      const receiptPayload = {
        access_mode: accessMode,
        agent_id: agentId,
        distribution: isMas ? "mas" : "official",
        issued_at: now().toISOString(),
        nonce: randomId(),
        project_root: projectRoot,
        security_scope_ref: securityScopeRef,
        trust_state: trustState,
      };
      const payloadSegment = Buffer.from(
        JSON.stringify(receiptPayload),
        "utf8",
      ).toString("base64url");
      const signature = crypto
        .createHmac("sha256", desktopToken)
        .update(payloadSegment)
        .digest("hex");
      return {
        ok: true,
        selectionReceipt: `${payloadSegment}.${signature}`,
        projectRoot,
        displayName: pathImpl.basename(projectRoot) || projectRoot,
        accessMode,
        trustState,
      };
    },
  );

  ipcMain.handle("lantai:restore-workspace", async (event, payload = {}) => {
    if (!trusted(event)) {
      return untrustedResult("ok");
    }
    const mainWindow = getMainWindow();
    if (!mainWindow || mainWindow.isDestroyed()) {
      return { ok: false, error: "Main window is unavailable." };
    }
    const agentId = String(payload.agentId || "").trim();
    const expectedPath = String(payload.expectedPath || "").trim();
    if (!/^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/.test(agentId)) {
      return { ok: false, error: "Invalid agent ID." };
    }
    if (!expectedPath || !pathImpl.isAbsolute(expectedPath)) {
      return { ok: false, error: "Expected workspace path is invalid." };
    }

    const isMas = ["app_store", "mas"].includes(distributionChannel);
    const result = await dialog.showOpenDialog(mainWindow, {
      defaultPath: expectedPath,
      properties: ["openDirectory"],
      securityScopedBookmarks: isMas ? true : undefined,
    });
    if (result.canceled || !result.filePaths?.length) {
      return null;
    }

    const selectedPath = pathImpl.resolve(result.filePaths[0]);
    if (selectedPath !== pathImpl.resolve(expectedPath)) {
      return {
        ok: false,
        error: "Select the workspace folder shown in the recovery prompt.",
        error_code: "workspace_recovery_path_mismatch",
      };
    }

    let stopAccessing = null;
    try {
      if (isMas) {
        const bookmark = String(result.bookmarks?.[0] || "");
        if (!bookmark) {
          return {
            ok: false,
            error: "The selected folder did not provide a security bookmark.",
            error_code: "mas_security_scoped_bookmark_missing",
          };
        }
        if (typeof app?.startAccessingSecurityScopedResource !== "function") {
          return {
            ok: false,
            error: "Security-scoped workspace access is unavailable.",
            error_code: "mas_security_scope_start_unavailable",
          };
        }
        stopAccessing = app.startAccessingSecurityScopedResource(bookmark);
        if (typeof stopAccessing !== "function") {
          return {
            ok: false,
            error: "The selected folder could not be authorized.",
            error_code: "mas_security_scope_start_failed",
          };
        }
      }

      const agentConfigPath = pathImpl.join(selectedPath, "agent.json");
      const parsed = JSON.parse(
        await fsImpl.promises.readFile(agentConfigPath, "utf8"),
      );
      if (String(parsed?.id || "") !== agentId) {
        return {
          ok: false,
          error: "The selected folder belongs to another agent.",
          error_code: "workspace_recovery_agent_mismatch",
        };
      }

      const importRoot = pathImpl.join(
        app.getPath("userData"),
        "working",
        "workspace-imports",
      );
      await fsImpl.promises.mkdir(importRoot, { recursive: true });
      const stagedPath = pathImpl.join(
        importRoot,
        `${agentId}-${Date.now()}-${randomId()}`,
      );
      try {
        await fsImpl.promises.cp(selectedPath, stagedPath, {
          recursive: true,
          preserveTimestamps: true,
          errorOnExist: true,
        });
      } catch (error) {
        await cleanupFs.promises.rm(stagedPath, {
          recursive: true,
          force: true,
        });
        throw error;
      }
      return {
        ok: true,
        stagedPath,
        sourcePath: selectedPath,
      };
    } catch (error) {
      return {
        ok: false,
        error: error?.message || "Workspace recovery copy failed.",
        error_code: "workspace_recovery_copy_failed",
      };
    } finally {
      if (typeof stopAccessing === "function") {
        try {
          stopAccessing();
        } catch {
          // Best-effort release after the managed copy is complete.
        }
      }
    }
  });

  ipcMain.handle("lantai:select-file", async (event) => {
    if (!trusted(event)) {
      return untrustedResult("nullable");
    }
    const mainWindow = getMainWindow();
    if (!mainWindow || mainWindow.isDestroyed()) {
      return null;
    }
    const result = await dialog.showOpenDialog(mainWindow, {
      properties: ["openFile"],
    });
    if (result.canceled || result.filePaths.length === 0) {
      return null;
    }
    return result.filePaths[0];
  });

  ipcMain.handle(
    "lantai:read-clipboard-attachments",
    async (event, payload = {}) => {
      if (!trusted(event)) {
        return {
          attachments: [],
          error: "untrusted_ipc_sender",
          error_code: "untrusted_ipc_sender",
        };
      }
      return readNativeClipboardAttachments({
        clipboard,
        payload,
        fsImpl,
        pathImpl,
      });
    },
  );

  ipcMain.handle("lantai:copy-image-from-url", async (event, payload = {}) => {
    if (!trusted(event)) {
      return untrustedResult("success");
    }
    if (
      !nativeImage ||
      typeof nativeImage.createFromBuffer !== "function" ||
      !clipboard ||
      typeof clipboard.writeImage !== "function"
    ) {
      return {
        success: false,
        error: "Native image clipboard is unavailable.",
        error_code: "image_clipboard_unavailable",
      };
    }
    try {
      const { bytes } = await loadGeneratedImage({
        localPaths: payload.localPaths,
        localPath: payload.localPath,
        url: payload.url,
        app,
        fsImpl,
        pathImpl,
        fetchImpl,
        getApiBaseUrl,
        getDesktopToken,
        getBackendWorkingDir,
      });
      const image = nativeImage.createFromBuffer(bytes);
      if (!image || (typeof image.isEmpty === "function" && image.isEmpty())) {
        throw new Error("Downloaded data is not a supported image.");
      }
      clipboard.writeImage(image);
      return { success: true };
    } catch (error) {
      return {
        success: false,
        error: error?.message || "Unable to copy generated image.",
        error_code: "copy_generated_image_failed",
      };
    }
  });

  ipcMain.handle("lantai:save-image-from-url", async (event, payload = {}) => {
    if (!trusted(event)) {
      return untrustedResult("success");
    }
    if (!dialog || typeof dialog.showSaveDialog !== "function") {
      return {
        success: false,
        error: "Native save dialog is unavailable.",
        error_code: "image_save_dialog_unavailable",
      };
    }
    const mainWindow = getMainWindow();
    if (!mainWindow || mainWindow.isDestroyed()) {
      return { success: false, error: "Main window is unavailable." };
    }
    try {
      const image = await loadGeneratedImage({
        localPaths: payload.localPaths,
        localPath: payload.localPath,
        url: payload.url,
        app,
        fsImpl,
        pathImpl,
        fetchImpl,
        getApiBaseUrl,
        getDesktopToken,
        getBackendWorkingDir,
      });
      const name = generatedImageFileName(image.sourceUrl, image.mimeType);
      const downloadsDirectory =
        typeof app?.getPath === "function" ? app.getPath("downloads") : "";
      const result = await dialog.showSaveDialog(mainWindow, {
        title: "保存生成图片",
        defaultPath: pathImpl.join(downloadsDirectory, name),
        filters: [
          {
            name: "图片",
            extensions: GENERATED_IMAGE_FILTER_EXTENSIONS[image.mimeType] || [
              GENERATED_IMAGE_EXTENSION_BY_MIME[image.mimeType] || "png",
            ],
          },
        ],
      });
      if (result.canceled || !result.filePath) {
        return { success: false, canceled: true };
      }
      await fsImpl.promises.writeFile(result.filePath, image.bytes);
      return { success: true, path: result.filePath };
    } catch (error) {
      return {
        success: false,
        error: error?.message || "Unable to save generated image.",
        error_code: "save_generated_image_failed",
      };
    }
  });

  ipcMain.handle("lantai:save-file-as", async (event, payload = {}) => {
    if (!trusted(event)) {
      return untrustedResult("success");
    }
    if (!dialog || typeof dialog.showSaveDialog !== "function") {
      return {
        success: false,
        error: "Native save dialog is unavailable.",
        error_code: "file_save_dialog_unavailable",
      };
    }
    const mainWindow = getMainWindow();
    if (!mainWindow || mainWindow.isDestroyed()) {
      return { success: false, error: "Main window is unavailable." };
    }
    try {
      const rawSource = String(payload.sourcePath || "").trim();
      if (!rawSource) {
        return {
          success: false,
          error: "Source file path is empty.",
          error_code: "file_source_empty",
        };
      }
      const sourcePath = await fsImpl.promises.realpath(rawSource);
      const stat = await fsImpl.promises.stat(sourcePath);
      if (!stat.isFile()) {
        return {
          success: false,
          error: "Source file is unavailable.",
          error_code: "file_source_unavailable",
        };
      }
      const name = safeClipboardName(
        payload.filename || pathImpl.basename(sourcePath),
        "lantai-deliverable",
      );
      const downloadsDirectory =
        typeof app?.getPath === "function" ? app.getPath("downloads") : "";
      const extension = pathImpl.extname(name).slice(1).toLowerCase();
      const result = await dialog.showSaveDialog(mainWindow, {
        title: "保存 Agent 交付文件",
        defaultPath: pathImpl.join(downloadsDirectory, name),
        ...(extension
          ? { filters: [{ name: "文件", extensions: [extension] }] }
          : {}),
      });
      if (result.canceled || !result.filePath) {
        return { success: false, canceled: true };
      }
      const destinationPath = pathImpl.resolve(result.filePath);
      if (destinationPath !== pathImpl.resolve(sourcePath)) {
        await fsImpl.promises.copyFile(sourcePath, destinationPath);
      }
      return {
        success: true,
        path: destinationPath,
        size_bytes: Number(stat.size || 0),
      };
    } catch (error) {
      return {
        success: false,
        error: error?.message || "Unable to save generated file.",
        error_code: "save_generated_file_failed",
      };
    }
  });

  ipcMain.handle("lantai:open-path", async (event, targetPath) => {
    if (!trusted(event)) {
      return untrustedResult("success");
    }
    if (typeof targetPath !== "string" || !targetPath.trim()) {
      return { success: false, error: "Path is empty" };
    }
    const error = await shell.openPath(targetPath);
    return error ? { success: false, error } : { success: true };
  });

  ipcMain.handle("lantai:open-external", async (event, rawUrl) => {
    if (!trusted(event)) {
      return untrustedResult("success");
    }
    let url;
    try {
      url = new URL(String(rawUrl || ""));
    } catch {
      return { success: false, error: "Link is invalid" };
    }
    if (url.protocol !== "https:" && url.protocol !== "http:") {
      return {
        success: false,
        error: "Only HTTP and HTTPS links can be opened",
      };
    }
    try {
      await shell.openExternal(url.toString());
      return { success: true };
    } catch (error) {
      return {
        success: false,
        error: error?.message || "Unable to open link",
      };
    }
  });

  ipcMain.handle(
    "lantai:open-html-work",
    async (event, targetPath, title = "") => {
      if (!trusted(event)) {
        return untrustedResult("success");
      }
      if (!BrowserWindow) {
        return { success: false, error: "Works window is unavailable" };
      }
      if (typeof targetPath !== "string" || !/\.html?$/i.test(targetPath)) {
        return { success: false, error: "Work must be an HTML file" };
      }
      let resolvedPath = "";
      try {
        resolvedPath = await fsImpl.promises.realpath(targetPath);
        const stat = await fsImpl.promises.stat(resolvedPath);
        if (!stat.isFile()) {
          return { success: false, error: "Work file is unavailable" };
        }
      } catch (error) {
        return {
          success: false,
          error: error?.message || "Work file is unavailable",
        };
      }

      const parent = getMainWindow();
      rendererGuard.assertCurrent();
      const workWindow = new BrowserWindow({
        width: 1120,
        height: 760,
        minWidth: 640,
        minHeight: 480,
        title: String(title || pathImpl.basename(resolvedPath)),
        parent: parent && !parent.isDestroyed() ? parent : undefined,
        show: false,
        backgroundColor: "#f5f5f7",
        webPreferences: {
          nodeIntegration: false,
          contextIsolation: true,
          sandbox: true,
          webSecurity: true,
          allowRunningInsecureContent: false,
        },
      });
      workWindow.webContents.setWindowOpenHandler(() => ({ action: "deny" }));
      workWindow.webContents.on("will-navigate", (navigationEvent, url) => {
        if (!String(url || "").startsWith("file:")) {
          navigationEvent.preventDefault();
        }
      });
      try {
        await workWindow.loadFile(resolvedPath);
        rendererGuard.assertCurrent();
        if (!workWindow.isDestroyed()) workWindow.show();
        return { success: true };
      } catch (error) {
        if (!workWindow.isDestroyed()) workWindow.destroy();
        return {
          success: false,
          error: error?.message || "Unable to load HTML work",
        };
      }
    },
  );

  ipcMain.handle("lantai:create-work-thumbnail", async (event, targetPath) => {
    if (!trusted(event)) {
      return untrustedResult("success");
    }
    if (
      !nativeImage ||
      typeof nativeImage.createThumbnailFromPath !== "function"
    ) {
      return {
        success: false,
        error: "Native work thumbnails are unavailable",
      };
    }
    if (typeof targetPath !== "string" || !targetPath.trim()) {
      return { success: false, error: "Work path is empty" };
    }
    try {
      const resolvedPath = await fsImpl.promises.realpath(targetPath.trim());
      const stat = await fsImpl.promises.stat(resolvedPath);
      if (!stat.isFile() || stat.size > 100 * 1024 * 1024) {
        return { success: false, error: "Work cannot be thumbnailed" };
      }
      const thumbnail = await nativeImage.createThumbnailFromPath(
        resolvedPath,
        {
          width: 960,
          height: 600,
        },
      );
      if (!thumbnail || thumbnail.isEmpty?.()) {
        return { success: false, error: "No native thumbnail is available" };
      }
      return { success: true, data_url: thumbnail.toDataURL() };
    } catch (error) {
      return {
        success: false,
        error: error?.message || "Unable to create work thumbnail",
      };
    }
  });

  ipcMain.handle("lantai:reveal-path", async (event, targetPath) => {
    if (!trusted(event)) {
      return untrustedResult("success");
    }
    if (typeof targetPath !== "string" || !targetPath.trim()) {
      return { success: false, error: "Path is empty" };
    }
    if (!shell || typeof shell.showItemInFolder !== "function") {
      return { success: false, error: "Finder reveal is unavailable" };
    }
    try {
      shell.showItemInFolder(targetPath);
      return { success: true };
    } catch (error) {
      return {
        success: false,
        error: error?.message || "Unable to reveal path",
      };
    }
  });

  ipcMain.handle("lantai:open-with", async (event, targetPath) => {
    if (!trusted(event)) {
      return untrustedResult("success");
    }
    if (typeof targetPath !== "string" || !targetPath.trim()) {
      return { success: false, error: "Path is empty" };
    }
    if (platform !== "darwin") {
      return {
        success: false,
        error: "Choosing an application is currently available on macOS.",
        error_code: "open_with_unsupported_platform",
      };
    }
    const isMas = ["app_store", "appstore", "mas", "testflight"].includes(
      String(distributionChannel || "").toLowerCase(),
    );
    if (isMas) {
      try {
        shell.showItemInFolder(targetPath);
      } catch {
        // The caller still receives the distribution limitation below.
      }
      return {
        success: false,
        error: "App Store 版本已在 Finder 中显示文件，请使用系统“打开方式”。",
        error_code: "open_with_requires_finder",
        revealed: true,
      };
    }
    const mainWindow = getMainWindow();
    if (!mainWindow || mainWindow.isDestroyed()) {
      return { success: false, error: "Main window is unavailable." };
    }
    const result = await dialog.showOpenDialog(mainWindow, {
      title: "选择用于打开文件的应用",
      defaultPath: "/Applications",
      properties: ["openFile"],
      filters: [{ name: "应用程序", extensions: ["app"] }],
    });
    if (result.canceled || !result.filePaths?.length) {
      return { success: false, canceled: true };
    }
    const applicationPath = String(result.filePaths[0] || "");
    if (!applicationPath.toLowerCase().endsWith(".app")) {
      return {
        success: false,
        error: "请选择一个 macOS 应用程序。",
        error_code: "open_with_application_required",
      };
    }
    return new Promise((resolve) => {
      execFileImpl(
        "/usr/bin/open",
        ["-a", applicationPath, targetPath],
        (error) => {
          resolve(
            error
              ? {
                  success: false,
                  error: error.message || "Unable to open with application",
                }
              : { success: true },
          );
        },
      );
    });
  });

  ipcMain.handle("lantai:app-version", async (event) => {
    if (!trusted(event)) {
      return untrustedResult("ok");
    }
    return { ok: true, version: String(app?.getVersion?.() || "") };
  });

  ipcMain.handle("lantai:computer-permissions-status", async (event) => {
    if (!trusted(event)) {
      return untrustedResult("ok");
    }
    return getNativeComputerPermissionStatus({
      app,
      systemPreferences,
      platform,
      pathImpl,
    });
  });

  ipcMain.handle("lantai:computer-permissions-request", async (event, key) => {
    if (!trusted(event)) {
      return untrustedResult("ok");
    }
    if (platform !== "darwin") {
      return {
        ok: false,
        key,
        status: "unsupported",
        error: "Computer permissions are only available on macOS.",
      };
    }
    return {
      ok: false,
      key,
      status: "unknown",
      error: "This permission must be enabled in System Settings.",
    };
  });

  ipcMain.handle("lantai:system-behavior-get", async (event) => {
    if (!trusted(event)) {
      return untrustedResult("ok");
    }
    if (!systemBehaviorController) {
      return { ok: false, error: "System behavior is unavailable." };
    }
    return {
      ok: true,
      preferences: systemBehaviorController.getPreferences(),
    };
  });

  ipcMain.handle(
    "lantai:system-behavior-update",
    async (event, payload = {}) => {
      if (!trusted(event)) {
        return untrustedResult("ok");
      }
      if (!systemBehaviorController) {
        return { ok: false, error: "System behavior is unavailable." };
      }
      return systemBehaviorController.updatePreferences(payload);
    },
  );

  ipcMain.handle(
    "lantai:system-behavior-task-activity",
    async (event, payload = {}) => {
      if (!trusted(event)) {
        return untrustedResult("ok");
      }
      if (!systemBehaviorController) {
        return { ok: false, error: "System behavior is unavailable." };
      }
      return systemBehaviorController.setTaskActivity(payload);
    },
  );

  ipcMain.handle(
    "lantai:system-behavior-notify",
    async (event, payload = {}) => {
      if (!trusted(event)) {
        return untrustedResult("ok");
      }
      if (!systemBehaviorController) {
        return { ok: false, error: "System behavior is unavailable." };
      }
      return systemBehaviorController.notify(payload);
    },
  );
}

module.exports = {
  chatAttachmentBuffer,
  collectClipboardFilePaths,
  findMacAppBundlePath,
  getNativeComputerPermissionStatus,
  installDesktopIpcHandlers,
  mapNativeMediaPermissionStatus,
  readNativeClipboardAttachments,
  uploadChatAttachmentThroughBackend,
};
