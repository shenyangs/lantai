const assert = require("node:assert/strict");
const test = require("node:test");
const { EventEmitter } = require("node:events");

const {
  OFFICE_MAX_BYTES,
  OFFICE_MAX_BYTES_BY_KIND,
  createOfficeEditorController,
  readOfficeAgentError,
  resolveOfficeEditorRuntimePaths,
} = require("./officeEditorController.cjs");

function makeHarness({
  size = 1024,
  trusted = true,
  fetchImpl,
  resolveOfficeModelBinding = async () => null,
  resolveOfficeModelOptions = async () => [],
  simulateLegacyRuntimeAiHandlers = false,
  simulatePdfOwnedHandlers = false,
  strictDuplicateHandlers = false,
  beforeActivate,
} = {}) {
  const handlers = new Map();
  const registerHandler = (channel, handler) => {
    if (strictDuplicateHandlers && handlers.has(channel)) {
      throw new Error(
        `Attempted to register a second handler for '${channel}'`,
      );
    }
    handlers.set(channel, handler);
  };
  const calls = { configured: [], opened: [], presented: [] };
  const editorView = {
    isDestroyed: () => false,
    webContents: Object.assign(new EventEmitter(), {
      id: 42,
      mainFrame: null,
      getURL() { return this.mainFrame?.url || "about:blank"; },
      isDestroyed: () => false,
      send: () => {},
      setWindowOpenHandler: () => {},
    }),
  };
  let routingId = 0;
  const setRenderer = (prefix) => {
    editorView.webContents.mainFrame = {
      url: `file:///${prefix}/renderer/index.html?mode=tab`,
      processId: 100, routingId: ++routingId, parent: null,
    };
  };
  const editorEvent = (receive) => {
    if (receive) editorView.webContents.send = (_channel, payload) => receive(payload);
    return { sender: editorView.webContents, senderFrame: editorView.webContents.mainFrame };
  };
  const sheets = {
    configureSheetsRuntime: (value) => calls.configured.push(["sheets", value]),
    setSheetsShellWindow: () => {},
    setForcedWorkbookPath: (value) => calls.opened.push(["sheet-path", value]),
    registerProjectIpc: () => calls.opened.push(["sheet-project-ipc"]),
    createSheetsView: async () => { setRenderer("s"); return editorView; },
  };
  const slides = {
    configureSlidesRuntime: (value) => calls.configured.push(["slides", value]),
    setSlidesShellWindow: () => {},
    registerSlidesIpc: () => {
      if (!simulateLegacyRuntimeAiHandlers) return;
      for (const channel of [
        "ai:web-search",
        "ai:image-search",
        "ai:generate-image",
        "ai:analyze-media",
        "pdf:generate-image",
      ]) {
        handlers.set(channel, async () => ({ legacy: true }));
      }
    },
    registerProjectIpc: () => calls.opened.push(["slides-project-ipc"]),
    createSlidesView: (value) => {
      setRenderer("p");
      calls.opened.push(["slides", value]);
      return editorView;
    },
  };
  const pdf = {
    configurePdfRuntime: (value) => calls.configured.push(["pdf", value]),
    createPdfView: (value) => {
      setRenderer("d");
      if (simulatePdfOwnedHandlers) {
        registerHandler("pdf:generate-image", async () => ({ legacy: true }));
        registerHandler("app:get-theme", async () => "legacy");
      }
      calls.opened.push(["pdf", value]);
      return editorView;
    },
  };
  const mainContents = Object.assign(new EventEmitter(), {
    id: 1, isDestroyed: () => false,
    getURL: () => "http://127.0.0.1:8765/#/chat",
    mainFrame: { url: "http://127.0.0.1:8765/#/chat", parent: null, processId: 100, routingId: 1 },
  });
  const controller = createOfficeEditorController({
    ipcMain: {
      handle: registerHandler,
      removeHandler: (channel) => handlers.delete(channel),
    },
    screen: {
      getDisplayMatching: () => ({
        workArea: { x: 0, y: 24, width: 1200, height: 760 },
      }),
    },
    fsImpl: {
      existsSync: () => true,
      realpathSync: (value) => value,
      statSync: () => ({ isFile: () => true, size }),
    },
    isTrustedIpcEvent: () => trusted,
    getMainWindow: () => ({ isDestroyed: () => false, webContents: mainContents }),
    getApiBaseUrl: () => "http://127.0.0.1:8765",
    getDesktopToken: () => "desktop-secret",
    fetchImpl,
    resolveOfficeModelBinding,
    resolveOfficeModelOptions,
    presentOfficeView: async (options) => {
      const view = await options.createView();
      await beforeActivate?.({ handlers, editorEvent });
      await options.onActivate?.(view, { reused: false });
      calls.presented.push([options.kind, options.path, view.webContents.id]);
      return {
        success: true,
        workbench_id: `workbench-${options.kind}`,
        route: "/office-workbench",
        reused: false,
      };
    },
    resolveRuntimePaths: () => ({
      sheets: {
        modulePath: "/s/main/lantai.js",
        preloadPath: "/s/preload/index.js",
        rendererFile: "/s/renderer/index.html",
        sidecarPath: "/s/native/xlsx-engine",
      },
      slides: {
        modulePath: "/p/main/lantai.js",
        preloadPath: "/p/preload/index.js",
        rendererFile: "/p/renderer/index.html",
      },
      pdf: {
        modulePath: "/d/main/lantai.js",
        preloadPath: "/d/preload/index.js",
        rendererFile: "/d/renderer/index.html",
        visionOcrPath: "/d/native/lantai-pdf-vision",
      },
    }),
    loadEditorRuntime: (modulePath) =>
      modulePath.startsWith("/s/")
        ? sheets
        : modulePath.startsWith("/p/")
          ? slides
          : pdf,
  });
  controller.installIpcHandlers();
  return {
    calls,
    controller,
    editorEvent,
    mainEvent: { sender: mainContents, senderFrame: mainContents.mainFrame },
    handler: handlers.get("lantai:office-editor-open"),
    handlers,
  };
}

test("routes spreadsheet, presentation, and PDF files to their local runtimes", async () => {
  const { calls, controller } = makeHarness();
  for (const file of [
    "book.xlsx",
    "legacy.xls",
    "data.csv",
    "deck.pptx",
    "paper.pdf",
  ]) {
    assert.equal(
      (await controller.openLocalOffice(`/tmp/${file}`)).route,
      "/office-workbench",
    );
  }
  assert.deepEqual(calls.opened, [
    ["sheet-project-ipc"],
    ["sheet-path", "/tmp/book.xlsx"],
    ["sheet-path", "/tmp/legacy.xls"],
    ["sheet-path", "/tmp/data.csv"],
    ["slides-project-ipc"],
    ["slides", "/tmp/deck.pptx"],
    ["pdf", "/tmp/paper.pdf"],
  ]);
  assert.equal(calls.configured.length, 3);
  assert.equal(calls.presented.length, 5);
});

test("lets the PDF runtime bootstrap owned channels before reclaiming the Lantai bridge", async () => {
  const { controller, handlers, editorEvent } = makeHarness({
    simulatePdfOwnedHandlers: true,
    strictDuplicateHandlers: true,
    fetchImpl: async () => ({
      ok: true,
      json: async () => ({ url: "data:image/png;base64,AA==" }),
    }),
  });

  assert.equal(
    (await controller.openLocalOffice("/tmp/paper.pdf")).route,
    "/office-workbench",
  );
  assert.equal(
    await handlers.get("app:get-theme")(editorEvent()),
    "system",
  );
  assert.notDeepEqual(
    await handlers.get("pdf:generate-image")(
      editorEvent(),
      { prompt: "cover" },
    ),
    { legacy: true },
  );
});

test("rejects unsupported, oversized, and untrusted office opens", async () => {
  const oversized = makeHarness({ size: OFFICE_MAX_BYTES + 1 });
  assert.equal(
    (await oversized.handler(oversized.mainEvent, "/tmp/large.xlsx")).error_code,
    "office_too_large",
  );
  assert.equal(
    (await oversized.controller.openLocalOffice("/tmp/file.doc")).error_code,
    "office_type_unsupported",
  );
  const untrusted = makeHarness({ trusted: false });
  assert.equal(
    (await untrusted.handler({}, "/tmp/file.pdf")).error_code,
    "office_untrusted_sender",
  );
});

test("uses a safer per-format size budget instead of one low global ceiling", async () => {
  const betweenSlidesAndSheets = makeHarness({
    size: OFFICE_MAX_BYTES_BY_KIND.slides + 1,
  });
  assert.equal(
    (await betweenSlidesAndSheets.controller.openLocalOffice("/tmp/deck.pptx"))
      .error_code,
    "office_too_large",
  );
  assert.deepEqual(
    await betweenSlidesAndSheets.controller.openLocalOffice("/tmp/book.xlsx"),
    {
      success: true,
      workbench_id: "workbench-sheets",
      route: "/office-workbench",
      reused: false,
    },
  );
});

test("trusts the created editor renderer before workbench activation", async () => {
  let startupChecks = 0;
  const { controller, handlers, editorEvent } = makeHarness({
    beforeActivate: async ({ handlers, editorEvent }) => {
      assert.equal(await handlers.get("app:get-theme")(editorEvent()), "system");
      assert.equal((await handlers.get("ai:get-settings")(editorEvent())).providers.custom.model, "澜台默认模型");
      startupChecks += 1;
    },
  });
  assert.equal((await controller.openLocalOffice("/tmp/book.xlsx")).success, true);
  assert.equal(startupChecks, 1);
  const legitimate = editorEvent();
  for (const url of [legitimate.sender.getURL(), "file:///tmp/untrusted.html"]) {
    const frame = { ...legitimate.senderFrame, url };
    const unknown = { ...legitimate.sender, id: 999, mainFrame: frame, getURL: () => url };
    await assert.rejects(handlers.get("ai:get-settings")({ sender: unknown, senderFrame: frame }), /Untrusted Office AI sender/);
  }
});

test("routes Office Agent calls through the active authenticated Lantai backend", async () => {
  let request;
  const chunks = [];
  const { controller, handlers, editorEvent } = makeHarness({
    fetchImpl: async (url, init) => {
      request = { url, init };
      return {
        ok: true,
        json: async () => ({
          text: "已分析工作簿",
          toolCalls: [
            { id: "call-1", name: "read_workbook", input: { sheet: "Sheet1" } },
          ],
        }),
      };
    },
  });
  await controller.openLocalOffice({
    path: "/tmp/book.xlsx",
    agent_id: "research-agent",
  });

  await handlers.get("ai:stream")(
    editorEvent((payload) => chunks.push(payload)),
    {
      requestId: "office-request-1",
      system: "edit workbook",
      messages: [{ role: "user", content: "summarize" }],
      tools: [{ name: "read_workbook", inputSchema: { type: "object" } }],
    },
  );

  assert.equal(request.url, "http://127.0.0.1:8765/api/console/office-agent");
  assert.equal(
    request.init.headers["X-Lantai-Desktop-Token"],
    "desktop-secret",
  );
  assert.equal(JSON.parse(request.init.body).agent_id, "research-agent");
  assert.deepEqual(
    chunks.map((chunk) => chunk.type),
    ["delta", "tool-call", "done"],
  );
});

test("streams provider-neutral reasoning and emits one bounded terminal sequence", async () => {
  const requests = [];
  const chunks = [];
  const encoder = new TextEncoder();
  const payload =
    [
      { type: "thinking-delta", text: "先看结构" },
      { type: "delta", text: "正在修改" },
      {
        type: "tool-call",
        toolCall: { id: "call-1", name: "read", input: {} },
      },
      {
        type: "tool-call",
        toolCall: { id: "call-1", name: "read", input: {} },
      },
      { type: "done", stopReason: "stop" },
      { type: "done", stopReason: "stop" },
    ]
      .map((item) => JSON.stringify(item))
      .join("\n") + "\n";
  const { controller, handlers, editorEvent } = makeHarness({
    fetchImpl: async (url, init) => {
      requests.push({ url, init });
      return {
        ok: true,
        status: 200,
        body: new ReadableStream({
          start(stream) {
            const encoded = encoder.encode(payload);
            stream.enqueue(encoded.slice(0, 17));
            stream.enqueue(encoded.slice(17));
            stream.close();
          },
        }),
      };
    },
  });
  await controller.openLocalOffice({
    path: "/tmp/deck.pptx",
    agent_id: "research-agent",
    source_chat_id: "chat-42",
  });

  await handlers.get("ai:stream")(
    editorEvent((payload) => chunks.push(payload)),
    { requestId: "office-stream-1", messages: [], tools: [] },
  );

  assert.equal(
    requests[0].url,
    "http://127.0.0.1:8765/api/console/office-agent/stream",
  );
  assert.equal(JSON.parse(requests[0].init.body).source_chat_id, "chat-42");
  assert.deepEqual(
    chunks.map((chunk) => chunk.type),
    ["thinking-delta", "delta", "tool-call", "done"],
  );
});

test("reclaims shared Office AI utility channels from an embedded runtime", async () => {
  const requests = [];
  const { controller, handlers, editorEvent } = makeHarness({
    simulateLegacyRuntimeAiHandlers: true,
    fetchImpl: async (url, init) => {
      requests.push({ url, body: JSON.parse(init.body) });
      return {
        ok: true,
        json: async () => ({
          results: [
            { title: "澜台", url: "https://example.com", snippet: "结果" },
          ],
        }),
      };
    },
  });

  await controller.openLocalOffice("/tmp/deck.pptx");
  const result = await handlers.get("ai:web-search")(
    editorEvent(),
    "澜台",
    4,
  );

  assert.equal(
    requests[0].url,
    "http://127.0.0.1:8765/api/console/office-tool",
  );
  assert.deepEqual(requests[0].body, {
    action: "web_search",
    agent_id: "default",
    query: "澜台",
    max_results: 4,
  });
  assert.equal(result.results[0].title, "澜台");
  for (const channel of [
    "ai:web-search",
    "ai:image-search",
    "ai:generate-image",
    "ai:analyze-media",
    "pdf:generate-image",
  ]) {
    assert.notDeepEqual(
      await handlers.get(channel)(
        editorEvent(),
        {},
      ),
      { legacy: true },
    );
  }
});

test("pins the opening model for one Office window and exposes its real label", async () => {
  let request;
  const chunks = [];
  const { controller, handlers, editorEvent } = makeHarness({
    resolveOfficeModelBinding: async (agentId) => ({
      provider_id: "minimax-cn",
      model: "MiniMax-M3",
      source:
        agentId === "writing-agent" ? "chat_snapshot" : "default_snapshot",
    }),
    fetchImpl: async (_url, init) => {
      request = JSON.parse(init.body);
      return { ok: true, json: async () => ({ text: "完成", toolCalls: [] }) };
    },
  });

  await controller.openLocalOffice({
    path: "/tmp/book.xlsx",
    agent_id: "writing-agent",
  });
  const settings = await handlers.get("ai:get-settings")(editorEvent());
  assert.equal(settings.providers.custom.model, "MiniMax-M3");
  assert.deepEqual(settings.lantaiModelBinding, {
    provider_id: "minimax-cn",
    model: "MiniMax-M3",
    source: "chat_snapshot",
  });

  await handlers.get("ai:stream")(
    editorEvent((payload) => chunks.push(payload)),
    { requestId: "pinned-model", messages: [], tools: [] },
  );

  assert.deepEqual(request.model_binding, settings.lantaiModelBinding);
  assert.deepEqual(
    chunks.map((chunk) => chunk.type),
    ["delta", "done"],
  );
});

test("switches a model only for the current Office editor window", async () => {
  let request;
  const { controller, handlers, editorEvent } = makeHarness({
    resolveOfficeModelBinding: async () => ({
      provider_id: "minimax-cn",
      model: "MiniMax-M3",
      source: "chat_snapshot",
    }),
    resolveOfficeModelOptions: async () => [
      { provider_id: "minimax-cn", model: "MiniMax-M3", label: "MiniMax M3" },
      { provider_id: "kimi", model: "kimi-k2", label: "Kimi K2" },
    ],
    fetchImpl: async (_url, init) => {
      request = JSON.parse(init.body);
      return { ok: true, json: async () => ({ text: "完成", toolCalls: [] }) };
    },
  });
  await controller.openLocalOffice({
    path: "/tmp/book.xlsx",
    agent_id: "writing-agent",
  });
  const event = editorEvent();
  const initial = await handlers.get("ai:get-settings")(event);
  assert.equal(initial.lantaiModelBinding.model, "MiniMax-M3");
  assert.deepEqual(
    initial.lantaiModelOptions.map((option) => option.model),
    ["MiniMax-M3", "kimi-k2"],
  );

  const switched = await handlers.get("ai:set-settings")(event, {
    lantaiModelBinding: { provider_id: "kimi", model: "kimi-k2" },
  });
  assert.deepEqual(switched.lantaiModelBinding, {
    provider_id: "kimi",
    model: "kimi-k2",
    source: "window_selection",
  });

  await handlers.get("ai:stream")(event, {
    requestId: "local-switch",
    messages: [],
    tools: [],
  });
  assert.deepEqual(request.model_binding, switched.lantaiModelBinding);
});

test("surfaces an explicit error instead of reporting success for an empty Office Agent turn", async () => {
  const chunks = [];
  const { controller, handlers, editorEvent } = makeHarness({
    fetchImpl: async () => ({
      ok: true,
      json: async () => ({ text: "", toolCalls: [] }),
    }),
  });
  await controller.openLocalOffice("/tmp/book.xlsx");

  await handlers.get("ai:stream")(
    editorEvent((payload) => chunks.push(payload)),
    { requestId: "empty-office-turn", messages: [], tools: [] },
  );

  assert.deepEqual(
    chunks.map((chunk) => chunk.type),
    ["error"],
  );
  assert.match(chunks[0].error, /没有产生可执行/);
});

test("unwraps backend JSON errors instead of exposing raw protocol objects", async () => {
  assert.equal(
    await readOfficeAgentError({
      status: 502,
      text: async () => JSON.stringify({ detail: "无法读取当前幻灯片" }),
    }),
    "无法读取当前幻灯片",
  );
});

test("resolves development and packaged runtime paths deterministically", () => {
  const dev = resolveOfficeEditorRuntimePaths({
    isDev: true,
    electronDir: "/app/electron",
    resourcesPath: "/resources",
    platform: "darwin",
    arch: "arm64",
  });
  assert.match(
    dev.sheets.sidecarPath,
    /native-bin\/darwin-arm64\/xlsx-engine$/,
  );
  assert.match(
    dev.pdf.visionOcrPath,
    /native-bin\/darwin-arm64\/lantai-pdf-vision$/,
  );
  const packaged = resolveOfficeEditorRuntimePaths({
    isDev: false,
    electronDir: "/app/electron",
    resourcesPath: "/resources",
  });
  assert.equal(
    packaged.pdf.modulePath,
    "/resources/lantai-office-editors/pdf/main/lantai.js",
  );
  assert.equal(
    packaged.pdf.visionOcrPath,
    "/resources/lantai-office-editors/pdf/native/lantai-pdf-vision",
  );
});
