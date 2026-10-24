const assert = require("node:assert/strict");
const path = require("node:path");
const { app, BrowserWindow, session } = require("electron");
const { normalizeConsoleMessage } = require("./consoleMessageDetails.cjs");

function parseArgs(argv) {
  const args = {};
  for (let index = 2; index < argv.length; index += 1) {
    const value = argv[index];
    if (!value.startsWith("--")) continue;
    const key = value.slice(2);
    const next = argv[index + 1];
    if (!next || next.startsWith("--")) {
      args[key] = "1";
      continue;
    }
    args[key] = next;
    index += 1;
  }
  return args;
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function normalizeBaseUrl(value) {
  return String(value || "").replace(/\/$/, "");
}

function createWindow(title) {
  const win = new BrowserWindow({
    width: 1200,
    height: 840,
    show: false,
    title,
    webPreferences: {
      sandbox: false,
      contextIsolation: true,
      nodeIntegration: false,
      preload: path.join(__dirname, "taskCenterMultiWindowSmokePreload.cjs"),
    },
  });
  win.webContents.on("console-message", (event, ...legacy) => {
    const details = normalizeConsoleMessage(event, ...legacy);
    if (details.level >= 2) {
      console.error(`[${title}] renderer console level=${details.level}: ${details.message}`);
    }
  });
  win.webContents.on("render-process-gone", (_event, details) => {
    console.error(`[${title}] render process gone: ${JSON.stringify(details)}`);
  });
  return win;
}

async function rendererSnapshot(win) {
  try {
    return await win.webContents.executeJavaScript(
      `(() => ({
        href: location.href,
        readyState: document.readyState,
        body: (document.body?.innerText || "").replace(/\\s+/g, " ").slice(0, 1200),
      }))()`,
      true,
    );
  } catch (error) {
    return { snapshotError: String(error) };
  }
}

async function waitForPath(win, pathname, timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const matched = await win.webContents.executeJavaScript(
      `(() => window.location.hash.split("?")[0] === ${JSON.stringify(`#${pathname}`)})()`,
      true,
    );
    if (matched) return;
    await sleep(100);
  }
  throw new Error(
    `Timed out waiting for path ${pathname}: ${JSON.stringify(await rendererSnapshot(win))}`,
  );
}

async function installFetchRecorder(win, encodedRunId) {
  await win.webContents.executeJavaScript(
    `
      (() => {
        const encodedRunId = ${JSON.stringify(encodedRunId)};
        window.__lantaiRuntimeSmokeStatuses = [];
        const originalFetch = window.fetch.bind(window);
        window.fetch = async (...args) => {
          const response = await originalFetch(...args);
          try {
            const request = args[0];
            const rawUrl = typeof request === "string"
              ? request
              : request && typeof request.url === "string" ? request.url : "";
            const url = new URL(rawUrl, window.location.href);
            const marker = \`/api/runtime/runs/\${encodedRunId}/\`;
            if (url.pathname.includes(marker)) {
              window.__lantaiRuntimeSmokeStatuses.push({
                action: url.pathname.slice(url.pathname.lastIndexOf("/") + 1),
                status: response.status,
              });
            }
          } catch {
            // Diagnostic only.
          }
          return response;
        };
        return true;
      })()
    `,
    true,
  );
}

async function readRendererRun(win, runId) {
  return win.webContents.executeJavaScript(
    `
      (async () => {
        const runId = ${JSON.stringify(runId)};
        const response = await fetch("/api/runtime/runs?limit=64");
        if (!response.ok) throw new Error(\`runtime runs fetch failed: \${response.status}\`);
        const payload = await response.json();
        const items = Array.isArray(payload.items) ? payload.items : [];
        return items.find((item) => item && item.id === runId) || null;
      })()
    `,
    true,
  );
}

async function postRendererAction(win, runId, action, body) {
  return win.webContents.executeJavaScript(
    `
      (async () => {
        const runId = ${JSON.stringify(runId)};
        const action = ${JSON.stringify(action)};
        const body = ${JSON.stringify(body)};
        const response = await fetch(
          \`/api/runtime/runs/\${encodeURIComponent(runId)}/\${action}\`,
          {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(body),
          },
        );
        let payload = null;
        try { payload = await response.json(); } catch { payload = null; }
        return { status: response.status, payload };
      })()
    `,
    true,
  );
}

function controlFence(run) {
  return {
    expected_attempt_id: run.attempt_id ?? null,
    expected_revision: run.revision ?? null,
    chat_id: run.chat_id ?? null,
    session_id: run.session_id ?? null,
    workspace_dir: run.workspace_dir ?? null,
  };
}

async function main() {
  const args = parseArgs(process.argv);
  const baseUrl = normalizeBaseUrl(args["base-url"]);
  const desktopToken = String(args["desktop-token"] || "");
  const runId = String(args["run-id"] || "");
  const timeoutMs = Number(args["timeout-ms"] || 30000);
  if (!baseUrl || !desktopToken || !runId) {
    throw new Error("--base-url, --desktop-token and --run-id are required");
  }

  process.env.LANTAI_TASK_CENTER_SMOKE_API_BASE_URL = baseUrl;
  const origin = new URL(baseUrl).origin;
  const encodedRunId = encodeURIComponent(runId);
  const webRequestPauseStatuses = [];
  const webRequestBudgetStatuses = [];
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
  session.defaultSession.webRequest.onCompleted(
    { urls: [`${origin}/api/runtime/runs/*/pause`] },
    (details) => {
      if (details.url.includes(`/api/runtime/runs/${encodedRunId}/pause`)) {
        webRequestPauseStatuses.push(details.statusCode);
      }
    },
  );
  session.defaultSession.webRequest.onCompleted(
    { urls: [`${origin}/api/runtime/runs/*/budget`] },
    (details) => {
      if (details.url.includes(`/api/runtime/runs/${encodedRunId}/budget`)) {
        webRequestBudgetStatuses.push(details.statusCode);
      }
    },
  );

  const first = createWindow("Runtime Concurrency Smoke A");
  const second = createWindow("Runtime Concurrency Smoke B");
  const retiredTaskCenterUrl = `${baseUrl}/#/task-center?run=${encodedRunId}`;
  try {
    await Promise.all([first.loadURL(retiredTaskCenterUrl), second.loadURL(retiredTaskCenterUrl)]);
    await Promise.all([waitForPath(first, "/chat", timeoutMs), waitForPath(second, "/chat", timeoutMs)]);
    await Promise.all([installFetchRecorder(first, encodedRunId), installFetchRecorder(second, encodedRunId)]);

    const initialRun = await readRendererRun(first, runId);
    assert.ok(initialRun, `First renderer could not find runtime run ${runId}`);
    const budgetResult = await postRendererAction(first, runId, "budget", {
      ...controlFence(initialRun),
      budget: { ...(initialRun.budget || {}), max_steps: 18, max_seconds: 1200 },
      changed_by: "user",
      reason: "packaged renderer concurrency smoke",
    });
    assert.equal(budgetResult.status, 200, `Renderer budget update failed: ${JSON.stringify(budgetResult)}`);

    const currentRun = await readRendererRun(first, runId);
    assert.ok(currentRun, `Renderer could not refresh runtime run ${runId}`);
    const pauseBody = controlFence(currentRun);
    const pauseResults = await Promise.all([
      postRendererAction(first, runId, "pause", pauseBody),
      postRendererAction(second, runId, "pause", pauseBody),
    ]);
    const pauseStatuses = pauseResults.map((item) => item.status).sort((a, b) => a - b);
    assert.deepEqual(pauseStatuses, [200, 409], `Expected fresh/stale pause responses: ${JSON.stringify(pauseResults)}`);

    const [firstFinal, secondFinal] = await Promise.all([
      readRendererRun(first, runId),
      readRendererRun(second, runId),
    ]);
    assert.equal(firstFinal?.authority_status, "paused", `First renderer did not refresh paused state: ${JSON.stringify(firstFinal)}`);
    assert.equal(secondFinal?.authority_status, "paused", `Second renderer did not refresh paused state: ${JSON.stringify(secondFinal)}`);

    console.log(JSON.stringify({
      ok: true,
      run_id: runId,
      retired_task_center_url: retiredTaskCenterUrl,
      redirect_path: "/chat",
      budget_statuses: [budgetResult.status],
      pause_statuses: pauseStatuses,
      web_request_budget_statuses: webRequestBudgetStatuses,
      web_request_pause_statuses: webRequestPauseStatuses,
      electron: process.versions.electron,
      chrome: process.versions.chrome,
    }, null, 2));
  } finally {
    for (const win of [first, second]) {
      if (!win.isDestroyed()) win.close();
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
