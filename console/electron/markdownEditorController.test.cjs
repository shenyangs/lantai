const assert = require("node:assert/strict");
const test = require("node:test");

const {
  MARKDOWN_HANDOFF_MAX_CHARS,
  MARKDOWN_MAX_BYTES,
  createMarkdownEditorController,
} = require("./markdownEditorController.cjs");

function makeHarness({
  size = 12,
  trusted = true,
  mainWindowAvailable = true,
  showMessageBox = async () => ({ response: 1 }),
} = {}) {
  const handlers = new Map();
  const listeners = new Map();
  const calls = {
    loads: [],
    writes: [],
    renames: [],
    bounds: [],
    topmost: [],
    mainSends: [],
    mainShows: [],
  };
  let nextId = 10;
  const windows = new Map();
  class BrowserWindow {
    constructor() {
      this.destroyed = false;
      this.events = new Map();
      this.contents = {
        id: nextId++,
        setWindowOpenHandler() {},
        on() {},
        send() {},
        isDestroyed: () => this.destroyed,
      };
      Object.defineProperty(this, "webContents", {
        get: () => {
          if (this.destroyed) throw new TypeError("Object has been destroyed");
          return this.contents;
        },
      });
      windows.set(this.contents.id, this);
    }
    static fromWebContents(contents) { return windows.get(contents.id); }
    static getFocusedWindow() { return null; }
    isDestroyed() { return this.destroyed; }
    getBounds() { return { x: 0, y: 0, width: 1280, height: 840 }; }
    setBounds(value) { calls.bounds.push(value); }
    setAlwaysOnTop(value) { calls.topmost.push(value); }
    on(event, handler) { this.events.set(event, handler); }
    once(event, handler) { this.events.set(event, handler); }
    show() {}
    destroy() {
      this.destroyed = true;
      this.events.get("closed")?.();
    }
    loadFile(value) { calls.loads.push(value); return Promise.resolve(); }
  }
  const controller = createMarkdownEditorController({
    BrowserWindow,
    Menu: { buildFromTemplate: (value) => value, setApplicationMenu() {} },
    dialog: {
      showOpenDialog: async () => ({ canceled: false, filePaths: ["/tmp/other.md"] }),
      showSaveDialog: async () => ({ canceled: false, filePath: "/tmp/copy.md" }),
      showMessageBox,
    },
    ipcMain: {
      handle: (channel, handler) => handlers.set(channel, handler),
      removeHandler: (channel) => handlers.delete(channel),
      on: (channel, listener) => listeners.set(channel, listener),
      removeAllListeners: (channel) => listeners.delete(channel),
    },
    screen: { getDisplayMatching: () => ({ workArea: { x: 0, y: 24, width: 1400, height: 900 } }) },
    fsImpl: {
      realpathSync: (value) => value,
      statSync: () => ({ isFile: () => true, size }),
      readFileSync: () => "# Hello",
      writeFileSync: (...args) => calls.writes.push(args),
      renameSync: (...args) => calls.renames.push(args),
    },
    getMainWindow: () => mainWindowAvailable ? {
      isDestroyed: () => false,
      webContents: {
        isDestroyed: () => false,
        send: (...args) => calls.mainSends.push(args),
      },
    } : null,
    showMainWindow: (options) => calls.mainShows.push(options),
    isTrustedIpcEvent: () => trusted,
  });
  controller.installIpcHandlers();
  return { calls, controller, handlers, listeners, windows };
}

test("opens a sandboxed Markdown editor and enforces the 100 MB limit", async () => {
  const { calls, controller } = makeHarness();
  assert.deepEqual(await controller.openLocalMarkdown("/tmp/note.md"), { success: true });
  assert.match(calls.loads[0], /markdown-editor\/index\.html$/);
  assert.deepEqual(calls.topmost, [false]);
  const oversized = makeHarness({ size: MARKDOWN_MAX_BYTES + 1 });
  assert.equal((await oversized.controller.openLocalMarkdown("/tmp/huge.md")).error_code, "markdown_too_large");
});

test("reads and atomically saves only a path granted to the Markdown window", async () => {
  const { calls, controller, handlers } = makeHarness();
  await controller.openLocalMarkdown("/tmp/note.md");
  const sender = { id: 10 };
  assert.deepEqual(await handlers.get("lantai:markdown-consume")({ sender }), { path: "/tmp/note.md", text: "# Hello" });
  const saved = await handlers.get("lantai:markdown-save")({ sender }, { text: "updated", saveAs: true });
  assert.deepEqual(saved, { success: true, path: "/tmp/copy.md" });
  assert.equal(calls.writes[0][1], "updated");
  assert.deepEqual(calls.renames[0].slice(1), ["/tmp/copy.md"]);
});

test("rejects untrusted Markdown open IPC", async () => {
  const { handlers } = makeHarness({ trusted: false });
  assert.equal((await handlers.get("lantai:markdown-editor-open")({}, "/tmp/note.md")).error_code, "markdown_untrusted_sender");
});

test("cleans up after BrowserWindow destruction without touching destroyed webContents", async () => {
  const { controller, windows } = makeHarness();
  await controller.openLocalMarkdown("/tmp/note.md");
  assert.doesNotThrow(() => windows.get(10).destroy());
});

test("does not throw when a dirty window is destroyed while close confirmation is open", async () => {
  let resolveDialog;
  const dialogResult = new Promise((resolve) => { resolveDialog = resolve; });
  const { controller, listeners, windows } = makeHarness({
    showMessageBox: () => dialogResult,
  });
  await controller.openLocalMarkdown("/tmp/note.md");
  const window = windows.get(10);
  listeners.get("lantai:markdown-dirty")({ sender: window.contents }, true);
  const close = window.events.get("close");
  close({ preventDefault() {} });
  window.destroy();
  resolveDialog({ response: 1 });
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(window.destroyed, true);
});

test("hands Markdown context to the main chat with an explicit co-creation mode", async () => {
  const { calls, controller, handlers } = makeHarness();
  await controller.openLocalMarkdown("/tmp/note.md");
  const result = await handlers.get("lantai:markdown-handoff-to-chat")(
    { sender: { id: 10 } },
    { mode: "execute", text: "## Selected block", selectionOnly: true },
  );
  assert.deepEqual(result, { success: true });
  assert.equal(calls.mainSends.length, 1);
  assert.equal(calls.mainSends[0][0], "lantai:markdown-chat-handoff");
  assert.deepEqual(calls.mainSends[0][1], {
    protocol_version: "2.0",
    mode: "execute",
    path: "/tmp/note.md",
    document_name: "note.md",
    text: "## Selected block",
    selection_only: true,
  });
  assert.deepEqual(calls.mainShows, [{ focus: true }]);
});

test("Markdown handoff is bounded and fails closed when the main window is unavailable", async () => {
  const unavailable = makeHarness({ mainWindowAvailable: false });
  await unavailable.controller.openLocalMarkdown("/tmp/note.md");
  const sender = { sender: { id: 10 } };
  assert.equal(
    (await unavailable.handlers.get("lantai:markdown-handoff-to-chat")(sender, { text: "hello" })).error_code,
    "markdown_main_window_unavailable",
  );

  const bounded = makeHarness();
  await bounded.controller.openLocalMarkdown("/tmp/note.md");
  assert.equal(
    (await bounded.handlers.get("lantai:markdown-handoff-to-chat")(
      sender,
      { text: "x".repeat(MARKDOWN_HANDOFF_MAX_CHARS + 1) },
    )).error_code,
    "markdown_handoff_too_large",
  );
  assert.equal(bounded.calls.mainSends.length, 0);
});
