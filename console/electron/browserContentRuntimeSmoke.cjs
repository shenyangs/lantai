const assert = require("node:assert/strict");
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

function normalizeBaseUrl(value) {
  return String(value || "").replace(/\/$/, "");
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function rendererSnapshot(win) {
  try {
    return await win.webContents.executeJavaScript(
      `(() => ({
        href: location.href,
        readyState: document.readyState,
        body: (document.body?.innerText || "").replace(/\\s+/g, " ").slice(0, 1600),
      }))()`,
      true,
    );
  } catch (error) {
    return { snapshotError: String(error) };
  }
}

async function waitFor(win, predicateSource, timeoutMs, label) {
  const deadline = Date.now() + timeoutMs;
  let latest;
  while (Date.now() < deadline) {
    latest = await win.webContents.executeJavaScript(predicateSource, true);
    if (latest) return latest;
    await sleep(100);
  }
  throw new Error(
    `Timed out waiting for ${label}: ${JSON.stringify(latest)}; renderer=${JSON.stringify(await rendererSnapshot(win))}`,
  );
}

async function waitForPath(win, pathname, timeoutMs) {
  return waitFor(
    win,
    `(() => window.location.hash.split("?")[0] === ${JSON.stringify(`#${pathname}`)})()`,
    timeoutMs,
    `path ${pathname}`,
  );
}

function createWindow() {
  return new BrowserWindow({
    width: 1200,
    height: 840,
    show: false,
    title: "Browser Content Runtime Smoke",
    webPreferences: {
      sandbox: false,
      contextIsolation: true,
      nodeIntegration: false,
    },
  });
}

async function installExportRecorder(win, encodedRunId) {
  await win.webContents.executeJavaScript(
    `
      (() => {
        const encodedRunId = ${JSON.stringify(encodedRunId)};
        window.__lantaiBrowserContentSmokeExports = [];
        const originalFetch = window.fetch.bind(window);
        window.fetch = async (...args) => {
          const response = await originalFetch(...args);
          try {
            const request = args[0];
            const rawUrl = typeof request === "string"
              ? request
              : request && typeof request.url === "string"
                ? request.url
                : "";
            const url = new URL(rawUrl, window.location.href);
            if (url.pathname.includes(\`/api/runtime/runs/\${encodedRunId}/export\`)) {
              let payload = null;
              try { payload = await response.clone().json(); } catch { payload = null; }
              window.__lantaiBrowserContentSmokeExports.push({
                status: response.status,
                url: url.pathname,
                payload,
              });
            }
          } catch {
            // Status capture is diagnostic and must not perturb app behavior.
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

async function exportRendererRun(win, runId, run) {
  return win.webContents.executeJavaScript(
    `
      (async () => {
        const runId = ${JSON.stringify(runId)};
        const run = ${JSON.stringify(run)};
        const response = await fetch(\`/api/runtime/runs/\${encodeURIComponent(runId)}/export\`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            expected_attempt_id: run.attempt_id ?? null,
            expected_revision: run.revision ?? null,
            chat_id: run.chat_id ?? null,
            session_id: run.session_id ?? null,
            workspace_dir: run.workspace_dir ?? null,
            approved: true,
            reason: "packaged browser content renderer smoke",
            exported_by: "packaged-smoke",
            version_info: { client: "packaged-smoke" },
          }),
        });
        let payload = null;
        try { payload = await response.json(); } catch { payload = null; }
        return { status: response.status, payload };
      })()
    `,
    true,
  );
}

async function main() {
  const args = parseArgs(process.argv);
  const baseUrl = normalizeBaseUrl(args["base-url"]);
  const desktopToken = String(args["desktop-token"] || "");
  const runId = String(args["run-id"] || "");
  const expectedContractId = String(
    args["expected-contract-id"] || "loop-browser-content-draft-preview-contract",
  );
  const timeoutMs = Number(args["timeout-ms"] || 30000);
  if (!baseUrl || !desktopToken || !runId) {
    throw new Error("--base-url, --desktop-token and --run-id are required");
  }

  const origin = new URL(baseUrl).origin;
  const encodedRunId = encodeURIComponent(runId);
  const exportStatuses = [];
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
    { urls: [`${origin}/api/runtime/runs/*/export`] },
    (details) => {
      if (details.url.includes(`/api/runtime/runs/${encodedRunId}/export`)) {
        exportStatuses.push(details.statusCode);
      }
    },
  );

  const win = createWindow();
  const rendererErrors = [];
  win.webContents.on("console-message", (event, ...legacy) => {
    const details = normalizeConsoleMessage(event, ...legacy);
    if (details.level >= 2) rendererErrors.push(details);
  });
  win.webContents.on("render-process-gone", (_event, details) => {
    rendererErrors.push({ type: "render-process-gone", details });
  });

  const retiredTaskCenterUrl = `${baseUrl}/#/task-center?run=${encodedRunId}`;
  try {
    await win.loadURL(retiredTaskCenterUrl);
    await waitForPath(win, "/chat", timeoutMs);
    await installExportRecorder(win, encodedRunId);

    const rendererRun = await readRendererRun(win, runId);
    assert.ok(rendererRun, `Renderer could not find Browser content run ${runId}`);
    assert.equal(rendererRun.source, "capability", `Unexpected run source: ${JSON.stringify(rendererRun)}`);
    assert.equal(rendererRun.status, "completed", `Unexpected run status: ${JSON.stringify(rendererRun)}`);
    assert.equal(rendererRun.authority_status, "succeeded", `Unexpected authority status: ${JSON.stringify(rendererRun)}`);
    assert.equal(rendererRun.pending_approval, false, `Unexpected pending approval flag: ${JSON.stringify(rendererRun)}`);

    const exportResult = await exportRendererRun(win, runId, rendererRun);
    assert.equal(exportResult.status, 200, `Renderer export failed: ${JSON.stringify(exportResult)}`);
    assert.equal(exportResult.payload?.exported, true, `Renderer export payload failed: ${JSON.stringify(exportResult)}`);
    const contractId = exportResult.payload?.package?.contract?.contract_id;
    assert.equal(contractId, expectedContractId, `Unexpected Browser content export contract: ${JSON.stringify(exportResult)}`);

    const exportRecord = await waitFor(
      win,
      `(() => (window.__lantaiBrowserContentSmokeExports || [])
        .find((item) => item.status === 200 && item.payload?.exported === true) || null)()`,
      timeoutMs,
      "renderer Browser content export response",
    );

    console.log(JSON.stringify({
      ok: true,
      run_id: runId,
      retired_task_center_url: retiredTaskCenterUrl,
      redirect_path: "/chat",
      renderer_run_status: rendererRun.status,
      renderer_authority_status: rendererRun.authority_status,
      renderer_pending_approval: rendererRun.pending_approval,
      export_statuses: exportStatuses,
      captured_export_status: exportRecord.status,
      captured_contract_id: contractId,
      electron: process.versions.electron,
      chrome: process.versions.chrome,
    }, null, 2));
  } catch (error) {
    if (rendererErrors.length > 0) {
      console.error(`Renderer diagnostics: ${JSON.stringify(rendererErrors.slice(-24), null, 2)}`);
    }
    throw error;
  } finally {
    if (!win.isDestroyed()) win.close();
    app.quit();
  }
}

app.whenReady().then(() => {
  main().catch((error) => {
    console.error(error);
    app.exit(1);
  });
});
