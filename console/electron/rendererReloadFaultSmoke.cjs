const assert = require("node:assert/strict");
const { app, BrowserWindow, session } = require("electron");

function parseArgs(argv) {
  const args = {};
  for (let index = 2; index < argv.length; index += 1) {
    const value = argv[index];
    if (!value.startsWith("--")) continue;
    const key = value.slice(2);
    const next = argv[index + 1];
    if (!next || next.startsWith("--")) args[key] = "1";
    else {
      args[key] = next;
      index += 1;
    }
  }
  return args;
}

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function waitFor(win, source, timeoutMs, label) {
  const deadline = Date.now() + timeoutMs;
  let latest = null;
  while (Date.now() < deadline) {
    try {
      latest = await win.webContents.executeJavaScript(source, true);
      if (latest) return latest;
    } catch {
      // Reload temporarily destroys the execution context.
    }
    await sleep(100);
  }
  throw new Error(`Timed out waiting for ${label}: ${JSON.stringify(latest)}`);
}

async function main() {
  const args = parseArgs(process.argv);
  const baseUrl = String(args["base-url"] || "").replace(/\/$/, "");
  const desktopToken = String(args["desktop-token"] || "");
  const chatId = String(args["chat-id"] || "");
  const sentinel = String(args.sentinel || "");
  const timeoutMs = Number(args["timeout-ms"] || 60000);
  if (!baseUrl || !desktopToken || !chatId || !sentinel) {
    throw new Error("--base-url, --desktop-token, --chat-id and --sentinel are required");
  }
  const origin = new URL(baseUrl).origin;
  session.defaultSession.webRequest.onBeforeSendHeaders(
    { urls: [`${origin}/*`] },
    (details, callback) => {
      const url = new URL(details.url);
      if (url.pathname.startsWith("/api/")) {
        details.requestHeaders["X-Lantai-Desktop-Token"] = desktopToken;
      }
      callback({ requestHeaders: details.requestHeaders });
    },
  );
  const win = new BrowserWindow({
    width: 1000,
    height: 760,
    show: false,
    webPreferences: { sandbox: false, contextIsolation: true, nodeIntegration: false },
  });
  let loadCount = 0;
  win.webContents.on("did-finish-load", () => { loadCount += 1; });
  try {
    const url = `${baseUrl}/#/chat/${encodeURIComponent(chatId)}`;
    await win.loadURL(url);
    await waitFor(
      win,
      `document.body && document.body.innerText.includes(${JSON.stringify(sentinel)})`,
      timeoutMs,
      "persisted assistant answer before reload",
    );
    const beforeUrl = win.webContents.getURL();
    win.webContents.reload();
    await waitFor(win, `document.readyState === "complete"`, timeoutMs, "renderer reload");
    await waitFor(
      win,
      `document.body && document.body.innerText.includes(${JSON.stringify(sentinel)})`,
      timeoutMs,
      "persisted assistant answer after reload",
    );
    const counts = await win.webContents.executeJavaScript(
      `(() => ({
        assistant_bubbles: document.querySelectorAll(".lantai-bubble-list-item.lantai-bubble-assistant").length,
        empty_assistants: Array.from(document.querySelectorAll(".lantai-bubble-list-item.lantai-bubble-assistant")).filter((node) => !(node.textContent || "").trim()).length,
      }))()`,
      true,
    );
    assert.equal(counts.assistant_bubbles, 1);
    assert.equal(counts.empty_assistants, 0);
    console.log(JSON.stringify({
      ok: true,
      chat_id: chatId,
      observed_failure: "renderer_execution_context_destroyed_by_reload",
      recovery_state: "same_chat_answer_restored_once",
      load_count: loadCount,
      before_url: beforeUrl,
      after_url: win.webContents.getURL(),
      ...counts,
    }, null, 2));
  } finally {
    if (!win.isDestroyed()) win.close();
    app.quit();
  }
}

app.whenReady().then(() => main().catch((error) => {
  console.error(error);
  app.exit(1);
}));
