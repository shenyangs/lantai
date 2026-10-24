const assert = require("node:assert/strict");
const crypto = require("node:crypto");
const http = require("node:http");
const path = require("node:path");
const {
  app,
  BrowserWindow,
  BrowserView,
  ipcMain,
} = require("electron");
const {
  createBrowserWorkbenchController,
} = require("./browserWorkbenchController.cjs");

const SMOKE_HOST = "lantai-smoke.test";

app.commandLine.appendSwitch(
  "host-resolver-rules",
  `MAP ${SMOKE_HOST} 127.0.0.1`,
);
app.commandLine.appendSwitch("no-proxy-server");

function listen(server) {
  return new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", () => {
      const address = server.address();
      resolve(`http://${SMOKE_HOST}:${address.port}`);
    });
  });
}

function serveSmokePage() {
  const server = http.createServer((req, res) => {
    const url = new URL(req.url || "/", "http://127.0.0.1");
    if (url.pathname === "/editor") {
      res.writeHead(200, {
        "Content-Type": "text/html; charset=utf-8",
        "Cache-Control": "no-store",
      });
      res.end(`<!doctype html>
<html>
  <head><title>Workbench Smoke Editor</title></head>
  <body>
    <input id="title" placeholder="标题" />
    <textarea id="summary" placeholder="摘要"></textarea>
    <div id="editor" contenteditable="true" role="textbox"></div>
    <button class="save-draft" onclick="window.__saved = true; document.querySelector('#save-status').innerText = 'saved:' + document.querySelector('#title').value + '|' + document.querySelector('#summary').value + '|' + document.querySelector('#editor').innerText;">保存草稿</button>
    <div id="save-status">saved:false</div>
    <script>window.__saved = false;</script>
  </body>
</html>`);
      return;
    }
    if (url.pathname === "/profile") {
      const profileValue = url.searchParams.get("value") || "";
      res.writeHead(200, {
        "Content-Type": "text/html; charset=utf-8",
        "Cache-Control": "no-store",
      });
      res.end(`<!doctype html>
<html>
  <head><title>Workbench Smoke Profile</title></head>
  <body>
    <h1>Profile State</h1>
    <p>Login and publish documentation links are informational.</p>
    <button id="open-editor" onclick="window.open('/editor', '_blank')">Open editor</button>
    <div id="profile-state"></div>
    <script>
      const value = ${JSON.stringify(profileValue)};
      if (value) {
        localStorage.setItem('lantaiSmokeProfile', value);
        document.cookie = 'lantaiSmokeCookie=' + encodeURIComponent(value) + '; SameSite=Lax';
      }
      document.querySelector('#profile-state').innerText =
        'storage:' + (localStorage.getItem('lantaiSmokeProfile') || '') + '; cookie:' + document.cookie;
    </script>
  </body>
</html>`);
      return;
    }
    if (url.pathname === "/challenge") {
      res.writeHead(200, {
        "Content-Type": "text/html; charset=utf-8",
        "Cache-Control": "no-store",
      });
      res.end(`<!doctype html>
<html>
  <head><title>Workbench Login Challenge</title></head>
  <body>
    <h1>Sign in</h1>
    <input name="account" autocomplete="username" />
    <input type="password" name="password" autocomplete="current-password" />
    <button>Continue</button>
  </body>
</html>`);
      return;
    }
    res.writeHead(200, {
      "Content-Type": "text/html; charset=utf-8",
      "Cache-Control": "no-store",
    });
    res.end(`<!doctype html>
<html>
  <head><title>Workbench Smoke Page</title></head>
  <body>
    <main>
      <h1>Browser Workbench Smoke</h1>
      <article>真实 Electron BrowserView 页面，用于验证 Profile 隔离和 Bridge 动作。</article>
      <a href="/editor">Editor</a>
    </main>
  </body>
</html>`);
  });
  return { server, baseUrlPromise: listen(server) };
}

async function postJson(baseUrl, token, pathName, payload = {}) {
  const requestBody = JSON.stringify(payload);
  const response = await fetch(`${baseUrl}${pathName}`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "X-Lantai-Browser-Bridge-Capability": signBridgeActionCapability(
        token,
        requestBody,
      ),
    },
    body: requestBody,
  });
  const body = await response.json();
  return { response, body };
}

async function getJson(baseUrl, token, pathName) {
  const response = await fetch(`${baseUrl}${pathName}`, {
    headers: token
      ? { "X-Lantai-Browser-Bridge-Token": token }
      : {},
  });
  const body = await response.json();
  return { response, body };
}

function signBridgeActionCapability(rootToken, body) {
  const expiresAtMs = Date.now() + 10000;
  const nonce = crypto.randomUUID
    ? crypto.randomUUID()
    : crypto.randomBytes(16).toString("hex");
  const bodyHash = crypto.createHash("sha256").update(body).digest("hex");
  const message = ["v1", String(expiresAtMs), nonce, bodyHash].join("\n");
  const signature = crypto
    .createHmac("sha256", rootToken)
    .update(message)
    .digest("hex");
  return ["v1", String(expiresAtMs), nonce, bodyHash, signature].join(":");
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function waitForSnapshotText(
  controller,
  pageId,
  pattern,
  timeoutMs = 5000,
) {
  const deadline = Date.now() + timeoutMs;
  let latestText = "";
  while (Date.now() < deadline) {
    const snapshot = await controller.performAction({
      action: "snapshot",
      page_id: pageId,
    });
    latestText = String(snapshot?.data?.text || "");
    if (pattern.test(latestText)) return snapshot;
    await sleep(100);
  }
  assert.match(latestText, pattern);
}

async function main() {
  const { server, baseUrlPromise } = serveSmokePage();
  const siteBaseUrl = await baseUrlPromise;
  const controller = createBrowserWorkbenchController({
    app,
    BrowserWindow,
    BrowserView,
    ipcMain,
    electronDir: __dirname,
    randomToken: () => "browser-workbench-smoke-token",
    lookupHost: async (hostname) => {
      if (hostname === SMOKE_HOST) {
        return [{ address: "93.184.216.34", family: 4 }];
      }
      return [{ address: "93.184.216.34", family: 4 }];
    },
  });
  controller.installIpcHandlers();
  const bridgeBaseUrl = await controller.startBridgeServer();
  const token = controller.getBridgeToken();

  try {
    const missingToken = await getJson(bridgeBaseUrl, "", "/state");
    assert.equal(missingToken.response.status, 401);
    assert.equal(missingToken.body.error_code, "electron_bridge_unauthorized");

    const wrongToken = await getJson(bridgeBaseUrl, "wrong-token", "/state");
    assert.equal(wrongToken.response.status, 401);
    assert.equal(wrongToken.body.error_code, "electron_bridge_unauthorized");

    const opened = await controller.performAction({
      action: "open_workbench",
      profile_id: "smoke-alpha",
      url: "about:blank",
    });
    assert.equal(opened.ok, true);

    const alphaTab = await controller.performAction({
      action: "new_tab",
      profile_id: "smoke-alpha",
      url: "about:blank",
    });
    const betaTab = await controller.performAction({
      action: "new_tab",
      profile_id: "smoke-beta",
      url: "about:blank",
    });
    assert.ok(alphaTab.page_id);
    assert.ok(betaTab.page_id);
    assert.notEqual(alphaTab.page_id, betaTab.page_id);

    await controller.performAction({
      action: "navigate",
      page_id: alphaTab.page_id,
      url: `${siteBaseUrl}/profile?value=alpha`,
    });
    await controller.performAction({
      action: "navigate",
      page_id: betaTab.page_id,
      url: `${siteBaseUrl}/profile?value=beta`,
    });

    await controller.performAction({
      action: "navigate",
      page_id: alphaTab.page_id,
      url: `${siteBaseUrl}/profile`,
    });
    await controller.performAction({
      action: "navigate",
      page_id: betaTab.page_id,
      url: `${siteBaseUrl}/profile`,
    });

    const alphaReadAgain = await waitForSnapshotText(
      controller,
      alphaTab.page_id,
      /storage:alpha/,
    );
    const betaReadAgain = await waitForSnapshotText(
      controller,
      betaTab.page_id,
      /storage:beta/,
    );
    assert.match(alphaReadAgain.data.text, /storage:alpha/);
    assert.match(alphaReadAgain.data.text, /lantaiSmokeCookie=alpha/);
    assert.equal(alphaReadAgain.data.page_context.kind, "normal");
    assert.equal(alphaReadAgain.data.page_context.requires_manual_takeover, false);
    assert.match(betaReadAgain.data.text, /storage:beta/);
    assert.match(betaReadAgain.data.text, /lantaiSmokeCookie=beta/);

    await controller.performAction({
      action: "navigate",
      page_id: betaTab.page_id,
      url: `${siteBaseUrl}/challenge`,
    });
    const challenge = await waitForSnapshotText(
      controller,
      betaTab.page_id,
      /Sign in/,
    );
    assert.equal(challenge.data.page_context.kind, "login");
    assert.equal(challenge.data.page_context.requires_manual_takeover, true);

    const beforePopup = await controller.performAction({ action: "state" });
    await controller.performAction({
      action: "click",
      page_id: alphaTab.page_id,
      selector: "#open-editor",
    });
    await sleep(700);
    const afterPopup = await controller.performAction({ action: "state" });
    await controller.performAction({
      action: "click",
      page_id: alphaTab.page_id,
      selector: "#open-editor",
    });
    await sleep(700);
    const afterDuplicatePopup = await controller.performAction({ action: "state" });
    assert.equal(afterPopup.tabs.length, beforePopup.tabs.length + 1);
    assert.equal(afterDuplicatePopup.tabs.length, afterPopup.tabs.length);
    assert.equal(afterPopup.tabs.at(-1).profile_id, "smoke-alpha");

    const editorTab = await controller.performAction({
      action: "new_tab",
      profile_id: "smoke-alpha",
      url: "about:blank",
    });
    await controller.performAction({
      action: "navigate",
      page_id: editorTab.page_id,
      url: `${siteBaseUrl}/editor`,
    });
    const filled = await controller.performAction({
      action: "fill_form",
      page_id: editorTab.page_id,
      fields: {
        "#title": "公众号草稿 smoke",
        "#summary": "等价富文本保存验证",
        "#editor": "正文第一段\n正文第二段",
      },
    });
    assert.equal(filled.ok, true);
    assert.equal(filled.data.details[2].mode, "contenteditable_insert_text");

    const saved = await controller.performAction({
      action: "click",
      page_id: editorTab.page_id,
      selector: "button.save-draft",
    });
    assert.equal(saved.ok, true);
    const saveState = await waitForSnapshotText(
      controller,
      editorTab.page_id,
      /saved:公众号草稿 smoke/,
    );
    assert.match(saveState.data.text, /saved:公众号草稿 smoke/);
    assert.match(saveState.data.text, /等价富文本保存验证/);
    assert.match(saveState.data.text, /正文第二段/);

    const activeBeforeBackground = await controller.performAction({ action: "state" });
    const backgroundOpen = await controller.performAction({
      action: "open",
      profile_id: "smoke-alpha",
      url: `${siteBaseUrl}/?background=1`,
      metadata: {
        background_observable: true,
        task_group_id: "smoke-background-task",
      },
    });
    const backgroundTab = backgroundOpen.tabs.find(
      (tab) => tab.task_group_id === "smoke-background-task",
    );
    assert.ok(backgroundTab);
    assert.equal(backgroundTab.background, true);
    assert.equal(backgroundOpen.active_tab_id, activeBeforeBackground.active_tab_id);

    const backgroundSnapshot = await controller.performAction({
      action: "snapshot",
      profile_id: "smoke-alpha",
      metadata: {
        background_observable: true,
        task_group_id: "smoke-background-task",
      },
    });
    assert.equal(backgroundSnapshot.page_id, backgroundTab.id);

    const activeAfterBackground = await controller.performAction({ action: "state" });
    assert.equal(activeAfterBackground.active_tab_id, activeBeforeBackground.active_tab_id);

    const screenshot = await postJson(bridgeBaseUrl, token, "/action", {
      action: "screenshot",
      page_id: editorTab.page_id,
    });
    assert.equal(screenshot.response.status, 200);
    assert.equal(screenshot.body.ok, true);
    assert.ok(screenshot.body.screenshot_path);

    const state = await getJson(bridgeBaseUrl, token, "/state");
    assert.equal(state.response.status, 200);
    assert.equal(state.body.ok, true);
    assert.ok(state.body.tabs.length >= 3);

    console.log(
      JSON.stringify(
        {
          ok: true,
          bridgeBaseUrl,
          siteBaseUrl,
          electron: process.versions.electron,
          chrome: process.versions.chrome,
          profiles: ["smoke-alpha", "smoke-beta"],
          screenshot_path: screenshot.body.screenshot_path,
        },
        null,
        2,
      ),
    );
  } finally {
    controller.stopBridgeServer();
    server.close();
    for (const window of BrowserWindow.getAllWindows()) {
      if (!window.isDestroyed()) {
        window.close();
      }
    }
    app.quit();
  }
}

app.whenReady().then(() => {
  main().catch((error) => {
    console.error(error);
    app.exit(1);
  });
});
