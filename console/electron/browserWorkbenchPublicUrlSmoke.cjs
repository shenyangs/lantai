const assert = require("node:assert/strict");
const dns = require("node:dns").promises;
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const {
  app,
  BrowserWindow,
  BrowserView,
  ipcMain,
  session,
} = require("electron");
const {
  createBrowserWorkbenchController,
  getProfilePartition,
} = require("./browserWorkbenchController.cjs");

function readArg(name, fallback = "") {
  const prefix = `--${name}=`;
  const item = process.argv.find((value) => value.startsWith(prefix));
  return item ? item.slice(prefix.length) : fallback;
}

function writeJson(target, value) {
  fs.mkdirSync(path.dirname(target), { recursive: true });
  const temporary = `${target}.tmp`;
  fs.writeFileSync(temporary, `${JSON.stringify(value, null, 2)}\n`);
  fs.renameSync(temporary, target);
}

async function run() {
  const targetUrl = readArg("url", "https://example.com/");
  const outputPath = path.resolve(
    readArg("output", path.join(process.cwd(), "browser-workbench-public-url-smoke.json")),
  );
  const screenshotPath = path.resolve(
    readArg("screenshot", path.join(process.cwd(), "browser-workbench-public-url-smoke.png")),
  );
  const profileId = readArg("profile-id", "public-url-smoke");
  const parsedUrl = new URL(targetUrl);
  assert.equal(parsedUrl.protocol, "https:", "public smoke URL must use HTTPS");

  const userDataDir = fs.mkdtempSync(path.join(os.tmpdir(), "lantai-browser-public-smoke-"));
  app.setPath("userData", userDataDir);
  await app.whenReady();

  const dnsStartedAt = Date.now();
  const dnsAddresses = await dns.lookup(parsedUrl.hostname, {
    all: true,
    verbatim: true,
  });
  const dnsElapsedMs = Date.now() - dnsStartedAt;
  assert.ok(dnsAddresses.length > 0, "system DNS returned no addresses");

  const profileSession = session.fromPartition(getProfilePartition(profileId));
  const proxyStartedAt = Date.now();
  const resolvedProxy = await profileSession.resolveProxy(targetUrl);
  const proxyElapsedMs = Date.now() - proxyStartedAt;
  assert.ok(resolvedProxy, "Electron session returned no proxy decision");

  const controller = createBrowserWorkbenchController({
    app,
    BrowserWindow,
    BrowserView,
    ipcMain,
    session,
    electronDir: __dirname,
  });
  controller.installIpcHandlers();
  const startedAt = new Date().toISOString();
  try {
    const opened = await controller.openWorkbench({
      profile_id: profileId,
      url: targetUrl,
    });
    assert.equal(opened?.ok, true, `open failed: ${JSON.stringify(opened)}`);
    const pageId = opened.active_tab_id || opened.tabs?.at(-1)?.id;
    assert.ok(pageId, "open result did not expose a page id");

    const snapshot = await controller.performAction({
      action: "snapshot",
      profile_id: profileId,
      page_id: pageId,
    });
    assert.equal(snapshot?.ok, true, `snapshot failed: ${JSON.stringify(snapshot)}`);
    assert.equal(new URL(snapshot.url).hostname, parsedUrl.hostname);
    assert.match(snapshot.data?.text || "", /Example Domain/i);

    const screenshot = await controller.performAction({
      action: "screenshot",
      profile_id: profileId,
      page_id: pageId,
    });
    assert.equal(screenshot?.ok, true, `screenshot failed: ${JSON.stringify(screenshot)}`);
    fs.mkdirSync(path.dirname(screenshotPath), { recursive: true });
    fs.copyFileSync(screenshot.screenshot_path, screenshotPath);

    const payload = {
      schema_version: "com.lantai.browser-workbench-public-url-smoke.v1",
      ok: true,
      started_at: startedAt,
      completed_at: new Date().toISOString(),
      runtime: "source-repo-electron",
      electron: process.versions.electron,
      chrome: process.versions.chrome,
      target_url: targetUrl,
      final_url: snapshot.url,
      final_title: snapshot.data?.title || "",
      visible_text_contains_example_domain: /Example Domain/i.test(snapshot.data?.text || ""),
      system_dns: {
        hostname: parsedUrl.hostname,
        elapsed_ms: dnsElapsedMs,
        addresses: dnsAddresses,
      },
      electron_proxy: {
        elapsed_ms: proxyElapsedMs,
        decision: resolvedProxy,
      },
      page_id: pageId,
      screenshot_path: screenshotPath,
    };
    writeJson(outputPath, payload);
    process.stdout.write(`${JSON.stringify(payload, null, 2)}\n`);
  } finally {
    controller.stopBridgeServer();
    for (const window of BrowserWindow.getAllWindows()) {
      window.destroy();
    }
  }
}

run()
  .then(() => app.exit(0))
  .catch((error) => {
    process.stderr.write(`${error.stack || error}\n`);
    app.exit(1);
  });
