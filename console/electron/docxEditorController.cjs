const fs = require("node:fs");
const path = require("node:path");
const { randomUUID } = require("node:crypto");
const { captureRendererDocument, sameRendererDocument, rendererPathForUrl,
  captureRendererRequest, isCurrentRendererRequest } = require("./rendererDocumentTrust.cjs");
const { normalizeConsoleMessage } = require("./consoleMessageDetails.cjs");

const DOCX_MAX_BYTES = 150 * 1024 * 1024;
const DOCX_EDITOR_CHANNEL = "lantai:docx-editor-open";
const DOCX_EDITOR_REWRITE_CHANNEL = "lantai:docx-editor-rewrite";
const DOCX_EDITOR_REWRITE_REQUEST_CHANNEL =
  "lantai:docx-editor-rewrite-request";
const DOCX_EDITOR_REWRITE_RESPONSE_CHANNEL =
  "lantai:docx-editor-rewrite-response";
const DOCX_EDITOR_REWRITE_TIMEOUT_MS = 120 * 1000;

function resolveDocxEditorRuntimePaths({ isDev, electronDir, resourcesPath }) {
  const runtimeRoot = isDev
    ? path.resolve(
        electronDir,
        "..",
        "vendor",
        "lantai-docx-editor",
        "apps",
        "docs",
        "out",
      )
    : path.join(resourcesPath, "lantai-docx-editor");
  return {
    modulePath: path.join(runtimeRoot, "main", "lantai.js"),
    preloadPath: path.join(runtimeRoot, "preload", "index.js"),
    rendererFile: path.join(runtimeRoot, "renderer", "index.html"),
  };
}

function errorResult(error, errorCode) {
  return { success: false, error, error_code: errorCode };
}

function createDocxEditorController({
  ipcMain,
  isDev = false,
  electronDir = __dirname,
  resourcesPath = process.resourcesPath,
  fsImpl = fs,
  isTrustedIpcEvent = () => false,
  getMainWindow = () => null,
  appendLog = () => {},
  installOfficeAiHandlers = () => {},
  registerOfficeAiEditorWindow = () => {},
  presentOfficeView = async () =>
    errorResult("Office 工作台不可用", "office_workbench_unavailable"),
  resolveOfficeModelBinding = async () => null,
  resolveRuntimePaths = () =>
    resolveDocxEditorRuntimePaths({ isDev, electronDir, resourcesPath }),
  loadEditorRuntime = (modulePath) => require(modulePath),
}) {
  let runtime = null;
  const editorOwners = new Map();
  const mainOwners = new WeakMap();
  const pendingRewriteRequests = new Map();

  function settleRewrite(requestId, result) {
    const pending = pendingRewriteRequests.get(requestId);
    if (!pending) return;
    pendingRewriteRequests.delete(requestId);
    clearTimeout(pending.timeout);
    pending.resolve(result);
  }

  function retireOwner(owner, errorCode, blockCurrentDocument = true) {
    owner.epoch += 1;
    if (blockCurrentDocument) owner.retiredDocument = captureRendererDocument(owner.webContents, owner.webContents.mainFrame);
    for (const [requestId, pending] of pendingRewriteRequests) {
      if (pending.editorOwner === owner || pending.mainOwner === owner) {
        settleRewrite(requestId, errorResult("页面已改变，请重新发起改写", errorCode));
      }
    }
  }

  function watchOwner(owner) {
    const webContents = owner.webContents;
    webContents.on?.("did-start-navigation", (_event, _url, isInPlace, isMainFrame) => {
      if (isMainFrame && !isInPlace) retireOwner(owner, "docx_rewrite_document_changed");
    });
    webContents.on?.("render-process-gone", () => retireOwner(owner, "docx_rewrite_document_changed"));
    webContents.once?.("destroyed", () => {
      retireOwner(owner, "docx_rewrite_editor_closed");
      if (editorOwners.get(webContents.id) === owner) editorOwners.delete(webContents.id);
    });
  }

  function ownerDocument(owner, frame) {
    const document = captureRendererDocument(owner.webContents, frame,
      owner.rendererPath ? (url) => rendererPathForUrl(url) === owner.rendererPath : () => true);
    if (!document || sameRendererDocument(document, owner.retiredDocument)) return null;
    return { ...document, epoch: owner.epoch };
  }

  function isCurrentOwnerDocument(owner, document, frame = owner.webContents.mainFrame) {
    const current = ownerDocument(owner, frame);
    return Boolean(current && current.epoch === document.epoch && sameRendererDocument(current, document));
  }

  function registerRewriteEditor(editorView, sourceChatId, targetPath, agentId) {
    const webContents = editorView?.webContents;
    if (!webContents || !Number.isInteger(webContents.id)) return;
    const rendererPath = path.resolve(resolveRuntimePaths().rendererFile);
    let owner = editorOwners.get(webContents.id);
    if (owner && (owner.webContents !== webContents || owner.rendererPath !== rendererPath)) {
      retireOwner(owner, "docx_rewrite_document_changed");
      owner = null;
    }
    if (!owner) {
      owner = { webContents, rendererPath, epoch: 0, sourceChatId, targetPath, agentId };
      editorOwners.set(webContents.id, owner);
      watchOwner(owner);
    } else if (owner.sourceChatId !== sourceChatId || owner.targetPath !== targetPath || owner.agentId !== agentId) {
      retireOwner(owner, "docx_rewrite_context_changed", false);
      Object.assign(owner, { sourceChatId, targetPath, agentId });
    }
  }

  function trustedRewriteDocument(event) {
    const owner = editorOwners.get(event?.sender?.id);
    if (!owner || owner.webContents !== event?.sender) return null;
    const document = ownerDocument(owner, event.senderFrame);
    return document ? { owner, document } : null;
  }

  function getMainDocument() {
    const window = getMainWindow();
    if (!window || window.isDestroyed()) return null;
    const webContents = window.webContents;
    const frame = webContents?.mainFrame;
    if (!isTrustedIpcEvent({ sender: webContents, senderFrame: frame })) return null;
    let owner = mainOwners.get(webContents);
    if (!owner) {
      owner = { webContents, epoch: 0 };
      mainOwners.set(webContents, owner);
      watchOwner(owner);
    }
    const document = ownerDocument(owner, frame);
    return document ? { owner, document } : null;
  }

  function prepareRuntime() {
    if (runtime) return runtime;
    const runtimePaths = resolveRuntimePaths();
    if (
      !runtimePaths ||
      !fsImpl.existsSync(runtimePaths.modulePath) ||
      !fsImpl.existsSync(runtimePaths.preloadPath) ||
      !fsImpl.existsSync(runtimePaths.rendererFile)
    ) {
      throw Object.assign(new Error("澜台文档编辑器还没有构建"), {
        code: "docx_editor_runtime_missing",
      });
    }
    const loaded = loadEditorRuntime(runtimePaths.modulePath);
    const requiredExports = [
      "configureDocsRuntime",
      "setDocsShellWindow",
      "registerDocsIpc",
      "registerProjectIpc",
      "createDocsView",
    ];
    if (requiredExports.some((name) => typeof loaded?.[name] !== "function")) {
      throw Object.assign(new Error("澜台文档编辑器构建不完整"), {
        code: "docx_editor_runtime_invalid",
      });
    }
    loaded.configureDocsRuntime({
      preloadPath: runtimePaths.preloadPath,
      rendererFile: runtimePaths.rendererFile,
    });
    loaded.setDocsShellWindow(getMainWindow());
    loaded.registerProjectIpc();
    loaded.registerDocsIpc();
    runtime = loaded;
    return runtime;
  }

  function validateTarget(targetPath) {
    if (typeof targetPath !== "string" || !targetPath.trim()) {
      return errorResult("DOCX 文件路径为空", "docx_path_empty");
    }
    if (!/\.docx$/i.test(targetPath.trim())) {
      return errorResult("澜台文档目前仅支持 DOCX", "docx_type_unsupported");
    }
    let canonicalPath;
    let stat;
    try {
      canonicalPath = fsImpl.realpathSync(targetPath.trim());
      stat = fsImpl.statSync(canonicalPath);
    } catch {
      return errorResult("DOCX 文件不存在或无法读取", "docx_unreadable");
    }
    if (!stat.isFile()) {
      return errorResult("DOCX 路径不是文件", "docx_not_file");
    }
    if (stat.size > DOCX_MAX_BYTES) {
      return errorResult("DOCX 文件不能超过 150 MB", "docx_too_large");
    }
    return { success: true, path: canonicalPath };
  }

  async function openLocalDocx(targetInput, isCurrentCaller = () => true) {
    const targetPath =
      typeof targetInput === "string" ? targetInput : targetInput?.path;
    const sourceChatId =
      typeof targetInput === "object" && targetInput
        ? String(targetInput.source_chat_id || "")
            .trim()
            .slice(0, 200)
        : "";
    const agentId =
      typeof targetInput === "object" && targetInput
        ? String(targetInput.agent_id || "")
            .trim()
            .slice(0, 200) || "default"
        : "default";
    let modelBinding = null;
    try {
      modelBinding = await resolveOfficeModelBinding(agentId);
    } catch (error) {
      appendLog(
        `[DocxEditor] model binding unavailable for ${agentId}: ${error instanceof Error ? error.message : String(error)}`,
      );
    }
    const requireCaller = () => {
      if (!isCurrentCaller()) throw Object.assign(new Error("页面已改变，请重新打开编辑器"), {
        code: "docx_document_changed",
      });
    };
    if (!isCurrentCaller()) return errorResult("页面已改变，请重新打开编辑器", "docx_document_changed");
    const target = validateTarget(targetPath);
    if (!target.success) return target;
    const mainWindow = getMainWindow();
    if (!mainWindow || mainWindow.isDestroyed()) {
      return errorResult("澜台主窗口不可用", "docx_main_window_unavailable");
    }
    try {
      const editor = prepareRuntime();
      editor.setDocsShellWindow(mainWindow);
      const editorContext = {
        kind: "docs",
        agent_id: agentId,
        source_chat_id: sourceChatId,
        ...(modelBinding ? { model_binding: modelBinding } : {}),
      };
      return await presentOfficeView({
        path: target.path,
        kind: "docs",
        source_chat_id: sourceChatId,
        createView: async () => {
          requireCaller();
          const editorView = await editor.createDocsView(target.path);
          if (!isCurrentCaller()) {
            editorView?.webContents?.destroy?.();
            requireCaller();
          }
          registerRewriteEditor(editorView, sourceChatId, target.path, agentId);
          registerOfficeAiEditorWindow(editorView, editorContext);
          installOfficeAiHandlers();
          return editorView;
        },
        onActivate: async (editorView, { reused } = {}) => {
          requireCaller();
          const webContents = editorView?.webContents;
          if (!reused) {
            webContents?.on?.(
              "did-fail-load",
              (_event, code, description, url) => {
                appendLog(
                  `[DocxEditor] did-fail-load code=${code} description=${description} url=${url} file=${target.path}`,
                );
              },
            );
            webContents?.on?.("render-process-gone", (_event, details = {}) => {
              appendLog(
                `[DocxEditor] render-process-gone reason=${details.reason || "unknown"} exitCode=${details.exitCode ?? "unknown"} file=${target.path}`,
              );
            });
            webContents?.on?.("console-message", (event, ...legacy) => {
              const { level, message, line, sourceId } =
                normalizeConsoleMessage(event, ...legacy);
              if (Number(level) < 2) return;
              appendLog(
                `[DocxEditor] renderer-console level=${level} message=${message} source=${sourceId || "unknown"}:${line || 0}`,
              );
            });
          }
          if (reused) {
            registerRewriteEditor(editorView, sourceChatId, target.path, agentId);
            registerOfficeAiEditorWindow(editorView, editorContext);
            installOfficeAiHandlers();
          }
          editor.installDocsMenu?.();
        },
      });
    } catch (reason) {
      return errorResult(
        reason instanceof Error ? reason.message : "澜台文档打开失败",
        reason?.code || "docx_editor_open_failed",
      );
    }
  }

  async function openFromIpc(event, targetInput) {
    const request = captureRendererRequest(event, isTrustedIpcEvent);
    if (!request) {
      return errorResult("未授权的文档编辑请求", "docx_untrusted_sender");
    }
    return await openLocalDocx(targetInput, () => isCurrentRendererRequest(request, event, isTrustedIpcEvent));
  }

  function normalizeRewritePayload(payload) {
    if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
      return errorResult("双写请求格式无效", "docx_rewrite_invalid");
    }
    const instruction = String(payload.instruction || "").trim();
    const selectedText = String(payload.selected_text || "");
    const documentBefore = String(payload.document_before || "");
    const documentAfter = String(payload.document_after || "");
    const currentDraft = String(payload.current_draft || "");
    const documentName = String(payload.document_name || "").trim();
    if (!instruction || instruction.length > 2000) {
      return errorResult(
        "修改要求不能为空且不能超过 2000 字",
        "docx_rewrite_instruction_invalid",
      );
    }
    if (!selectedText.trim() || selectedText.length > 20_000) {
      return errorResult(
        "请选择不超过 20000 字的正文",
        "docx_rewrite_selection_invalid",
      );
    }
    if (currentDraft.length > 50_000) {
      return errorResult(
        "候选稿不能超过 50000 字",
        "docx_rewrite_draft_invalid",
      );
    }
    return {
      success: true,
      instruction,
      selected_text: selectedText,
      document_before: documentBefore,
      document_after: documentAfter,
      current_draft: currentDraft,
      document_name: documentName.slice(0, 200),
    };
  }

  async function rewriteFromEditor(event, payload) {
    const editor = trustedRewriteDocument(event);
    if (!editor) return errorResult("未授权的文档双写请求", "docx_rewrite_untrusted_sender");
    const normalized = normalizeRewritePayload(payload);
    if (!normalized.success) return normalized;
    const main = getMainDocument();
    if (!main) return errorResult("澜台主窗口不可用", "docx_main_window_unavailable");
    const requestId = randomUUID();
    const result = new Promise((resolve) => {
      const timeout = setTimeout(() => settleRewrite(requestId,
        errorResult("沈行知响应超时", "docx_rewrite_timeout")), DOCX_EDITOR_REWRITE_TIMEOUT_MS);
      pendingRewriteRequests.set(requestId, {
        editorOwner: editor.owner, editorDocument: editor.document,
        mainOwner: main.owner, mainDocument: main.document, resolve, timeout,
      });
    });
    try {
      main.owner.webContents.send(DOCX_EDITOR_REWRITE_REQUEST_CHANNEL, {
        request_id: requestId,
        payload: { ...normalized, source_chat_id: editor.owner.sourceChatId },
      });
    } catch {
      settleRewrite(requestId, errorResult("澜台主窗口不可用", "docx_main_window_unavailable"));
    }
    return await result;
  }

  function handleRewriteResponse(event, envelope) {
    if (!isTrustedIpcEvent(event)) return;
    const requestId = String(envelope?.request_id || "");
    const pending = pendingRewriteRequests.get(requestId);
    if (!pending) return;
    if (event.sender !== pending.mainOwner.webContents ||
        !isCurrentOwnerDocument(pending.mainOwner, pending.mainDocument, event.senderFrame) ||
        editorOwners.get(pending.editorOwner.webContents.id) !== pending.editorOwner ||
        !isCurrentOwnerDocument(pending.editorOwner, pending.editorDocument)) {
      settleRewrite(requestId, errorResult("页面已改变，请重新发起改写", "docx_rewrite_document_changed"));
      return;
    }
    pendingRewriteRequests.delete(requestId);
    clearTimeout(pending.timeout);
    const result = envelope?.result;
    if (!result || typeof result !== "object" || Array.isArray(result)) {
      pending.resolve(
        errorResult("Agent 返回格式无效", "docx_rewrite_invalid_result"),
      );
      return;
    }
    if (result.success !== true) {
      pending.resolve(
        errorResult(
          String(result.error || "Agent 未完成改写"),
          String(result.error_code || "docx_rewrite_failed"),
        ),
      );
      return;
    }
    const replacementText = String(result.replacement_text || "");
    if (!replacementText.trim() || replacementText.length > 50_000) {
      pending.resolve(
        errorResult("Agent 返回的替换文本无效", "docx_rewrite_invalid_result"),
      );
      return;
    }
    pending.resolve({
      success: true,
      replacement_text: replacementText,
      summary: String(result.summary || "").slice(0, 500),
    });
  }

  function installIpcHandlers() {
    ipcMain.removeHandler(DOCX_EDITOR_CHANNEL);
    ipcMain.removeHandler(DOCX_EDITOR_REWRITE_CHANNEL);
    ipcMain.handle(DOCX_EDITOR_CHANNEL, openFromIpc);
    ipcMain.handle(DOCX_EDITOR_REWRITE_CHANNEL, rewriteFromEditor);
    ipcMain.removeListener(
      DOCX_EDITOR_REWRITE_RESPONSE_CHANNEL,
      handleRewriteResponse,
    );
    ipcMain.on(DOCX_EDITOR_REWRITE_RESPONSE_CHANNEL, handleRewriteResponse);
  }

  return {
    installIpcHandlers,
    openLocalDocx,
    openFromIpc,
    rewriteFromEditor,
    validateTarget,
  };
}

module.exports = {
  DOCX_EDITOR_CHANNEL,
  DOCX_EDITOR_REWRITE_CHANNEL,
  DOCX_EDITOR_REWRITE_REQUEST_CHANNEL,
  DOCX_EDITOR_REWRITE_RESPONSE_CHANNEL,
  DOCX_MAX_BYTES,
  createDocxEditorController,
  resolveDocxEditorRuntimePaths,
};
