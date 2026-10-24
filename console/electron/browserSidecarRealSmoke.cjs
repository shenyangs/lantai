const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const {
  app,
  BrowserView,
  BrowserWindow,
  ipcMain,
  session,
} = require("electron");
const {
  createBrowserWorkbenchController,
  getProfilePartition,
} = require("./browserWorkbenchController.cjs");

const delay = (milliseconds) =>
  new Promise((resolve) => setTimeout(resolve, milliseconds));

async function run() {
  const userDataDir = fs.mkdtempSync(
    path.join(os.tmpdir(), "lantai-browser-sidecar-smoke-"),
  );
  app.setPath("userData", userDataDir);
  await app.whenReady();

  const browserSession = session.fromPartition(getProfilePartition("default"));
  const page = `<!doctype html><html><body style="margin:0;background:#fff;font-family:-apple-system;color:#202124"><header style="height:64px;padding:0 32px;display:flex;align-items:center;background:#f6f3ee;color:#2c6345;font-size:22px">澜台测试页面</header><main style="padding:38px"><h1>右侧浏览器已连接</h1><button id="proof" style="padding:10px 16px">可交互按钮</button></main></body></html>`;
  await browserSession.protocol.handle("https", () =>
    Promise.resolve(
      new Response(page, {
        headers: { "content-type": "text/html; charset=utf-8" },
      }),
    ),
  );

  const mainWindow = new BrowserWindow({
    width: 1200,
    height: 760,
    show: false,
    webPreferences: { contextIsolation: true, nodeIntegration: false },
  });
  await mainWindow.loadURL(
    `data:text/html;charset=utf-8,${encodeURIComponent(
      '<body style="margin:0;background:#faf9f7"><aside style="width:500px;height:100vh;border-right:1px solid #ddd"></aside></body>',
    )}`,
  );

  const controller = createBrowserWorkbenchController({
    app,
    BrowserWindow,
    BrowserView,
    ipcMain,
    session,
    electronDir: __dirname,
    getMainWindow: () => mainWindow,
    lookupHost: async () => [{ address: "93.184.216.34", family: 4 }],
  });
  const opened = await controller.performAction({
    action: "open",
    profile_id: "default",
    url: "https://sidecar.example/page",
    metadata: {
      background_observable: true,
      task_group_id: "sidecar-real-smoke",
    },
  });
  assert.equal(opened.ok, true);

  const bounds = { x: 520, y: 70, width: 640, height: 620 };
  const mounted = await controller.mountSidecar({
    run_id: "sidecar-real-smoke",
    profile_id: "default",
    bounds,
  });
  assert.equal(mounted.surface.mode, "sidecar_preview");
  assert.equal(mainWindow.getBrowserView(), null);

  await delay(250);
  const preview = await controller.captureSidecarPreview();
  assert.equal(preview.ok, true);
  assert.match(preview.data_url, /^data:image\/png;base64,/);

  const takeover = await controller.setSidecarControl("user");
  assert.equal(takeover.surface.mode, "sidecar_interactive");
  const attached = mainWindow.getBrowserView();
  assert.ok(attached);
  assert.deepEqual(attached.getBounds(), bounds);
  assert.equal(
    await attached.webContents.executeJavaScript(
      'document.querySelector("#proof")?.textContent',
    ),
    "可交互按钮",
  );
  assert.equal(
    (await controller.captureSidecarPreview()).error_code,
    "browser_sidecar_user_control_active",
  );

  const handedBack = await controller.setSidecarControl("agent");
  assert.equal(handedBack.surface.mode, "sidecar_preview");
  assert.equal(mainWindow.getBrowserView(), null);
  const hidden = controller.unmountSidecar();
  assert.equal(hidden.surface.mode, "hidden");
  assert.equal(hidden.tabs.length, 1);

  controller.stopBridgeServer();
  await browserSession.protocol.unhandle("https");
  mainWindow.destroy();
  fs.rmSync(userDataDir, { recursive: true, force: true });
  return {
    ok: true,
    preview: true,
    interactive: true,
    privacy_pause: true,
    handback: true,
    tabs_preserved_after_hide: true,
  };
}

run()
  .then((result) => {
    process.stdout.write(`${JSON.stringify(result)}\n`);
    app.quit();
  })
  .catch((error) => {
    process.stderr.write(`${error.stack || error.message}\n`);
    app.exit(1);
  });
