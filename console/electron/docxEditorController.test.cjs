const assert = require("node:assert/strict");
const test = require("node:test");
const { EventEmitter } = require("node:events");

const {
  DOCX_EDITOR_REWRITE_CHANNEL,
  DOCX_EDITOR_REWRITE_REQUEST_CHANNEL,
  DOCX_EDITOR_REWRITE_RESPONSE_CHANNEL,
  DOCX_MAX_BYTES,
  createDocxEditorController,
} = require("./docxEditorController.cjs");

function makeHarness({ size = 1024, trusted = true } = {}) {
  const handlers = new Map();
  const listeners = new Map();
  const calls = {
    configure: [],
    parents: [],
    register: 0,
    open: [],
    forwarded: [],
    officeAiInstalled: 0,
    officeAiWindows: [],
  };
  const editorContents = Object.assign(new EventEmitter(), {
    id: 42, getURL: () => "file:///runtime/renderer/index.html", isDestroyed: () => false,
    mainFrame: { url: "file:///runtime/renderer/index.html", parent: null, processId: 10, routingId: 42 },
  });
  const editorWindow = {
    webContents: editorContents,
    once: () => {},
  };
  const runtime = {
    configureDocsRuntime: (value) => calls.configure.push(value),
    setDocsShellWindow: (value) => calls.parents.push(value),
    registerDocsIpc: () => {
      calls.register += 1;
    },
    registerProjectIpc: () => {
      calls.registerProject = (calls.registerProject || 0) + 1;
    },
    createDocsView: (value) => {
      calls.open.push(value);
      return editorWindow;
    },
  };
  const mainContents = Object.assign(new EventEmitter(), {
    id: 1, getURL: () => "http://127.0.0.1:19090/#/chat", isDestroyed: () => false,
    mainFrame: { url: "http://127.0.0.1:19090/#/chat", parent: null, processId: 10, routingId: 1 },
    send: (channel, payload) => calls.forwarded.push([channel, payload]),
  });
  const mainWindow = {
    isDestroyed: () => false,
    webContents: mainContents,
  };
  const controller = createDocxEditorController({
    ipcMain: {
      handle: (channel, handler) => handlers.set(channel, handler),
      removeHandler: (channel) => handlers.delete(channel),
      on: (channel, listener) => listeners.set(channel, listener),
      removeListener: (channel) => listeners.delete(channel),
    },
    fsImpl: {
      existsSync: () => true,
      realpathSync: (value) => value,
      statSync: () => ({ isFile: () => true, size }),
    },
    isTrustedIpcEvent: () => trusted,
    getMainWindow: () => mainWindow,
    installOfficeAiHandlers: () => {
      calls.officeAiInstalled += 1;
    },
    registerOfficeAiEditorWindow: (value, context) =>
      calls.officeAiWindows.push([value, context]),
    presentOfficeView: async (options) => {
      const view = await options.createView();
      await options.onActivate?.(view, { reused: false });
      calls.presented = (calls.presented || []).concat([
        [options.kind, options.path, view.webContents.id],
      ]);
      return {
        success: true,
        workbench_id: "workbench-docs",
        route: "/office-workbench",
        reused: false,
      };
    },
    resolveRuntimePaths: () => ({
      modulePath: "/runtime/main/lantai.js",
      preloadPath: "/runtime/preload/index.js",
      rendererFile: "/runtime/renderer/index.html",
    }),
    loadEditorRuntime: () => runtime,
  });
  controller.installIpcHandlers();
  return {
    calls,
    controller,
    handler: handlers.get("lantai:docx-editor-open"),
    handlers,
    listeners,
    editorEvent: { sender: editorContents, senderFrame: editorContents.mainFrame },
    mainEvent: { sender: mainContents, senderFrame: mainContents.mainFrame },
  };
}

test("opens a local DOCX from trusted native desktop entry points", async () => {
  const { calls, controller } = makeHarness();

  assert.deepEqual(
    await controller.openLocalDocx({
      path: "/tmp/native-entry.docx",
      source_chat_id: "chat-native",
      agent_id: "writing-agent",
    }),
    {
      success: true,
      workbench_id: "workbench-docs",
      route: "/office-workbench",
      reused: false,
    },
  );

  assert.deepEqual(calls.open, ["/tmp/native-entry.docx"]);
  assert.equal(calls.officeAiInstalled, 1);
  assert.equal(calls.officeAiWindows.length, 1);
  assert.equal(calls.officeAiWindows[0][0].webContents.id, 42);
  assert.deepEqual(calls.officeAiWindows[0][1], {
    kind: "docs",
    agent_id: "writing-agent",
    source_chat_id: "chat-native",
  });
});

test("rejects DOCX files larger than the 150 MB local-editor limit", async () => {
  const { calls, handler, mainEvent } = makeHarness({ size: DOCX_MAX_BYTES + 1 });

  const result = await handler(mainEvent, "/tmp/large.docx");

  assert.deepEqual(result, {
    success: false,
    error: "DOCX 文件不能超过 150 MB",
    error_code: "docx_too_large",
  });
  assert.deepEqual(calls.open, []);
});

test("opens a trusted local DOCX and initializes the embedded runtime once", async () => {
  const { calls, handler, mainEvent } = makeHarness({ size: DOCX_MAX_BYTES });

  assert.equal(
    (await handler(mainEvent, "/tmp/first.docx")).route,
    "/office-workbench",
  );
  assert.equal(
    (await handler(mainEvent, "/tmp/second.DOCX")).route,
    "/office-workbench",
  );

  assert.equal(calls.configure.length, 1);
  assert.equal(calls.register, 1);
  assert.equal(calls.registerProject, 1);
  assert.deepEqual(calls.open, ["/tmp/first.docx", "/tmp/second.DOCX"]);
  assert.equal(calls.parents.length, 3);
});

test("rejects untrusted senders before touching the file or runtime", async () => {
  const { calls, handler } = makeHarness({ trusted: false });

  const result = await handler({}, "/tmp/report.docx");

  assert.equal(result.success, false);
  assert.equal(result.error_code, "docx_untrusted_sender");
  assert.deepEqual(calls.open, []);
});

test("forwards a rewrite request only from an opened editor window", async () => {
  const { calls, handler, handlers, listeners, editorEvent, mainEvent } = makeHarness();
  await handler(
    mainEvent,
    {
      path: "/tmp/report.docx",
      source_chat_id: "chat-source-1",
    },
  );

  const pending = handlers.get(DOCX_EDITOR_REWRITE_CHANNEL)(
    editorEvent,
    {
      instruction: "make it clearer",
      selected_text: "old",
      document_before: "before".repeat(5000),
      document_after: "after".repeat(5000),
      current_draft: "current candidate",
      document_name: "report.docx",
    },
  );
  const [, envelope] = calls.forwarded[0];
  listeners.get(DOCX_EDITOR_REWRITE_RESPONSE_CHANNEL)(
    mainEvent,
    {
      request_id: envelope.request_id,
      result: { success: true, replacement_text: "new" },
    },
  );

  assert.equal(calls.forwarded[0][0], DOCX_EDITOR_REWRITE_REQUEST_CHANNEL);
  assert.equal(envelope.payload.source_chat_id, "chat-source-1");
  assert.equal(envelope.payload.document_before.length, 30_000);
  assert.equal(envelope.payload.document_after.length, 25_000);
  assert.equal(envelope.payload.current_draft, "current candidate");
  assert.deepEqual(await pending, {
    success: true,
    replacement_text: "new",
    summary: "",
  });
});
