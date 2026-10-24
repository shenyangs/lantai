const assert = require("node:assert/strict");
const fs = require("node:fs");
const http = require("node:http");
const os = require("node:os");
const path = require("node:path");
const { pathToFileURL } = require("node:url");
const test = require("node:test");
const { EventEmitter } = require("node:events");

function currentFrame(contents, url) {
  contents.url = url;
  contents.mainFrame = { url, parent: null, processId: 1, routingId: contents.id };
  return contents.mainFrame;
}
function rendererContents(id) {
  const contents = new EventEmitter();
  contents.id = id; contents.getURL = () => contents.url; contents.isDestroyed = () => false;
  currentFrame(contents, "http://127.0.0.1:19090/chat");
  return contents;
}

const {
  getNativeComputerPermissionStatus,
  installDesktopIpcHandlers,
  readNativeClipboardAttachments,
  uploadChatAttachmentThroughBackend,
} = require("./desktopIpcController.cjs");
const { buildBackendLaunchPlan } = require("./backendProcess.cjs");

function makeHarness(overrides = {}) {
  const handlers = new Map();
  const mainWebContents = rendererContents(7);
  const calls = {
    dialog: 0,
    openExternal: [],
    openPath: 0,
    revealPath: 0,
    openWith: 0,
  };

  installDesktopIpcHandlers({
    app: { getVersion: () => "2.0.25" },
    ipcMain: {
      handle: (channel, handler) => handlers.set(channel, handler),
    },
    dialog: {
      showOpenDialog: async () => {
        calls.dialog += 1;
        return { canceled: false, filePaths: ["/tmp/project"] };
      },
    },
    shell: {
      openExternal: async (url) => {
        calls.openExternal.push(url);
      },
      openPath: async () => {
        calls.openPath += 1;
        return "";
      },
      showItemInFolder: () => {
        calls.revealPath += 1;
      },
    },
    execFileImpl: (_command, _args, callback) => {
      calls.openWith += 1;
      callback(null);
    },
    platform: "darwin",
    getMainWindow: () => ({
      isDestroyed: () => false,
      webContents: mainWebContents,
    }),
    getAllowedOrigins: () => ["http://127.0.0.1:19090"],
    ...overrides,
  });

  return {
    calls,
    handlers,
    trustedEvent: {
      sender: mainWebContents,
      senderFrame: mainWebContents.mainFrame,
    },
    forgedSenderEvent: {
      sender: { id: 99 },
      senderFrame: {
        url: "http://127.0.0.1:19090/chat",
        parent: null,
      },
    },
    externalFrameEvent: {
      sender: mainWebContents,
      senderFrame: {
        url: "https://evil.example/chat",
        parent: null,
      },
    },
  };
}

test("desktop IPC copies generated image bytes instead of URL text", async () => {
  const imageBytes = Buffer.from("generated-image");
  let clipboardImage = null;
  const { handlers, trustedEvent } = makeHarness({
    getApiBaseUrl: () => "http://127.0.0.1:19090",
    getDesktopToken: () => "desktop-token",
    clipboard: {
      writeImage: (image) => {
        clipboardImage = image;
      },
    },
    nativeImage: {
      createFromBuffer: (buffer) => ({
        bytes: buffer,
        isEmpty: () => false,
      }),
    },
    fetchImpl: async (url, options) => {
      assert.equal(url, "https://images.example/generated.png");
      assert.equal(options.headers, undefined);
      assert.equal(options.redirect, "follow");
      return {
        ok: true,
        status: 200,
        headers: {
          get: (name) =>
            String(name).toLowerCase() === "content-type" ? "image/png" : null,
        },
        arrayBuffer: async () => Uint8Array.from(imageBytes).buffer,
      };
    },
  });

  const result = await handlers.get("lantai:copy-image-from-url")(
    trustedEvent,
    { url: "https://images.example/generated.png" },
  );

  assert.deepEqual(result, { success: true });
  assert.deepEqual(clipboardImage.bytes, imageBytes);
});

test("desktop IPC authorizes generated images served by the current backend", async () => {
  let requestOptions = null;
  const { handlers, trustedEvent } = makeHarness({
    getApiBaseUrl: () => "http://127.0.0.1:19090",
    getDesktopToken: () => "desktop-token",
    clipboard: {
      writeImage: () => {},
    },
    nativeImage: {
      createFromBuffer: () => ({ isEmpty: () => false }),
    },
    fetchImpl: async (_url, options) => {
      requestOptions = options;
      return {
        ok: true,
        status: 200,
        headers: {
          get: (name) =>
            String(name).toLowerCase() === "content-type" ? "image/png" : null,
        },
        arrayBuffer: async () => Uint8Array.from([1, 2, 3]).buffer,
      };
    },
  });

  const result = await handlers.get("lantai:copy-image-from-url")(
    trustedEvent,
    {
      url: "http://127.0.0.1:19090/api/files/preview/generated.png",
    },
  );

  assert.deepEqual(result, { success: true });
  assert.equal(
    requestOptions.headers["X-Lantai-Desktop-Token"],
    "desktop-token",
  );
});

test("desktop IPC saves generated image bytes to the selected file", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "lantai-image-save-"));
  const destination = path.join(root, "generated.jpeg");
  const imageBytes = Buffer.from("downloaded-image");
  const { handlers, trustedEvent } = makeHarness({
    getApiBaseUrl: () => "http://127.0.0.1:19090",
    getDesktopToken: () => "desktop-token",
    app: {
      getPath: (name) => {
        assert.equal(name, "downloads");
        return root;
      },
    },
    dialog: {
      showSaveDialog: async (_window, options) => {
        assert.equal(options.defaultPath, path.join(root, "generated.jpeg"));
        assert.deepEqual(options.filters[0].extensions, ["jpg", "jpeg"]);
        return { canceled: false, filePath: destination };
      },
    },
    fetchImpl: async (_url, options) => {
      assert.equal(options.headers["X-Lantai-Desktop-Token"], "desktop-token");
      assert.equal(options.redirect, "error");
      return {
        ok: true,
        status: 200,
        headers: {
          get: (name) =>
            String(name).toLowerCase() === "content-type" ? "image/jpeg" : null,
        },
        arrayBuffer: async () => Uint8Array.from(imageBytes).buffer,
      };
    },
  });

  const result = await handlers.get("lantai:save-image-from-url")(
    trustedEvent,
    {
      url: "http://127.0.0.1:19090/api/files/preview/generated.jpeg?source=local",
    },
  );

  assert.deepEqual(result, { success: true, path: destination });
  assert.deepEqual(fs.readFileSync(destination), imageBytes);
});

test("desktop IPC copies a trusted local generated image before trying an expired URL", async () => {
  const root = fs.mkdtempSync(
    path.join(os.tmpdir(), "lantai-copy-local-image-"),
  );
  const userData = path.join(root, "user-data");
  const source = path.join(
    userData,
    "working",
    "media",
    "generated_images",
    "source.png",
  );
  fs.mkdirSync(path.dirname(source), { recursive: true });
  fs.writeFileSync(source, Buffer.from("local-image"));
  let clipboardImage = null;
  const { handlers, trustedEvent } = makeHarness({
    app: {
      getVersion: () => "2.0.25",
      getPath: (name) => (name === "userData" ? userData : root),
    },
    clipboard: {
      writeImage: (image) => {
        clipboardImage = image;
      },
    },
    nativeImage: {
      createFromBuffer: (buffer) => ({
        bytes: buffer,
        isEmpty: () => false,
      }),
    },
    fetchImpl: async () => {
      throw new Error("remote image must not be fetched");
    },
  });

  const result = await handlers.get("lantai:copy-image-from-url")(
    trustedEvent,
    { url: "https://images.example/expired.png", localPath: source },
  );

  assert.deepEqual(result, { success: true });
  assert.equal(clipboardImage.bytes.toString(), "local-image");
});

test("desktop IPC tries every trusted generated-image path before an expired URL", async () => {
  const root = fs.mkdtempSync(
    path.join(os.tmpdir(), "lantai-copy-local-image-candidates-"),
  );
  const userData = path.join(root, "user-data");
  const managedSource = path.join(
    userData,
    "working",
    "media",
    "generated_images",
    "source.png",
  );
  const externalWorkspaceSource = path.join(root, "external", "source.png");
  fs.mkdirSync(path.dirname(managedSource), { recursive: true });
  fs.mkdirSync(path.dirname(externalWorkspaceSource), { recursive: true });
  fs.writeFileSync(managedSource, Buffer.from("managed-local-image"));
  fs.writeFileSync(
    externalWorkspaceSource,
    Buffer.from("external-local-image"),
  );
  let clipboardImage = null;
  let fetchCalls = 0;
  const { handlers, trustedEvent } = makeHarness({
    app: {
      getVersion: () => "2.0.25",
      getPath: (name) => (name === "userData" ? userData : root),
    },
    clipboard: {
      writeImage: (image) => {
        clipboardImage = image;
      },
    },
    nativeImage: {
      createFromBuffer: (buffer) => ({
        bytes: buffer,
        isEmpty: () => false,
      }),
    },
    fetchImpl: async () => {
      fetchCalls += 1;
      throw new Error("expired URL must not be fetched");
    },
  });

  const result = await handlers.get("lantai:copy-image-from-url")(
    trustedEvent,
    {
      url: "https://images.example/expired.png",
      localPaths: [managedSource, externalWorkspaceSource],
    },
  );

  assert.deepEqual(result, { success: true });
  assert.equal(clipboardImage.bytes.toString(), "managed-local-image");
  assert.equal(fetchCalls, 0);
});

test("packaged desktop trusts the backend's overridden generated-image working root", async () => {
  const root = fs.mkdtempSync(
    path.join(os.tmpdir(), "lantai-copy-custom-working-image-"),
  );
  const userData = path.join(root, "user-data");
  const customWorking = path.join(root, "custom-working");
  const plan = buildBackendLaunchPlan({
    isDev: false,
    electronDir: __dirname,
    resourcesPath: path.join(root, "resources"),
    userDataPath: userData,
    baseEnv: { LANTAI_WORKING_DIR: customWorking },
    backendPort: 19090,
    desktopToken: "desktop-token",
  });
  const managedSource = path.join(
    customWorking,
    "media",
    "generated_images",
    "source.png",
  );
  fs.mkdirSync(path.dirname(managedSource), { recursive: true });
  fs.writeFileSync(managedSource, Buffer.from("custom-working-local-image"));
  let clipboardImage = null;
  let fetchCalls = 0;
  const { handlers, trustedEvent } = makeHarness({
    app: {
      getVersion: () => "2.0.25",
      getPath: (name) => (name === "userData" ? userData : root),
    },
    getBackendWorkingDir: () => plan.backendWorkingDir,
    clipboard: {
      writeImage: (image) => {
        clipboardImage = image;
      },
    },
    nativeImage: {
      createFromBuffer: (buffer) => ({
        bytes: buffer,
        isEmpty: () => false,
      }),
    },
    fetchImpl: async () => {
      fetchCalls += 1;
      throw new Error("expired URL must not be fetched");
    },
  });

  const result = await handlers.get("lantai:copy-image-from-url")(
    trustedEvent,
    {
      url: "https://images.example/expired.png",
      localPaths: [managedSource],
    },
  );

  assert.equal(plan.env.LANTAI_WORKING_DIR, customWorking);
  assert.equal(plan.backendWorkingDir, customWorking);
  assert.deepEqual(result, { success: true });
  assert.equal(clipboardImage.bytes.toString(), "custom-working-local-image");
  assert.equal(fetchCalls, 0);
});

test("packaged desktop saves from the backend's overridden generated-image working root", async () => {
  const root = fs.mkdtempSync(
    path.join(os.tmpdir(), "lantai-save-custom-working-image-"),
  );
  const userData = path.join(root, "user-data");
  const downloads = path.join(root, "Downloads");
  const customWorking = path.join(root, "custom-working");
  const plan = buildBackendLaunchPlan({
    isDev: false,
    electronDir: __dirname,
    resourcesPath: path.join(root, "resources"),
    userDataPath: userData,
    baseEnv: { LANTAI_WORKING_DIR: customWorking },
    backendPort: 19090,
    desktopToken: "desktop-token",
  });
  const managedSource = path.join(
    customWorking,
    "media",
    "generated_images",
    "source.png",
  );
  const destination = path.join(downloads, "source.png");
  const sourceBytes = Buffer.from("custom-working-saved-image");
  fs.mkdirSync(path.dirname(managedSource), { recursive: true });
  fs.mkdirSync(downloads, { recursive: true });
  fs.writeFileSync(managedSource, sourceBytes);
  let fetchCalls = 0;
  const { handlers, trustedEvent } = makeHarness({
    app: {
      getVersion: () => "2.0.25",
      getPath: (name) => {
        if (name === "userData") return userData;
        if (name === "downloads") return downloads;
        return root;
      },
    },
    getBackendWorkingDir: () => plan.backendWorkingDir,
    dialog: {
      showSaveDialog: async (_window, options) => {
        assert.equal(options.defaultPath, destination);
        return { canceled: false, filePath: destination };
      },
    },
    fetchImpl: async () => {
      fetchCalls += 1;
      throw new Error("expired URL must not be fetched");
    },
  });

  const result = await handlers.get("lantai:save-image-from-url")(
    trustedEvent,
    {
      url: "https://images.example/expired.png",
      localPaths: [managedSource],
    },
  );

  assert.equal(plan.backendWorkingDir, customWorking);
  assert.deepEqual(result, { success: true, path: destination });
  assert.deepEqual(fs.readFileSync(destination), sourceBytes);
  assert.equal(fetchCalls, 0);
});

test("desktop IPC refuses an untrusted local image path and falls back to the remote URL", async () => {
  const root = fs.mkdtempSync(
    path.join(os.tmpdir(), "lantai-copy-untrusted-image-"),
  );
  const userData = path.join(root, "user-data");
  const outsideSource = path.join(root, "outside.png");
  fs.mkdirSync(path.join(userData, "working"), { recursive: true });
  fs.writeFileSync(outsideSource, Buffer.from("private-local-image"));
  let clipboardImage = null;
  let fetched = false;
  const { handlers, trustedEvent } = makeHarness({
    app: {
      getVersion: () => "2.0.25",
      getPath: (name) => (name === "userData" ? userData : root),
    },
    clipboard: {
      writeImage: (image) => {
        clipboardImage = image;
      },
    },
    nativeImage: {
      createFromBuffer: (buffer) => ({
        bytes: buffer,
        isEmpty: () => false,
      }),
    },
    fetchImpl: async () => {
      fetched = true;
      return {
        ok: true,
        headers: new Map([["content-type", "image/png"]]),
        arrayBuffer: async () => Buffer.from("remote-image"),
      };
    },
  });

  const result = await handlers.get("lantai:copy-image-from-url")(
    trustedEvent,
    { url: "https://images.example/fallback.png", localPath: outsideSource },
  );

  assert.deepEqual(result, { success: true });
  assert.equal(fetched, true);
  assert.equal(clipboardImage.bytes.toString(), "remote-image");
});

test("desktop IPC saves a trusted local generated image to Downloads by default", async () => {
  const root = fs.mkdtempSync(
    path.join(os.tmpdir(), "lantai-save-local-image-"),
  );
  const userData = path.join(root, "user-data");
  const downloads = path.join(root, "Downloads");
  const source = path.join(
    userData,
    "working",
    "media",
    "generated_images",
    "source.png",
  );
  const destination = path.join(downloads, "source.png");
  fs.mkdirSync(path.dirname(source), { recursive: true });
  fs.mkdirSync(downloads, { recursive: true });
  fs.writeFileSync(source, Buffer.from("local-download"));
  const { handlers, trustedEvent } = makeHarness({
    app: {
      getVersion: () => "2.0.25",
      getPath: (name) => {
        if (name === "userData") return userData;
        if (name === "downloads") return downloads;
        return root;
      },
    },
    dialog: {
      showSaveDialog: async (_window, options) => {
        assert.equal(options.defaultPath, destination);
        return { canceled: false, filePath: destination };
      },
    },
    fetchImpl: async () => {
      throw new Error("remote image must not be fetched");
    },
  });

  const result = await handlers.get("lantai:save-image-from-url")(
    trustedEvent,
    { url: "https://images.example/expired.png", localPath: source },
  );

  assert.deepEqual(result, { success: true, path: destination });
  assert.equal(fs.readFileSync(destination, "utf8"), "local-download");
});

test("desktop IPC saves a generated local file with byte-identical contents", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "lantai-file-save-"));
  const source = path.join(root, "Agent 生成 报告.md");
  const destination = path.join(root, "用户保存 报告.md");
  const sourceBytes = Buffer.from(
    "# deliverable\nFILE_SENTINEL_92ac\n",
    "utf8",
  );
  fs.writeFileSync(source, sourceBytes);
  const { handlers, trustedEvent } = makeHarness({
    app: { getPath: (name) => (name === "downloads" ? root : "") },
    dialog: {
      showSaveDialog: async (_window, options) => {
        assert.equal(options.defaultPath, path.join(root, "交付报告.md"));
        return { canceled: false, filePath: destination };
      },
    },
  });

  const handler = handlers.get("lantai:save-file-as");
  assert.equal(typeof handler, "function");
  const result = await handler(trustedEvent, {
    sourcePath: source,
    filename: "交付报告.md",
  });

  assert.deepEqual(result, {
    success: true,
    path: destination,
    size_bytes: sourceBytes.length,
  });
  assert.deepEqual(fs.readFileSync(destination), sourceBytes);
});

test("desktop IPC rejects forged senders before saving generated files", async () => {
  const { handlers, forgedSenderEvent } = makeHarness();

  const result = await handlers.get("lantai:save-file-as")(forgedSenderEvent, {
    sourcePath: "/tmp/private.txt",
    filename: "private.txt",
  });

  assert.deepEqual(result, {
    success: false,
    error: "untrusted_ipc_sender",
  });
});

test("desktop IPC rejects forged senders before opening directory picker", async () => {
  const { calls, handlers, forgedSenderEvent } = makeHarness();

  const result = await handlers.get("lantai:select-directory")(
    forgedSenderEvent,
  );

  assert.equal(result, null);
  assert.equal(calls.dialog, 0);
});

test("desktop IPC accepts trusted main-window sender", async () => {
  const { calls, handlers, trustedEvent } = makeHarness();

  const result = await handlers.get("lantai:select-directory")(trustedEvent);

  assert.equal(result, "/tmp/project");
  assert.equal(calls.dialog, 1);
});

test("native computer permission status is read from the Electron app process", () => {
  const status = getNativeComputerPermissionStatus({
    app: {
      getPath: (name) => {
        assert.equal(name, "exe");
        return "/Applications/澜台.app/Contents/MacOS/澜台";
      },
    },
    platform: "darwin",
    systemPreferences: {
      getMediaAccessStatus: () => "not-determined",
      isTrustedAccessibilityClient: (prompt) => {
        assert.equal(prompt, false);
        return false;
      },
    },
  });

  assert.deepEqual(status, {
    ok: true,
    platform: "darwin",
    app_path: "/Applications/澜台.app",
    permissions: [
      { key: "accessibility", status: "denied", can_check: true },
      { key: "screen_recording", status: "unknown", can_check: true },
    ],
  });
});

test("desktop IPC never requests microphone access", async () => {
  let requested = "";
  const { handlers, trustedEvent } = makeHarness({
    systemPreferences: {
      askForMediaAccess: async (kind) => {
        requested = kind;
        return true;
      },
      getMediaAccessStatus: () => "granted",
    },
  });

  const result = await handlers.get("lantai:computer-permissions-request")(
    trustedEvent,
    "microphone",
  );

  assert.equal(requested, "");
  assert.deepEqual(result, {
    ok: false,
    key: "microphone",
    status: "unknown",
    error: "This permission must be enabled in System Settings.",
  });
});

test("desktop permission IPC is wired through main and preload", () => {
  const preload = fs.readFileSync(path.join(__dirname, "preload.js"), "utf8");
  const main = fs.readFileSync(path.join(__dirname, "main.cjs"), "utf8");

  assert.match(preload, /lantai:computer-permissions-status/);
  assert.match(preload, /lantai:computer-permissions-request/);
  assert.match(main, /systemPreferences,/);
  assert.match(
    main,
    /installDesktopIpcHandlers\(\{[\s\S]*?systemPreferences,[\s\S]*?\}\);/,
  );
});

test("desktop chat attachment IPC uploads through the main process transport", async () => {
  let capturedUrl = "";
  let capturedOptions = null;
  const { handlers, trustedEvent } = makeHarness({
    getApiBaseUrl: () => "http://127.0.0.1:19090",
    getDesktopToken: () => "desktop-token",
    fetchImpl: async (url, options) => {
      capturedUrl = url;
      capturedOptions = options;
      return {
        ok: true,
        status: 200,
        json: async () => ({
          url: "/tmp/stored_clipboard.png",
          file_name: "clipboard.png",
          stored_name: "digest_clipboard.png",
          size: 11,
          digest: "sha256:clipboard",
          version: 1,
          upload_receipt: "upload-receipt:v1:desktop",
        }),
      };
    },
  });

  const bytes = Uint8Array.from(Buffer.from("small-image")).buffer;
  const result = await handlers.get("lantai:upload-chat-attachment")(
    trustedEvent,
    {
      fileName: "clipboard.png",
      mimeType: "image/png",
      bytes,
      agentId: "default",
      workspaceGrantId: "wsg_example123",
    },
  );

  assert.deepEqual(result, {
    ok: true,
    url: "/tmp/stored_clipboard.png",
    file_name: "clipboard.png",
    stored_name: "digest_clipboard.png",
    size: 11,
    digest: "sha256:clipboard",
    version: 1,
    upload_receipt: "upload-receipt:v1:desktop",
  });
  assert.equal(capturedUrl, "http://127.0.0.1:19090/api/console/upload");
  assert.equal(capturedOptions.method, "POST");
  assert.equal(
    capturedOptions.headers["X-Lantai-Desktop-Token"],
    "desktop-token",
  );
  assert.equal(capturedOptions.headers["X-Agent-Id"], "default");
  assert.equal(
    capturedOptions.headers["X-Workspace-Grant-Id"],
    "wsg_example123",
  );
  const uploadedFile = capturedOptions.body.get("file");
  assert.equal(uploadedFile.name, "clipboard.png");
  assert.equal(uploadedFile.type, "image/png");
  assert.equal(
    Buffer.from(await uploadedFile.arrayBuffer()).toString(),
    "small-image",
  );
});

test("desktop chat attachment transport sends real multipart bytes to localhost", async (t) => {
  let receivedBody = Buffer.alloc(0);
  let receivedDesktopToken = "";
  const server = http.createServer((request, response) => {
    const chunks = [];
    receivedDesktopToken = String(
      request.headers["x-lantai-desktop-token"] || "",
    );
    request.on("data", (chunk) => chunks.push(Buffer.from(chunk)));
    request.on("end", () => {
      receivedBody = Buffer.concat(chunks);
      response.writeHead(200, { "Content-Type": "application/json" });
      response.end(
        JSON.stringify({
          url: "/tmp/real_transport.png",
          file_name: "real.png",
        }),
      );
    });
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  t.after(() => new Promise((resolve) => server.close(resolve)));
  const address = server.address();
  assert.equal(typeof address, "object");

  const result = await uploadChatAttachmentThroughBackend({
    payload: {
      fileName: "real.png",
      mimeType: "image/png",
      bytes: Uint8Array.from(Buffer.from("real-image-bytes")).buffer,
    },
    getApiBaseUrl: () => `http://127.0.0.1:${address.port}`,
    getDesktopToken: () => "desktop-token",
  });

  assert.equal(result.ok, true);
  assert.equal(result.url, "/tmp/real_transport.png");
  assert.equal(receivedDesktopToken, "desktop-token");
  assert.match(receivedBody.toString("latin1"), /real\.png/);
  assert.match(receivedBody.toString("latin1"), /real-image-bytes/);
});

test("desktop chat attachment IPC rejects forged senders", async () => {
  let called = false;
  const { handlers, forgedSenderEvent } = makeHarness({
    getApiBaseUrl: () => "http://127.0.0.1:19090",
    getDesktopToken: () => "desktop-token",
    fetchImpl: async () => {
      called = true;
      throw new Error("must not run");
    },
  });

  const result = await handlers.get("lantai:upload-chat-attachment")(
    forgedSenderEvent,
    {
      fileName: "private.txt",
      mimeType: "text/plain",
      bytes: Uint8Array.from([1]).buffer,
    },
  );

  assert.deepEqual(result, { ok: false, error: "untrusted_ipc_sender" });
  assert.equal(called, false);
});

test("desktop chat attachment upload aborts a stuck backend request", async () => {
  const startedAt = Date.now();
  const result = await uploadChatAttachmentThroughBackend({
    payload: {
      fileName: "stuck.txt",
      mimeType: "text/plain",
      bytes: Uint8Array.from([1]).buffer,
    },
    getApiBaseUrl: () => "http://127.0.0.1:19090",
    getDesktopToken: () => "desktop-token",
    timeoutMs: 5,
    fetchImpl: async (_url, options) =>
      new Promise((_resolve, reject) => {
        options.signal.addEventListener("abort", () => {
          reject(options.signal.reason);
        });
      }),
  });

  assert.equal(result.ok, false);
  assert.equal(result.error_code, "upload_timeout");
  assert.ok(Date.now() - startedAt < 500);
});

test("desktop chat attachment IPC is wired through main and preload", () => {
  const preload = fs.readFileSync(path.join(__dirname, "preload.js"), "utf8");
  const main = fs.readFileSync(path.join(__dirname, "main.cjs"), "utf8");

  assert.match(preload, /lantai:upload-chat-attachment/);
  assert.match(
    main,
    /installDesktopIpcHandlers\(\{[\s\S]*?getApiBaseUrl:[\s\S]*?getDesktopToken:[\s\S]*?\}\);/,
  );
});

test("desktop IPC runs an HTML work in a sandboxed Lantai child window", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "lantai-work-"));
  const htmlPath = path.join(root, "tetris.html");
  fs.writeFileSync(htmlPath, "<!doctype html><title>Tetris</title>");
  let options = null;
  let loadedPath = null;
  let shown = false;
  class FakeBrowserWindow {
    constructor(value) {
      options = value;
      this.webContents = {
        setWindowOpenHandler: () => undefined,
        on: () => undefined,
      };
    }
    async loadFile(value) {
      loadedPath = value;
    }
    isDestroyed() {
      return false;
    }
    show() {
      shown = true;
    }
    destroy() {}
  }
  const { handlers, trustedEvent } = makeHarness({
    BrowserWindow: FakeBrowserWindow,
  });

  const result = await handlers.get("lantai:open-html-work")(
    trustedEvent,
    htmlPath,
    "俄罗斯方块",
  );

  assert.deepEqual(result, { success: true });
  assert.equal(loadedPath, fs.realpathSync(htmlPath));
  assert.equal(shown, true);
  assert.equal(options.title, "俄罗斯方块");
  assert.equal(options.webPreferences.nodeIntegration, false);
  assert.equal(options.webPreferences.contextIsolation, true);
  assert.equal(options.webPreferences.sandbox, true);
});

test("desktop IPC creates a native first-page thumbnail for a work", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "lantai-work-cover-"));
  const workPath = path.join(root, "deck.pptx");
  fs.writeFileSync(workPath, "deck");
  let requestedPath = "";
  let requestedSize = null;
  const { handlers, trustedEvent } = makeHarness({
    nativeImage: {
      createThumbnailFromPath: async (targetPath, size) => {
        requestedPath = targetPath;
        requestedSize = size;
        return {
          isEmpty: () => false,
          toDataURL: () => "data:image/png;base64,dGh1bWI=",
        };
      },
    },
  });

  const result = await handlers.get("lantai:create-work-thumbnail")(
    trustedEvent,
    workPath,
  );

  assert.deepEqual(result, {
    success: true,
    data_url: "data:image/png;base64,dGh1bWI=",
  });
  assert.equal(requestedPath, fs.realpathSync(workPath));
  assert.deepEqual(requestedSize, { width: 960, height: 600 });
});

test("desktop clipboard bridge returns the real native image instead of its filename text", async () => {
  const result = await readNativeClipboardAttachments({
    clipboard: {
      availableFormats: () => ["text/plain", "image/png"],
      readImage: () => ({
        isEmpty: () => false,
        toPNG: () => Buffer.from("image"),
      }),
    },
    payload: {
      preferredName: "image.png",
      mimeTypes: ["text/plain", "image/png"],
    },
  });

  assert.deepEqual(result, {
    attachments: [
      {
        name: "image.png",
        type: "image/png",
        size: 5,
        lastModified: 0,
        dataBase64: Buffer.from("image").toString("base64"),
      },
    ],
  });
});

test("desktop clipboard bridge reads common Finder file URLs as binary attachments", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "lantai-clipboard-"));
  const documentPath = path.join(root, "brief.docx");
  fs.writeFileSync(documentPath, "office");

  const result = await readNativeClipboardAttachments({
    clipboard: {
      availableFormats: () => ["public.file-url"],
      read: () => pathToFileURL(documentPath).href,
      readBuffer: () => Buffer.alloc(0),
      readImage: () => ({ isEmpty: () => true }),
    },
  });

  assert.equal(result.attachments.length, 1);
  assert.equal(result.attachments[0].name, "brief.docx");
  assert.equal(result.attachments[0].size, 6);
  assert.equal(
    Buffer.from(result.attachments[0].dataBase64, "base64").toString("utf8"),
    "office",
  );
});

test("workspace picker returns a backend-verifiable selection receipt", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "lantai-project-picker-"));
  const project = path.join(root, "alpha");
  fs.mkdirSync(project);
  const handlers = new Map();
  const webContents = rendererContents(71);
  installDesktopIpcHandlers({
    app: { getPath: () => root },
    ipcMain: { handle: (channel, handler) => handlers.set(channel, handler) },
    dialog: {
      showOpenDialog: async () => ({
        canceled: false,
        filePaths: [project],
      }),
    },
    shell: { openPath: async () => "" },
    getMainWindow: () => ({ isDestroyed: () => false, webContents }),
    getAllowedOrigins: () => ["http://127.0.0.1:19090"],
    getDesktopToken: () => "desktop-secret",
    now: () => new Date("2026-07-27T10:00:00.000Z"),
    randomId: () => "selection-1",
  });
  const event = {
    sender: webContents,
    senderFrame: currentFrame(webContents, "http://127.0.0.1:19090/coding"),
  };

  const result = await handlers.get("lantai:select-workspace-grant")(event, {
    agentId: "default",
    accessMode: "read_write",
    trustState: "trusted",
  });

  assert.equal(result.ok, true);
  assert.equal(result.projectRoot, project);
  assert.equal(result.displayName, "alpha");
  const [payloadSegment, signature] = result.selectionReceipt.split(".");
  const expectedSignature = require("node:crypto")
    .createHmac("sha256", "desktop-secret")
    .update(payloadSegment)
    .digest("hex");
  assert.equal(signature, expectedSignature);
  const payload = JSON.parse(
    Buffer.from(payloadSegment, "base64url").toString("utf8"),
  );
  assert.equal(payload.agent_id, "default");
  assert.equal(payload.project_root, project);
  assert.equal(payload.nonce, "selection-1");
  assert.equal(payload.distribution, "official");
});

test("MAS workspace picker persists bookmark before signing its reference", async () => {
  const handlers = new Map();
  const webContents = rendererContents(72);
  let bookmarkInput = null;
  installDesktopIpcHandlers({
    app: { getPath: () => "/tmp/user-data" },
    ipcMain: { handle: (channel, handler) => handlers.set(channel, handler) },
    dialog: {
      showOpenDialog: async (_window, options) => {
        assert.equal(options.securityScopedBookmarks, true);
        return {
          canceled: false,
          filePaths: ["/tmp/project"],
          bookmarks: ["mas-bookmark"],
        };
      },
    },
    shell: { openPath: async () => "" },
    getMainWindow: () => ({ isDestroyed: () => false, webContents }),
    getAllowedOrigins: () => ["http://127.0.0.1:19090"],
    getDesktopToken: () => "desktop-secret",
    distributionChannel: "mas",
    workspaceBookmarkStore: {
      authorizeSelection: (input) => {
        bookmarkInput = input;
        return { ok: true, securityScopeRef: "wgb_ref-1" };
      },
    },
  });
  const event = {
    sender: webContents,
    senderFrame: currentFrame(webContents, "http://127.0.0.1:19090/coding"),
  };

  const result = await handlers.get("lantai:select-workspace-grant")(event, {
    agentId: "default",
  });
  const payload = JSON.parse(
    Buffer.from(result.selectionReceipt.split(".")[0], "base64url").toString(
      "utf8",
    ),
  );

  assert.deepEqual(bookmarkInput, {
    agentId: "default",
    projectRoot: "/tmp/project",
    bookmark: "mas-bookmark",
  });
  assert.equal(payload.distribution, "mas");
  assert.equal(payload.security_scope_ref, "wgb_ref-1");
});

test("desktop IPC rejects external frame origins before opening paths", async () => {
  const { calls, handlers, externalFrameEvent } = makeHarness();

  const result = await handlers.get("lantai:open-path")(
    externalFrameEvent,
    "/tmp/project",
  );

  assert.deepEqual(result, {
    success: false,
    error: "untrusted_ipc_sender",
  });
  assert.equal(calls.openPath, 0);
});

test("desktop IPC opens safe web links in the system browser", async () => {
  const { calls, handlers, trustedEvent } = makeHarness();

  const result = await handlers.get("lantai:open-external")(
    trustedEvent,
    "https://github.com/shenyangs",
  );

  assert.deepEqual(result, { success: true });
  assert.deepEqual(calls.openExternal, ["https://github.com/shenyangs"]);
});

test("desktop IPC rejects unsafe external-link protocols", async () => {
  const { calls, handlers, trustedEvent } = makeHarness();

  const result = await handlers.get("lantai:open-external")(
    trustedEvent,
    "javascript:alert(1)",
  );

  assert.deepEqual(result, {
    success: false,
    error: "Only HTTP and HTTPS links can be opened",
  });
  assert.deepEqual(calls.openExternal, []);
});

test("desktop IPC reveals trusted paths in Finder", async () => {
  const { calls, handlers, trustedEvent } = makeHarness();

  const result = await handlers.get("lantai:reveal-path")(
    trustedEvent,
    "/tmp/project/index.html",
  );

  assert.deepEqual(result, { success: true });
  assert.equal(calls.revealPath, 1);
});

test("desktop IPC lets the user choose a macOS application for a file", async () => {
  const handlers = new Map();
  const webContents = rendererContents(17);
  let opened = null;
  installDesktopIpcHandlers({
    ipcMain: { handle: (channel, handler) => handlers.set(channel, handler) },
    dialog: {
      showOpenDialog: async (_window, options) => {
        assert.equal(options.defaultPath, "/Applications");
        return {
          canceled: false,
          filePaths: ["/Applications/Safari.app"],
        };
      },
    },
    shell: {
      openPath: async () => "",
      showItemInFolder: () => {},
    },
    execFileImpl: (command, args, callback) => {
      opened = { command, args };
      callback(null);
    },
    platform: "darwin",
    getMainWindow: () => ({ isDestroyed: () => false, webContents }),
    getAllowedOrigins: () => ["http://127.0.0.1:19090"],
  });
  const event = {
    sender: webContents,
    senderFrame: currentFrame(webContents, "http://127.0.0.1:19090/chat"),
  };

  const result = await handlers.get("lantai:open-with")(
    event,
    "/tmp/project/index.html",
  );

  assert.deepEqual(result, { success: true });
  assert.deepEqual(opened, {
    command: "/usr/bin/open",
    args: ["-a", "/Applications/Safari.app", "/tmp/project/index.html"],
  });
});

test("MAS open-with falls back to Finder without spawning a process", async () => {
  const handlers = new Map();
  const webContents = rendererContents(18);
  let revealed = "";
  let spawned = false;
  installDesktopIpcHandlers({
    ipcMain: { handle: (channel, handler) => handlers.set(channel, handler) },
    dialog: { showOpenDialog: async () => ({ canceled: true, filePaths: [] }) },
    shell: {
      openPath: async () => "",
      showItemInFolder: (target) => {
        revealed = target;
      },
    },
    execFileImpl: () => {
      spawned = true;
    },
    platform: "darwin",
    distributionChannel: "mas",
    getMainWindow: () => ({ isDestroyed: () => false, webContents }),
    getAllowedOrigins: () => ["http://127.0.0.1:19090"],
  });
  const event = {
    sender: webContents,
    senderFrame: currentFrame(webContents, "http://127.0.0.1:19090/chat"),
  };

  const result = await handlers.get("lantai:open-with")(
    event,
    "/tmp/project/index.html",
  );

  assert.equal(result.success, false);
  assert.equal(result.revealed, true);
  assert.equal(result.error_code, "open_with_requires_finder");
  assert.equal(revealed, "/tmp/project/index.html");
  assert.equal(spawned, false);
});

test("desktop IPC returns the local app version without updater state", async () => {
  const { handlers, trustedEvent, externalFrameEvent } = makeHarness();

  const result = await handlers.get("lantai:app-version")(trustedEvent);
  const rejected = await handlers.get("lantai:app-version")(externalFrameEvent);

  assert.deepEqual(result, { ok: true, version: "2.0.25" });
  assert.deepEqual(rejected, {
    ok: false,
    error: "untrusted_ipc_sender",
  });
});

test("desktop IPC stages an explicitly selected matching workspace", async () => {
  const root = fs.mkdtempSync(
    path.join(os.tmpdir(), "lantai-workspace-restore-"),
  );
  const source = path.join(root, "external");
  const userData = path.join(root, "user-data");
  fs.mkdirSync(source, { recursive: true });
  fs.writeFileSync(
    path.join(source, "agent.json"),
    JSON.stringify({ id: "default" }),
  );
  fs.writeFileSync(path.join(source, "memory.txt"), "preserved");

  const handlers = new Map();
  const webContents = rendererContents(8);
  installDesktopIpcHandlers({
    app: { getPath: () => userData },
    ipcMain: { handle: (channel, handler) => handlers.set(channel, handler) },
    dialog: {
      showOpenDialog: async () => ({ canceled: false, filePaths: [source] }),
    },
    shell: { openPath: async () => "" },
    getMainWindow: () => ({ isDestroyed: () => false, webContents }),
    getAllowedOrigins: () => ["http://127.0.0.1:19090"],
    randomId: () => "fixed",
  });
  const event = {
    sender: webContents,
    senderFrame: currentFrame(webContents, "http://127.0.0.1:19090/chat"),
  };

  const result = await handlers.get("lantai:restore-workspace")(event, {
    agentId: "default",
    expectedPath: source,
  });

  assert.equal(result.ok, true);
  assert.equal(result.sourcePath, source);
  assert.equal(
    fs.readFileSync(path.join(result.stagedPath, "memory.txt"), "utf8"),
    "preserved",
  );
});

test("MAS workspace staging holds and releases the picker security scope", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "lantai-mas-restore-"));
  const source = path.join(root, "external");
  fs.mkdirSync(source, { recursive: true });
  fs.writeFileSync(
    path.join(source, "agent.json"),
    JSON.stringify({ id: "default" }),
  );
  let released = false;
  const handlers = new Map();
  const webContents = rendererContents(9);
  installDesktopIpcHandlers({
    app: {
      getPath: () => path.join(root, "user-data"),
      startAccessingSecurityScopedResource: (bookmark) => {
        assert.equal(bookmark, "bookmark");
        return () => {
          released = true;
        };
      },
    },
    ipcMain: { handle: (channel, handler) => handlers.set(channel, handler) },
    dialog: {
      showOpenDialog: async (_window, options) => {
        assert.equal(options.securityScopedBookmarks, true);
        return {
          canceled: false,
          filePaths: [source],
          bookmarks: ["bookmark"],
        };
      },
    },
    shell: { openPath: async () => "" },
    getMainWindow: () => ({ isDestroyed: () => false, webContents }),
    getAllowedOrigins: () => ["http://127.0.0.1:19090"],
    distributionChannel: "mas",
  });
  const event = {
    sender: webContents,
    senderFrame: currentFrame(webContents, "http://127.0.0.1:19090/chat"),
  };

  const result = await handlers.get("lantai:restore-workspace")(event, {
    agentId: "default",
    expectedPath: source,
  });

  assert.equal(result.ok, true);
  assert.equal(released, true);
});
