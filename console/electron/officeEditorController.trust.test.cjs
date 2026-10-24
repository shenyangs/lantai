const assert = require("node:assert/strict");
const test = require("node:test");
const { EventEmitter } = require("node:events");
const { createOfficeEditorController } = require("./officeEditorController.cjs");
const { createDocxEditorController } = require("./docxEditorController.cjs");

function harness(fetchOverride, extra = {}) {
  const handlers = new Map();
  const requests = [];
  const renderer = "file:///tmp/lantai-audit-editor/renderer/index.html";
  const mainFrame = { url: renderer, processId: 100, routingId: 1, parent: null };
  const sender = Object.assign(new EventEmitter(), { id: 42, mainFrame, getURL: () => renderer, isDestroyed: () => false, send: () => {}, setWindowOpenHandler: (handler) => { sender.windowOpenHandler = handler; } });
  const controller = createOfficeEditorController({
    ipcMain: { handle: (name, handler) => handlers.set(name, handler), removeHandler: (name) => handlers.delete(name) },
    resolveRuntimePaths: () => ({ slides: { rendererFile: "/tmp/lantai-audit-editor/renderer/index.html" } }),
    getDesktopToken: () => "fixture-token",
    fetchImpl: fetchOverride || (async (url, options) => { requests.push({ url, options }); return { ok: true, json: async () => ({ ok: true }) }; }),
    ...extra,
  });
  controller.installIpcHandlers();
  controller.registerEditorWindow({ webContents: sender }, { agent_id: "fixture-agent" });
  return { controller, handlers, sender, mainFrame, renderer, requests };
}

test("registered editor current top frame can use the local tool bridge", async () => {
  const h = harness();
  await h.handlers.get("ai:web-search")({ sender: h.sender, senderFrame: h.mainFrame }, "fixture", 1);
  assert.equal(h.requests.length, 1);
});

for (const kind of ["external navigation", "child iframe", "stale frame document", "destroyed contents", "unregistered copy of renderer URL"]) {
  test(`reject ${kind} before accessing the privileged backend`, async () => {
    const h = harness();
    let sender = h.sender;
    let senderFrame = h.mainFrame;
    if (kind === "external navigation") {
      sender.getURL = () => "https://fixture.invalid/attacker";
      sender.mainFrame = senderFrame = { ...h.mainFrame, url: sender.getURL() };
    } else if (kind === "child iframe") {
      senderFrame = { url: h.renderer, processId: 101, routingId: 2, parent: h.mainFrame };
    } else if (kind === "stale frame document") {
      senderFrame = { ...h.mainFrame, url: "file:///tmp/untrusted-preview.html" };
    } else if (kind === "destroyed contents") {
      sender.isDestroyed = () => true;
    } else {
      sender = { ...h.sender, id: 99 };
    }
    await assert.rejects(h.handlers.get("ai:web-search")({ sender, senderFrame }, "fixture", 1), /Untrusted/);
    assert.equal(h.requests.length, 0);
  });
}

test("a second editor cannot cancel a request owned by the first editor", async () => {
  let signal;
  let release;
  const gate = new Promise((resolve) => { release = resolve; });
  const h = harness(async (_url, options) => {
    signal = options.signal;
    await gate;
    return { ok: true, body: { getReader: () => ({ read: async () => ({ done: true, value: new TextEncoder().encode('{"type":"done"}\n') }) }) } };
  });
  const first = { sender: h.sender, senderFrame: h.mainFrame };
  const secondSender = { ...h.sender, id: 43, mainFrame: { ...h.mainFrame, routingId: 3 } };
  h.controller.registerEditorWindow({ webContents: secondSender }, { agent_id: "second-agent" });
  const stream = h.handlers.get("ai:stream")(first, { requestId: "request-owner-fixture", messages: [] });
  assert.ok(signal);
  h.handlers.get("ai:stream-cancel")({ sender: secondSender, senderFrame: secondSender.mainFrame }, "request-owner-fixture");
  const wasAbortedByOtherEditor = signal.aborted;
  release();
  await stream;
  assert.equal(wasAbortedByOtherEditor, false);
});

test("stream chunks are not sent to a different document after navigation", async () => {
  let release;
  const gate = new Promise((resolve) => { release = resolve; });
  const h = harness(async () => {
    await gate;
    return { ok: true, body: { getReader: () => ({ read: async () => ({ done: true, value: new TextEncoder().encode('{"type":"delta","text":"fixture-result"}\n{"type":"done"}\n') }) }) } };
  });
  const chunks = [];
  h.sender.send = (...args) => chunks.push(args);
  const stream = h.handlers.get("ai:stream")({ sender: h.sender, senderFrame: h.mainFrame }, { requestId: "navigation-fixture", messages: [] });
  h.sender.getURL = () => "file:///tmp/untrusted-preview.html";
  h.sender.mainFrame = { ...h.mainFrame, url: h.sender.getURL(), routingId: 4 };
  release();
  await stream;
  assert.deepEqual(chunks, []);
});


test("the registered id alone cannot authorize another webContents object", async () => {
  const h = harness();
  const impostor = { ...h.sender };
  await assert.rejects(h.handlers.get("ai:web-search")({ sender: impostor, senderFrame: h.mainFrame }, "fixture", 1), /Untrusted/);
  assert.equal(h.requests.length, 0);
});

test("an editor cannot switch its trusted document to another editor runtime", async () => {
  const h = harness(undefined, { resolveRuntimePaths: () => ({
    slides: { rendererFile: "/tmp/lantai-audit-editor/renderer/index.html" },
    sheets: { rendererFile: "/tmp/lantai-audit-sheets/renderer/index.html" },
  }) });
  h.sender.getURL = () => "file:///tmp/lantai-audit-sheets/renderer/index.html";
  h.sender.mainFrame = { ...h.mainFrame, url: h.sender.getURL(), routingId: 3 };
  await assert.rejects(h.handlers.get("ai:web-search")({ sender: h.sender, senderFrame: h.sender.mainFrame }, "fixture", 1), /Untrusted/);
  assert.equal(h.requests.length, 0);
});

function deferredStreamHarness(extra = {}) {
  let release;
  const gate = new Promise((resolve) => { release = resolve; });
  const signals = [];
  const h = harness(async (_url, options) => {
    signals.push(options.signal);
    await gate;
    return { ok: true, body: { getReader: () => ({ read: async () => ({
      done: true, value: new TextEncoder().encode('{"type":"delta","text":"private-fixture"}\n{"type":"done"}\n'),
    }) }) } };
  }, extra);
  const chunks = [];
  h.sender.send = (...args) => chunks.push(args);
  return { ...h, release, signals, chunks, event: { sender: h.sender, senderFrame: h.mainFrame } };
}

test("reload of the same renderer retires the previous document stream", async () => {
  const h = deferredStreamHarness();
  const stream = h.handlers.get("ai:stream")(h.event, { requestId: "reload", messages: [] });
  h.sender.emit("did-start-navigation", {}, h.renderer, false, true, 100, 1);
  const abortedAtNavigation = h.signals[0].aborted;
  h.release();
  await stream;
  assert.equal(abortedAtNavigation, true);
  assert.deepEqual(h.chunks, []);
});

test("destroying the editor aborts its pending owned stream", async () => {
  const h = deferredStreamHarness();
  const stream = h.handlers.get("ai:stream")(h.event, { requestId: "destroyed", messages: [] });
  h.sender.emit("destroyed");
  const abortedAtDestruction = h.signals[0].aborted;
  h.release();
  await stream;
  assert.equal(abortedAtDestruction, true);
  assert.deepEqual(h.chunks, []);
});

test("an active request id cannot silently replace a request in the same editor", async () => {
  const h = deferredStreamHarness();
  const first = h.handlers.get("ai:stream")(h.event, { requestId: "duplicate", messages: [] });
  const second = h.handlers.get("ai:stream")(h.event, { requestId: "duplicate", messages: [] });
  const result = Promise.allSettled([first, second]);
  h.release();
  const settled = await result;
  assert.equal(settled[0].status, "fulfilled");
  assert.equal(settled[1].status, "rejected");
  assert.match(settled[1].reason.message, /already active/);
  assert.equal(h.signals.length, 1);
});

test("equal request ids in two editors have independent cancellation owners", async () => {
  const h = deferredStreamHarness();
  const secondSender = Object.assign(new EventEmitter(), { id: 43, mainFrame: { ...h.mainFrame, routingId: 3 },
    getURL: () => h.renderer, isDestroyed: () => false, send: () => {} });
  h.controller.registerEditorWindow({ webContents: secondSender }, { agent_id: "second-agent" });
  const first = h.handlers.get("ai:stream")(h.event, { requestId: "same", messages: [] });
  const second = h.handlers.get("ai:stream")({ sender: secondSender, senderFrame: secondSender.mainFrame }, { requestId: "same", messages: [] });
  h.handlers.get("ai:stream-cancel")(h.event, "same");
  const beforeRelease = h.signals.map((signal) => signal.aborted);
  h.release();
  await Promise.all([first, second]);
  assert.deepEqual(beforeRelease, [true, false]);
});

test("a model selection resolving after navigation cannot mutate the new document", async () => {
  let release;
  const gate = new Promise((resolve) => { release = resolve; });
  const h = harness(undefined, { resolveOfficeModelOptions: async () => { await gate; return [
    { provider_id: "fixture-provider", model: "fixture-model" },
  ]; } });
  const original = { sender: h.sender, senderFrame: h.mainFrame };
  const pending = h.handlers.get("ai:set-settings")(original, { lantaiModelBinding: { provider_id: "fixture-provider", model: "fixture-model" } });
  const result = Promise.allSettled([pending]);
  h.sender.emit("did-start-navigation", {}, h.renderer, false, true, 100, 1);
  h.sender.mainFrame = { ...h.mainFrame, routingId: 3 };
  release();
  const [settled] = await result;
  assert.equal(settled.status, "rejected");
  assert.match(settled.reason.message, /Untrusted/);
  const fresh = await h.handlers.get("ai:get-settings")({ sender: h.sender, senderFrame: h.sender.mainFrame });
  assert.equal(fresh.lantaiModelBinding, null);
});

test("editor windows deny new windows and prevent external top navigation", () => {
  const h = harness();
  let prevented = false;
  h.sender.emit("will-navigate", { preventDefault: () => { prevented = true; } }, "https://fixture.invalid/outside");
  assert.equal(prevented, true);
  assert.deepEqual(h.sender.windowOpenHandler?.({ url: "https://fixture.invalid/outside" }), { action: "deny" });
});
test("navigation start prevents a new privileged request from the old document", async () => {
  const h = harness();
  h.sender.emit("did-start-navigation", {}, h.renderer, false, true, 100, 99);
  await assert.rejects(h.handlers.get("ai:web-search")({ sender: h.sender, senderFrame: h.mainFrame }, "fixture", 1), /Untrusted/);
  assert.equal(h.requests.length, 0);
  h.sender.mainFrame = { ...h.mainFrame, routingId: 99 };
  await h.handlers.get("ai:web-search")({ sender: h.sender, senderFrame: h.sender.mainFrame }, "fixture", 1);
  assert.equal(h.requests.length, 1);
});
for (const eventName of ["will-redirect", "will-frame-navigate"]) {
  test(`editor prevents an external ${eventName}`, () => {
    const h = harness();
    let prevented = false;
    h.sender.emit(eventName, { preventDefault: () => { prevented = true; } }, "https://fixture.invalid/outside");
    assert.equal(prevented, true);
  });
}
test("changing an editor chat context aborts the previous context's pending stream", async () => {
  const h = deferredStreamHarness();
  const stream = h.handlers.get("ai:stream")(h.event, { requestId: "changed-context", messages: [] });
  h.controller.registerEditorWindow({ webContents: h.sender }, { agent_id: "new-agent", source_chat_id: "new-chat" });
  const abortedAtChange = h.signals[0].aborted;
  h.release();
  await stream;
  assert.equal(abortedAtChange, true);
  assert.deepEqual(h.chunks, []);
  const fresh = await h.handlers.get("ai:get-settings")(h.event);
  assert.equal(fresh.lantaiModelBinding, null);
});
test("re-registering the same editor context preserves its pending stream", async () => {
  const h = deferredStreamHarness();
  const stream = h.handlers.get("ai:stream")(h.event, { requestId: "same-context", messages: [] });
  h.controller.registerEditorWindow({ webContents: h.sender }, { agent_id: "fixture-agent" });
  const abortedAtRegistration = h.signals[0].aborted;
  h.release();
  await stream;
  assert.equal(abortedAtRegistration, false);
  assert.equal(h.chunks.length, 2);
});


test("DOCX retains its legitimate AI settings during workbench startup", async () => {
  const { createDocxEditorController, resolveDocxEditorRuntimePaths } = require("./docxEditorController.cjs");
  const paths = resolveDocxEditorRuntimePaths({ isDev: false, electronDir: "/audit/electron", resourcesPath: "/audit/resources" });
  const renderer = require("node:url").pathToFileURL(paths.rendererFile).href;
  const sender = Object.assign(new EventEmitter(), {
    id: 70, getURL: () => renderer, isDestroyed: () => false,
    mainFrame: { url: renderer, processId: 110, routingId: 1, parent: null },
    setWindowOpenHandler: () => {}, send: () => {},
  });
  const handlers = new Map();
  const ipcMain = { handle: (name, handler) => handlers.set(name, handler), removeHandler: (name) => handlers.delete(name), on: () => {}, removeListener: () => {} };
  const office = createOfficeEditorController({ ipcMain, isDev: false, electronDir: "/audit/electron", resourcesPath: "/audit/resources", resolveOfficeModelOptions: async () => [] });
  office.installOfficeAiHandlers();
  const runtime = { configureDocsRuntime() {}, setDocsShellWindow() {}, registerDocsIpc() {}, registerProjectIpc() {}, createDocsView: () => ({ webContents: sender }) };
  let startupSettings;
  const docx = createDocxEditorController({
    ipcMain, fsImpl: { existsSync: () => true, realpathSync: (p) => p, statSync: () => ({ isFile: () => true, size: 100 }) },
    getMainWindow: () => ({ isDestroyed: () => false }), resolveRuntimePaths: () => paths, loadEditorRuntime: () => runtime,
    registerOfficeAiEditorWindow: (view, context) => office.registerEditorWindow(view, context), installOfficeAiHandlers: () => office.installOfficeAiHandlers(),
    resolveOfficeModelBinding: async () => ({ provider_id: "fixture", model: "fixture-model" }),
    presentOfficeView: async (options) => {
      const view = await options.createView();
      startupSettings = await handlers.get("ai:get-settings")({ sender, senderFrame: sender.mainFrame });
      await options.onActivate(view, { reused: false });
      return { success: true };
    },
  });
  const result = await docx.openLocalDocx({ path: "/tmp/fixture.docx", agent_id: "writing-agent" });
  assert.equal(result.success, true);
  assert.equal(startupSettings.lantaiModelBinding.model, "fixture-model");
});


for (const kind of ['docs', 'sheets', 'slides', 'pdf']) {
  for (const reload of [false, true]) {
    test(`${kind}: asynchronous editor open ${reload ? 'retires the original request at navigation start' : 'preserves a current main document'}`, async () => {
      let release, entered;
      const gate = new Promise(resolve => { release = resolve; });
      const started = new Promise(resolve => { entered = resolve; });
      const mainContents = Object.assign(new EventEmitter(), {
        id: 1, isDestroyed: () => false, getURL: () => 'http://127.0.0.1:8765/#/chat',
        mainFrame: { url: 'http://127.0.0.1:8765/#/chat', parent: null, processId: 100, routingId: 1 },
      });
      let opened = 0;
      const common = {
        ipcMain: { handle() {}, removeHandler() {}, on() {}, removeListener() {} },
        fsImpl: { existsSync: () => true, realpathSync: value => value, statSync: () => ({ isFile: () => true, size: 10 }) },
        getMainWindow: () => ({ isDestroyed: () => false, webContents: mainContents }),
        isTrustedIpcEvent: () => true,
        resolveOfficeModelBinding: async () => { entered(); await gate; return null; },
        presentOfficeView: async () => { opened++; return { success: true }; },
        resolveRuntimePaths: () => kind === 'docs'
          ? { modulePath: '/runtime/main/lantai.js', preloadPath: '/runtime/preload/index.js', rendererFile: '/runtime/renderer/index.html' }
          : { [kind]: { modulePath: '/runtime/main/lantai.js', preloadPath: '/runtime/preload/index.js', rendererFile: '/runtime/renderer/index.html',
              sidecarPath: '/runtime/native/xlsx-sidecar', visionOcrPath: '/runtime/native/vision-ocr' } },
        loadEditorRuntime: () => ({
          configureDocsRuntime() {}, setDocsShellWindow() {}, registerDocsIpc() {}, createDocsView() {},
          configureSheetsRuntime() {}, setSheetsShellWindow() {}, setForcedWorkbookPath() {}, createSheetsView() {},
          configureSlidesRuntime() {}, setSlidesShellWindow() {}, registerSlidesIpc() {}, createSlidesView() {},
          configurePdfRuntime() {}, createPdfView() {}, registerProjectIpc() {},
        }),
      };
      const controller = kind === 'docs' ? createDocxEditorController(common) : createOfficeEditorController(common);
      const suffix = { docs: 'docx', sheets: 'xlsx', slides: 'pptx', pdf: 'pdf' }[kind];
      const pending = controller.openFromIpc({ sender: mainContents, senderFrame: mainContents.mainFrame }, `/synthetic/file.${suffix}`);
      await started;
      if (reload) mainContents.emit('did-start-navigation', {}, mainContents.getURL(), false, true, 100, 2);
      release();
      const result = await pending;
      assert.equal(opened, reload ? 0 : 1);
      assert.equal(result.success, !reload);
    });
  }
}
