const fs = require("node:fs");
const path = require("node:path");
const {
  rendererPathForUrl,
  captureRendererDocument,
  sameRendererDocument,
  captureRendererRequest,
  isCurrentRendererRequest,
} = require("./rendererDocumentTrust.cjs");
const { normalizeConsoleMessage } = require("./consoleMessageDetails.cjs");
const { resolveDocxEditorRuntimePaths } = require("./docxEditorController.cjs");

const OFFICE_EDITOR_CHANNEL = "lantai:office-editor-open";
const OFFICE_MAX_BYTES = 250 * 1024 * 1024;
const OFFICE_MAX_BYTES_BY_KIND = Object.freeze({
  sheets: 250 * 1024 * 1024,
  pdf: 250 * 1024 * 1024,
  slides: 150 * 1024 * 1024,
});
const OFFICE_FORMATS = Object.freeze({
  ".xlsx": "sheets",
  ".xls": "sheets",
  ".csv": "sheets",
  ".pptx": "slides",
  ".pdf": "pdf",
});

function resolveOfficeEditorRuntimePaths({
  isDev,
  electronDir,
  resourcesPath,
  platform = process.platform,
  arch = process.arch,
}) {
  const vendorRoot = path.resolve(
    electronDir,
    "..",
    "vendor",
    "lantai-docx-editor",
  );
  const generatedPdfVision = path.join(
    vendorRoot,
    "apps",
    "pdf",
    "generated-native",
    `${platform}-${arch}`,
    "lantai-pdf-vision",
  );
  const runtimeRoot = isDev
    ? (kind) => path.join(vendorRoot, "apps", kind, "out")
    : (kind) => path.join(resourcesPath, "lantai-office-editors", kind);
  const runtime = (kind) => ({
    modulePath: path.join(runtimeRoot(kind), "main", "lantai.js"),
    preloadPath: path.join(runtimeRoot(kind), "preload", "index.js"),
    rendererFile: path.join(runtimeRoot(kind), "renderer", "index.html"),
  });
  return {
    docs: resolveDocxEditorRuntimePaths({ isDev, electronDir, resourcesPath }),
    sheets: {
      ...runtime("sheets"),
      sidecarPath: isDev
        ? path.join(
            vendorRoot,
            "apps",
            "sheets",
            "native-bin",
            `${platform}-${arch}`,
            "xlsx-engine",
          )
        : path.join(
            resourcesPath,
            "lantai-office-editors",
            "sheets",
            "native",
            "xlsx-engine",
          ),
    },
    slides: runtime("slides"),
    pdf: {
      ...runtime("pdf"),
      visionOcrPath: isDev
        ? fs.existsSync(generatedPdfVision)
          ? generatedPdfVision
          : path.join(
              vendorRoot,
              "apps",
              "pdf",
              "native-bin",
              `${platform}-${arch}`,
              "lantai-pdf-vision",
            )
        : path.join(
            resourcesPath,
            "lantai-office-editors",
            "pdf",
            "native",
            "lantai-pdf-vision",
          ),
    },
  };
}

function errorResult(error, errorCode) {
  return { success: false, error, error_code: errorCode };
}

async function readOfficeAgentError(response) {
  const raw = String((await response.text()) || "").trim();
  if (!raw) return `Office Agent HTTP ${response.status}`;
  try {
    const parsed = JSON.parse(raw);
    if (typeof parsed?.detail === "string" && parsed.detail.trim()) {
      return parsed.detail.trim();
    }
    if (typeof parsed?.message === "string" && parsed.message.trim()) {
      return parsed.message.trim();
    }
  } catch {
    // Plain-text backends are still supported.
  }
  return raw;
}

function createOfficeEditorController({
  ipcMain,
  screen,
  isDev = false,
  electronDir = __dirname,
  resourcesPath = process.resourcesPath,
  fsImpl = fs,
  isTrustedIpcEvent = () => false,
  getMainWindow = () => null,
  getApiBaseUrl = () => "http://127.0.0.1:8000",
  getDesktopToken = () => "",
  fetchImpl = globalThis.fetch,
  resolveOfficeModelBinding,
  resolveOfficeModelOptions,
  presentOfficeView = async () =>
    errorResult("Office 工作台不可用", "office_workbench_unavailable"),
  appendLog = () => {},
  resolveRuntimePaths = () =>
    resolveOfficeEditorRuntimePaths({ isDev, electronDir, resourcesPath }),
  loadEditorRuntime = (modulePath) => require(modulePath),
}) {
  const runtimes = new Map();
  const editors = new Map();
  const agentIdsByEditor = new Map();
  const modelBindingsByEditor = new Map();
  const sourceChatIdsByEditor = new Map();
  const officeAiRequests = new Map();

  function normalizeAgentId(value) {
    const agentId = String(value || "").trim();
    return agentId && agentId.length <= 200 ? agentId : "default";
  }

  function normalizeModelBinding(value) {
    if (!value || typeof value !== "object") return null;
    const providerId = String(value.provider_id || "").trim();
    const model = String(value.model || "").trim();
    const source = String(value.source || "editor_snapshot").trim();
    if (
      !providerId ||
      !model ||
      providerId.length > 200 ||
      model.length > 300
    ) {
      return null;
    }
    return {
      provider_id: providerId,
      model,
      source: source.slice(0, 64) || "editor_snapshot",
    };
  }

  function normalizeModelOption(value) {
    const binding = normalizeModelBinding(value);
    if (!binding) return null;
    const label =
      String(value?.label || binding.model)
        .trim()
        .slice(0, 360) || binding.model;
    return { ...binding, label };
  }

  async function fetchOfficeModelBinding(agentId) {
    const response = await fetchImpl(
      `${getApiBaseUrl()}/api/models/active?scope=effective&agent_id=${encodeURIComponent(agentId)}`,
      {
        headers: {
          "X-Lantai-Desktop-Token": String(getDesktopToken() || ""),
        },
      },
    );
    if (!response?.ok) {
      throw new Error(
        `Office model binding HTTP ${response?.status || "unknown"}`,
      );
    }
    const payload = await response.json();
    return normalizeModelBinding({
      ...payload?.active_llm,
      source: agentId === "default" ? "default_snapshot" : "chat_snapshot",
    });
  }

  async function resolveModelBinding(agentId) {
    try {
      const binding = normalizeModelBinding(
        await (resolveOfficeModelBinding || fetchOfficeModelBinding)(agentId),
      );
      if (binding) return binding;
    } catch (error) {
      appendLog(
        `[OfficeEditor] model binding unavailable for ${agentId}: ${error instanceof Error ? error.message : String(error)}`,
      );
    }
    return null;
  }

  async function fetchOfficeModelOptions() {
    const response = await fetchImpl(`${getApiBaseUrl()}/api/models`, {
      headers: {
        "X-Lantai-Desktop-Token": String(getDesktopToken() || ""),
      },
    });
    if (!response?.ok) {
      throw new Error(
        `Office model options HTTP ${response?.status || "unknown"}`,
      );
    }
    const providers = await response.json();
    if (!Array.isArray(providers)) return [];
    const options = [];
    for (const provider of providers) {
      const providerId = String(provider?.id || "").trim();
      if (!providerId) continue;
      const models = [
        ...(Array.isArray(provider?.models) ? provider.models : []),
        ...(Array.isArray(provider?.extra_models) ? provider.extra_models : []),
      ];
      for (const modelInfo of models) {
        const option = normalizeModelOption({
          provider_id: providerId,
          model: modelInfo?.id,
          label: modelInfo?.name || modelInfo?.id,
        });
        if (option) options.push(option);
      }
    }
    return options;
  }

  async function resolveModelOptions() {
    try {
      const raw = await (
        resolveOfficeModelOptions || fetchOfficeModelOptions
      )();
      const unique = new Map();
      for (const value of Array.isArray(raw) ? raw : []) {
        const option = normalizeModelOption(value);
        if (option) unique.set(`${option.provider_id}:${option.model}`, option);
      }
      return [...unique.values()];
    } catch (error) {
      appendLog(
        `[OfficeEditor] model options unavailable: ${error instanceof Error ? error.message : String(error)}`,
      );
      return [];
    }
  }

  async function officeAiSettingsForEvent(event) {
    const document = trustedEditorDocument(event);
    if (!document) throw new Error("Untrusted Office AI sender.");
    const binding = modelBindingsByEditor.get(event?.sender?.id) || null;
    const options = await resolveModelOptions();
    requireCurrentEditorDocument(event, document);
    return {
      provider: "custom",
      providers: {
        custom: {
          apiKey: "lantai-local-bridge",
          model: binding?.model || "澜台默认模型",
        },
      },
      lantaiModelBinding: binding,
      lantaiModelOptions: options,
    };
  }

  function officeBindingPayloadForEvent(event) {
    return {
      agent_id: agentIdsByEditor.get(event?.sender?.id) || "default",
      ...(sourceChatIdsByEditor.get(event?.sender?.id)
        ? { source_chat_id: sourceChatIdsByEditor.get(event?.sender?.id) }
        : {}),
      ...(modelBindingsByEditor.get(event?.sender?.id)
        ? { model_binding: modelBindingsByEditor.get(event?.sender?.id) }
        : {}),
    };
  }

  async function postOfficeJson(pathname, body, signal) {
    const response = await fetchImpl(`${getApiBaseUrl()}${pathname}`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Lantai-Desktop-Token": String(getDesktopToken() || ""),
      },
      body: JSON.stringify(body),
      ...(signal ? { signal } : {}),
    });
    if (!response.ok) {
      const detail = await readOfficeAgentError(response);
      throw new Error(detail || `Office bridge HTTP ${response.status}`);
    }
    return await response.json();
  }

  async function streamOfficeAgent(body, signal, send) {
    const response = await fetchImpl(
      `${getApiBaseUrl()}/api/console/office-agent/stream`,
      {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-Lantai-Desktop-Token": String(getDesktopToken() || ""),
        },
        body: JSON.stringify(body),
        signal,
      },
    );
    signal.throwIfAborted();
    if (response?.status === 404 || response?.status === 405) return false;
    if (!response?.ok) {
      throw new Error(await readOfficeAgentError(response));
    }
    const reader = response.body?.getReader?.();
    if (!reader) return false;
    const decoder = new TextDecoder();
    const seenToolCalls = new Set();
    let buffer = "";
    let terminal = false;
    let eventCount = 0;

    const forwardLine = (line) => {
      if (!line.trim() || terminal) return;
      if (line.length > 1024 * 1024) {
        throw new Error("Office Agent 单条事件过大，已安全终止");
      }
      const chunk = JSON.parse(line);
      eventCount += 1;
      if (eventCount > 2000) {
        throw new Error("Office Agent 事件过多，已安全终止");
      }
      if (chunk?.type === "tool-call") {
        const call = chunk.toolCall;
        const key = `${String(call?.id || "")}:${String(call?.name || "")}`;
        if (!call?.name || seenToolCalls.has(key)) return;
        seenToolCalls.add(key);
        send({ type: "tool-call", toolCall: call });
      } else if (chunk?.type === "thinking-delta") {
        send({
          type: "thinking-delta",
          text: String(chunk.text || "").slice(0, 32_000),
        });
      } else if (chunk?.type === "delta") {
        send({ type: "delta", text: String(chunk.text || "") });
      } else if (chunk?.type === "ping") {
        send({ type: "ping" });
      } else if (chunk?.type === "done") {
        terminal = true;
        send({ type: "done", stopReason: String(chunk.stopReason || "") });
      } else if (chunk?.type === "error") {
        terminal = true;
        send({
          type: "error",
          error: String(chunk.error || "Office Agent 执行失败"),
        });
      }
    };

    try {
      while (!terminal) {
        signal.throwIfAborted();
        const { done, value } = await reader.read();
        signal.throwIfAborted();
        buffer += decoder.decode(value || new Uint8Array(), { stream: !done });
        if (buffer.length > 1024 * 1024) {
          throw new Error("Office Agent 流缓冲区过大，已安全终止");
        }
        let newline = buffer.indexOf("\n");
        while (newline >= 0 && !terminal) {
          forwardLine(buffer.slice(0, newline));
          buffer = buffer.slice(newline + 1);
          newline = buffer.indexOf("\n");
        }
        if (done) break;
      }
      if (!terminal && buffer.trim()) forwardLine(buffer);
      if (!terminal) throw new Error("Office Agent 流意外结束，请重试");
      return true;
    } finally {
      await reader.cancel?.();
    }
  }

  async function callOfficeTool(event, action, input = {}) {
    const document = trustedEditorDocument(event);
    if (!document) throw new Error("Untrusted Office AI sender.");
    const result = await postOfficeJson("/api/console/office-tool", {
      action,
      ...officeBindingPayloadForEvent(event),
      ...input,
    });
    requireCurrentEditorDocument(event, document);
    return result;
  }

  function retireEditorDocument(editor, blockCurrentDocument = true) {
    editor.epoch += 1;
    if (blockCurrentDocument) {
      editor.retiredDocument = captureRendererDocument(
        editor.webContents,
        editor.webContents.mainFrame,
        (url) => rendererPathForUrl(url) === editor.rendererPath,
      );
    }
    for (const request of officeAiRequests.values()) {
      if (request.document.editor === editor) request.controller.abort();
    }
  }

  function registerEditorWindow(editorWindow, context = {}) {
    const webContents = editorWindow?.webContents;
    const editorWebContentsId = webContents?.id;
    if (!editorWebContentsId) return;
    const runtimePaths = resolveRuntimePaths() || {};
    const initialPath = rendererPathForUrl(webContents.getURL?.());
    const rendererFile = context.kind
      ? runtimePaths[context.kind]?.rendererFile
      : Object.values(runtimePaths).find(
          (runtime) =>
            runtime?.rendererFile &&
            path.resolve(runtime.rendererFile) === initialPath,
        )?.rendererFile;
    if (!rendererFile) return;
    const rendererPath = path.resolve(rendererFile);
    const contextKey = JSON.stringify([
      normalizeAgentId(context.agent_id),
      normalizeModelBinding(context.model_binding),
      String(context.source_chat_id || "")
        .trim()
        .slice(0, 200),
    ]);
    let editor = editors.get(editorWebContentsId);
    if (
      editor &&
      (editor.webContents !== webContents ||
        editor.rendererPath !== rendererPath)
    ) {
      retireEditorDocument(editor);
      editor = null;
    }
    if (!editor) {
      editor = {
        webContents,
        rendererPath,
        epoch: 0,
        contextKey,
        retiredDocument: null,
      };
      editors.set(editorWebContentsId, editor);
      const registeredEditor = editor;
      webContents.setWindowOpenHandler?.(() => ({ action: "deny" }));
      const preventNavigation = (event, detailsOrUrl) => {
        const url =
          detailsOrUrl && typeof detailsOrUrl === "object"
            ? detailsOrUrl.url
            : (detailsOrUrl ?? event.url);
        if (rendererPathForUrl(url) !== registeredEditor.rendererPath)
          event.preventDefault();
      };
      for (const eventName of [
        "will-navigate",
        "will-redirect",
        "will-frame-navigate",
      ]) {
        webContents.on?.(eventName, preventNavigation);
      }
      webContents.on?.(
        "did-start-navigation",
        (_event, _url, isInPlace, isMainFrame) => {
          if (isMainFrame && !isInPlace) retireEditorDocument(registeredEditor);
        },
      );
      webContents.on?.("render-process-gone", () =>
        retireEditorDocument(registeredEditor),
      );
      webContents.once?.("destroyed", () => {
        retireEditorDocument(registeredEditor);
        if (editors.get(editorWebContentsId) !== registeredEditor) return;
        editors.delete(editorWebContentsId);
        agentIdsByEditor.delete(editorWebContentsId);
        modelBindingsByEditor.delete(editorWebContentsId);
        sourceChatIdsByEditor.delete(editorWebContentsId);
      });
    } else if (editor.contextKey !== contextKey) {
      retireEditorDocument(editor, false);
      editor.contextKey = contextKey;
    }
    agentIdsByEditor.set(
      editorWebContentsId,
      normalizeAgentId(context?.agent_id),
    );
    modelBindingsByEditor.set(
      editorWebContentsId,
      normalizeModelBinding(context?.model_binding),
    );
    sourceChatIdsByEditor.set(
      editorWebContentsId,
      String(context?.source_chat_id || "")
        .trim()
        .slice(0, 200),
    );
  }

  function trustedEditorDocument(event) {
    try {
      const sender = event?.sender;
      const editor = editors.get(sender?.id);
      if (!editor || editor.webContents !== sender || sender.isDestroyed?.())
        return null;
      const document = captureRendererDocument(
        sender,
        event.senderFrame,
        (url) => rendererPathForUrl(url) === editor.rendererPath,
      );
      return document && !sameRendererDocument(document, editor.retiredDocument)
        ? { ...document, editor, epoch: editor.epoch }
        : null;
    } catch {
      return null;
    }
  }

  function isEditorSender(event) {
    return Boolean(trustedEditorDocument(event));
  }

  function isCurrentEditorDocument(event, document) {
    const current = trustedEditorDocument(event);
    return Boolean(
      current &&
      current.editor === document.editor &&
      current.epoch === document.epoch &&
      current.url === document.url &&
      current.processId === document.processId &&
      current.routingId === document.routingId,
    );
  }

  function requireCurrentEditorDocument(event, document) {
    if (!isCurrentEditorDocument(event, document))
      throw new Error("Untrusted Office AI sender.");
  }
  function removeHandler(channel) {
    try {
      ipcMain.removeHandler(channel);
    } catch {
      // Electron permits replacing optional handlers; tests use a tiny stub.
    }
  }

  function installOfficeAiHandlers() {
    for (const channel of [
      "app:get-theme",
      "ai:get-settings",
      "ai:set-settings",
      "ai:gsk-status",
      "ai:gsk-login",
      "ai:chat",
      "ai:stream",
      "ai:stream-cancel",
      "ai:web-search",
      "ai:image-search",
      "ai:fetch-image",
      "ai:generate-image",
      "ai:analyze-media",
      "pdf:generate-image",
    ]) {
      removeHandler(channel);
    }
    ipcMain.handle("app:get-theme", async (event) => {
      if (!isEditorSender(event))
        throw new Error("Untrusted Office theme sender.");
      return "system";
    });
    ipcMain.handle("ai:get-settings", async (event) => {
      if (!isEditorSender(event))
        throw new Error("Untrusted Office AI sender.");
      return officeAiSettingsForEvent(event);
    });
    ipcMain.handle("ai:set-settings", async (event, settings) => {
      const document = trustedEditorDocument(event);
      if (!document) throw new Error("Untrusted Office AI sender.");
      const requested = normalizeModelBinding(settings?.lantaiModelBinding);
      if (!requested)
        return { ok: true, ...(await officeAiSettingsForEvent(event)) };
      const options = await resolveModelOptions();
      requireCurrentEditorDocument(event, document);
      const allowed = options.some(
        (option) =>
          option.provider_id === requested.provider_id &&
          option.model === requested.model,
      );
      if (!allowed) {
        throw new Error("该模型当前不可用于此编辑窗口，请刷新模型列表后重试。");
      }
      modelBindingsByEditor.set(event?.sender?.id, {
        ...requested,
        source: "window_selection",
      });
      return { ok: true, ...(await officeAiSettingsForEvent(event)) };
    });
    ipcMain.handle("ai:gsk-status", (event) => {
      if (!isEditorSender(event))
        throw new Error("Untrusted Office AI sender.");
      return { loggedIn: true };
    });
    ipcMain.handle("ai:gsk-login", (event) => {
      if (!isEditorSender(event))
        throw new Error("Untrusted Office AI sender.");
      return { ok: true };
    });
    ipcMain.handle("ai:web-search", async (event, query, maxResults) => {
      return await callOfficeTool(event, "web_search", {
        query: String(query || ""),
        max_results: Number(maxResults) || 6,
      });
    });
    ipcMain.handle("ai:image-search", async (event, query, maxResults) => {
      return await callOfficeTool(event, "image_search", {
        query: String(query || ""),
        max_results: Number(maxResults) || 8,
      });
    });
    ipcMain.handle("ai:fetch-image", async (event, url) => {
      return await callOfficeTool(event, "fetch_image", {
        url: String(url || ""),
      });
    });
    ipcMain.handle("ai:generate-image", async (event, input) => {
      return await callOfficeTool(event, "generate_image", {
        prompt: String(input?.prompt || ""),
        aspect_ratio: input?.aspectRatio
          ? String(input.aspectRatio)
          : undefined,
        reference_image_urls: Array.isArray(input?.referenceImageUrls)
          ? input.referenceImageUrls.map(String).filter(Boolean)
          : [],
      });
    });
    ipcMain.handle("pdf:generate-image", async (event, input) => {
      return await callOfficeTool(event, "generate_image", {
        prompt: String(input?.prompt || ""),
        aspect_ratio: input?.aspectRatio
          ? String(input.aspectRatio)
          : undefined,
      });
    });
    ipcMain.handle("ai:analyze-media", async (event, input) => {
      return await callOfficeTool(event, "analyze_media", {
        media_urls: Array.isArray(input?.mediaUrls)
          ? input.mediaUrls.map(String).filter(Boolean)
          : [],
        requirements: String(input?.requirements || ""),
      });
    });
    ipcMain.handle("ai:chat", async (event, request) => {
      const document = trustedEditorDocument(event);
      if (!document) throw new Error("Untrusted Office AI sender.");
      try {
        const result = await postOfficeJson("/api/console/office-agent", {
          ...officeBindingPayloadForEvent(event),
          system: String(request?.system || ""),
          messages: [{ role: "user", text: String(request?.user || "") }],
          tools: [],
          max_tokens: 8192,
        });
        requireCurrentEditorDocument(event, document);
        return { ok: true, text: String(result?.text || "") };
      } catch (error) {
        return {
          ok: false,
          error: error instanceof Error ? error.message : String(error),
        };
      }
    });
    ipcMain.handle("ai:stream-cancel", (event, requestId) => {
      if (!isEditorSender(event))
        throw new Error("Untrusted Office AI sender.");
      const owned = officeAiRequests.get(
        `${event.sender.id}:${String(requestId || "")}`,
      );
      if (owned && isCurrentEditorDocument(event, owned.document))
        owned.controller.abort();
    });
    ipcMain.handle("ai:stream", async (event, request) => {
      const document = trustedEditorDocument(event);
      if (!document) throw new Error("Untrusted Office AI sender.");
      const requestId = String(request?.requestId || "");
      if (!requestId) throw new Error("Invalid Office AI request id.");
      const requestKey = `${event.sender.id}:${requestId}`;
      if (officeAiRequests.has(requestKey))
        throw new Error("Office AI request is already active.");
      const controller = new AbortController();
      const owned = { controller, document };
      officeAiRequests.set(requestKey, owned);
      const send = (chunk) => {
        if (isCurrentEditorDocument(event, document)) {
          event.sender.send("ai:stream-chunk", { requestId, ...chunk });
        } else {
          controller.abort();
        }
      };
      try {
        const body = {
          ...officeBindingPayloadForEvent(event),
          system: String(request?.system || ""),
          messages: Array.isArray(request?.messages) ? request.messages : [],
          tools: Array.isArray(request?.tools) ? request.tools : [],
          max_tokens: Number(request?.maxTokens) || 8192,
        };
        if (await streamOfficeAgent(body, controller.signal, send)) return;
        const result = await postOfficeJson(
          "/api/console/office-agent",
          body,
          controller.signal,
        );
        controller.signal.throwIfAborted();
        const text = String(result?.text || "").trim();
        const toolCalls = Array.isArray(result?.toolCalls)
          ? result.toolCalls.filter((toolCall) => toolCall?.name)
          : [];
        if (!text && toolCalls.length === 0) {
          throw new Error(
            "沈行知没有产生可执行的操作或可见回复，请重试或补充具体修改目标。",
          );
        }
        if (text) send({ type: "delta", text });
        for (const toolCall of toolCalls) {
          send({ type: "tool-call", toolCall });
        }
        send({ type: "done" });
      } catch (error) {
        appendLog(
          `[OfficeEditor] AI request failed: ${error instanceof Error ? error.message : String(error)}`,
        );
        if (controller.signal.aborted) send({ type: "done" });
        else
          send({
            type: "error",
            error: error instanceof Error ? error.message : String(error),
          });
      } finally {
        if (officeAiRequests.get(requestKey) === owned)
          officeAiRequests.delete(requestKey);
      }
    });
  }

  function validateTarget(targetPath) {
    if (typeof targetPath !== "string" || !targetPath.trim()) {
      return errorResult("文件路径为空", "office_path_empty");
    }
    const requestedPath = targetPath.trim();
    const extension = path.extname(requestedPath).toLowerCase();
    const kind = OFFICE_FORMATS[extension];
    if (!kind) {
      return errorResult(
        "澜台本地编辑器支持 DOCX、XLSX、XLS、CSV、PPTX 和 PDF",
        "office_type_unsupported",
      );
    }
    let canonicalPath;
    let stat;
    try {
      canonicalPath = fsImpl.realpathSync(requestedPath);
      stat = fsImpl.statSync(canonicalPath);
    } catch {
      return errorResult("文件不存在或无法读取", "office_unreadable");
    }
    if (!stat.isFile()) {
      return errorResult("目标路径不是文件", "office_not_file");
    }
    const maxBytes = OFFICE_MAX_BYTES_BY_KIND[kind] || OFFICE_MAX_BYTES;
    if (stat.size > maxBytes) {
      return errorResult(
        `该格式文件不能超过 ${Math.floor(maxBytes / (1024 * 1024))} MB`,
        "office_too_large",
      );
    }
    return { success: true, path: canonicalPath, extension, kind };
  }

  function assertRuntimeFiles(kind, runtimePaths) {
    const required = [
      runtimePaths.modulePath,
      runtimePaths.preloadPath,
      runtimePaths.rendererFile,
      ...(kind === "sheets" ? [runtimePaths.sidecarPath] : []),
      ...(kind === "pdf" ? [runtimePaths.visionOcrPath] : []),
    ];
    if (
      required.some((candidate) => !candidate || !fsImpl.existsSync(candidate))
    ) {
      throw Object.assign(new Error("澜台本地编辑器还没有完整构建"), {
        code: "office_editor_runtime_missing",
      });
    }
  }

  function prepareRuntime(kind) {
    if (runtimes.has(kind)) return runtimes.get(kind);
    const runtimePaths = resolveRuntimePaths()?.[kind];
    if (!runtimePaths) {
      throw Object.assign(new Error("澜台本地编辑器配置缺失"), {
        code: "office_editor_runtime_missing",
      });
    }
    assertRuntimeFiles(kind, runtimePaths);
    const loaded = loadEditorRuntime(runtimePaths.modulePath);
    if (kind === "sheets") {
      const required = [
        "configureSheetsRuntime",
        "setSheetsShellWindow",
        "setForcedWorkbookPath",
        "registerProjectIpc",
        "createSheetsView",
      ];
      if (required.some((name) => typeof loaded?.[name] !== "function")) {
        throw Object.assign(new Error("澜台表格编辑器构建不完整"), {
          code: "office_editor_runtime_invalid",
        });
      }
      loaded.configureSheetsRuntime({
        preloadPath: runtimePaths.preloadPath,
        rendererFile: runtimePaths.rendererFile,
        sidecarPath: runtimePaths.sidecarPath,
      });
      loaded.registerProjectIpc();
    } else if (kind === "slides") {
      const required = [
        "configureSlidesRuntime",
        "setSlidesShellWindow",
        "registerSlidesIpc",
        "registerProjectIpc",
        "createSlidesView",
      ];
      if (required.some((name) => typeof loaded?.[name] !== "function")) {
        throw Object.assign(new Error("澜台演示编辑器构建不完整"), {
          code: "office_editor_runtime_invalid",
        });
      }
      loaded.configureSlidesRuntime({
        preloadPath: runtimePaths.preloadPath,
        rendererFilePath: runtimePaths.rendererFile,
      });
      loaded.registerSlidesIpc();
      loaded.registerProjectIpc();
    } else {
      const required = ["configurePdfRuntime", "createPdfView"];
      if (required.some((name) => typeof loaded?.[name] !== "function")) {
        throw Object.assign(new Error("澜台 PDF 编辑器构建不完整"), {
          code: "office_editor_runtime_invalid",
        });
      }
      loaded.configurePdfRuntime({
        preloadPath: runtimePaths.preloadPath,
        rendererFile: runtimePaths.rendererFile,
        visionOcrPath: runtimePaths.visionOcrPath,
      });
    }
    runtimes.set(kind, loaded);
    return loaded;
  }

  function fitWindowToWorkArea(editorWindow) {
    if (!editorWindow || editorWindow.isDestroyed?.()) return;
    editorWindow.setAlwaysOnTop?.(false);
    if (!screen?.getDisplayMatching || !editorWindow.getBounds) return;
    const current = editorWindow.getBounds();
    const display = screen.getDisplayMatching(current);
    const workArea = display?.workArea;
    if (!workArea) return;
    const margin = 16;
    const width = Math.max(
      720,
      Math.min(current.width || 1280, workArea.width - margin * 2),
    );
    const height = Math.max(
      560,
      Math.min(current.height || 840, workArea.height - margin * 2),
    );
    const x = Math.round(workArea.x + (workArea.width - width) / 2);
    const y = Math.max(
      workArea.y + margin,
      Math.round(workArea.y + (workArea.height - height) / 2),
    );
    editorWindow.setBounds?.({ x, y, width, height });
  }

  function installEditorDiagnostics(editorWindow, kind, targetPath) {
    const webContents = editorWindow?.webContents;
    if (!webContents?.on) return;
    webContents.on("did-fail-load", (_event, code, description, url) => {
      appendLog(
        `[OfficeEditor:${kind}] did-fail-load code=${code} description=${description} url=${url} file=${targetPath}`,
      );
    });
    webContents.on("render-process-gone", (_event, details = {}) => {
      appendLog(
        `[OfficeEditor:${kind}] render-process-gone reason=${details.reason || "unknown"} exitCode=${details.exitCode ?? "unknown"} file=${targetPath}`,
      );
    });
    webContents.on("console-message", (event, ...legacy) => {
      const { level, message, line, sourceId } = normalizeConsoleMessage(
        event,
        ...legacy,
      );
      if (Number(level) < 2) return;
      appendLog(
        `[OfficeEditor:${kind}] renderer-console level=${level} message=${message} source=${sourceId || "unknown"}:${line || 0}`,
      );
    });
  }

  async function openLocalOffice(targetInput, isCurrentCaller = () => true) {
    const targetPath =
      typeof targetInput === "string" ? targetInput : targetInput?.path;
    const agentId =
      typeof targetInput === "object" && targetInput
        ? normalizeAgentId(targetInput.agent_id)
        : "default";
    const sourceChatId =
      typeof targetInput === "object" && targetInput
        ? String(targetInput.source_chat_id || "")
            .trim()
            .slice(0, 200)
        : "";
    const modelBinding = await resolveModelBinding(agentId);
    const requireCaller = () => {
      if (!isCurrentCaller())
        throw Object.assign(new Error("页面已改变，请重新打开编辑器"), {
          code: "office_document_changed",
        });
    };
    if (!isCurrentCaller())
      return errorResult(
        "页面已改变，请重新打开编辑器",
        "office_document_changed",
      );
    const target = validateTarget(targetPath);
    if (!target.success) return target;
    const mainWindow = getMainWindow();
    if (!mainWindow || mainWindow.isDestroyed()) {
      return errorResult("澜台主窗口不可用", "office_main_window_unavailable");
    }
    try {
      const runtime = prepareRuntime(target.kind);
      // The DOCX runtime and the other editor runtimes share upstream `ai:*`
      // channel names. Reclaim the Lantai bridge whenever one of these editors
      // opens so an older DOCX window cannot leave its handler installed.
      if (target.kind === "pdf") {
        // The PDF runtime owns these two channels while bootstrapping. Let it
        // register synchronously, then replace them with the authenticated
        // Lantai bridge below. Registering our handlers first makes Electron
        // reject createPdfWindow() before any editor window can appear.
        removeHandler("app:get-theme");
        removeHandler("pdf:generate-image");
      } else {
        installOfficeAiHandlers();
      }
      return await presentOfficeView({
        path: target.path,
        kind: target.kind,
        source_chat_id: sourceChatId,
        createView: async () => {
          requireCaller();
          let editorView;
          if (target.kind === "sheets") {
            runtime.setSheetsShellWindow(mainWindow);
            runtime.setForcedWorkbookPath(target.path);
            editorView = await runtime.createSheetsView({
              includeAiHandlers: false,
            });
          } else if (target.kind === "slides") {
            runtime.setSlidesShellWindow(mainWindow);
            editorView = runtime.createSlidesView(target.path);
          } else {
            editorView = runtime.createPdfView(target.path);
            installOfficeAiHandlers();
          }
          if (!isCurrentCaller()) {
            editorView?.webContents?.destroy?.();
            requireCaller();
          }
          registerEditorWindow(editorView, {
            kind: target.kind,
            agent_id: agentId,
            source_chat_id: sourceChatId,
            model_binding: modelBinding,
          });
          installEditorDiagnostics(editorView, target.kind, target.path);
          return editorView;
        },
        onActivate: async (editorView) => {
          requireCaller();
          installOfficeAiHandlers();
          registerEditorWindow(editorView, {
            kind: target.kind,
            agent_id: agentId,
            source_chat_id: sourceChatId,
            model_binding: modelBinding,
          });
          if (target.kind === "sheets") runtime.installSheetsMenu?.();
          else if (target.kind === "slides") runtime.installSlidesMenu?.();
          else runtime.installPdfMenu?.();
        },
      });
    } catch (reason) {
      return errorResult(
        reason instanceof Error ? reason.message : "澜台本地编辑器打开失败",
        reason?.code || "office_editor_open_failed",
      );
    }
  }

  async function openFromIpc(event, targetInput) {
    const request = captureRendererRequest(event, isTrustedIpcEvent);
    if (!request) {
      return errorResult("未授权的本地编辑请求", "office_untrusted_sender");
    }
    return await openLocalOffice(targetInput, () =>
      isCurrentRendererRequest(request, event, isTrustedIpcEvent),
    );
  }

  function installIpcHandlers() {
    ipcMain.removeHandler(OFFICE_EDITOR_CHANNEL);
    ipcMain.handle(OFFICE_EDITOR_CHANNEL, openFromIpc);
    installOfficeAiHandlers();
  }

  return {
    fitWindowToWorkArea,
    installIpcHandlers,
    installOfficeAiHandlers,
    openFromIpc,
    openLocalOffice,
    resolveModelBinding,
    registerEditorWindow,
    isTrustedBackendRequest: (details) =>
      Boolean(
        details?.webContents &&
        details.webContentsId === details.webContents.id &&
        trustedEditorDocument({
          sender: details.webContents,
          senderFrame: details.frame,
        }),
      ),
    validateTarget,
  };
}

module.exports = {
  OFFICE_EDITOR_CHANNEL,
  OFFICE_FORMATS,
  OFFICE_MAX_BYTES,
  OFFICE_MAX_BYTES_BY_KIND,
  createOfficeEditorController,
  readOfficeAgentError,
  resolveOfficeEditorRuntimePaths,
};
