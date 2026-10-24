import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { spawn } from "node:child_process";
import net from "node:net";

const [baseUrl, outputDir] = process.argv.slice(2);

if (!baseUrl || !outputDir) {
  console.error("Usage: node capture-motion-chrome.mjs <baseUrl> <outputDir>");
  process.exit(1);
}

const viewport = { width: 1440, height: 2400 };
const scenarios = [
  { name: "motion-matrix-light", theme: "light", motion: "normal" },
  { name: "motion-matrix-dark", theme: "dark", motion: "normal" },
  { name: "motion-matrix-reduced", theme: "light", motion: "reduce" },
];

function delay(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function stopProcess(child) {
  if (child.exitCode !== null || child.signalCode !== null) {
    return;
  }

  child.kill("SIGTERM");
  await new Promise((resolve) => {
    const timer = setTimeout(() => {
      if (child.exitCode === null && child.signalCode === null) {
        child.kill("SIGKILL");
      }
      resolve();
    }, 1500);
    child.once("exit", () => {
      clearTimeout(timer);
      resolve();
    });
  });
}

function candidateChromePaths() {
  const candidates = [
    process.env.CHROME_PATH,
    process.env.GOOGLE_CHROME_BIN,
  ].filter(Boolean);

  if (process.platform === "darwin") {
    candidates.push(
      "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
      path.join(
        os.homedir(),
        "Applications",
        "Google Chrome.app",
        "Contents",
        "MacOS",
        "Google Chrome",
      ),
      "/Applications/Chromium.app/Contents/MacOS/Chromium",
    );
  }

  if (process.platform === "linux") {
    candidates.push(
      "/usr/bin/google-chrome",
      "/usr/bin/google-chrome-stable",
      "/usr/bin/chromium",
      "/usr/bin/chromium-browser",
    );
  }

  if (process.platform === "win32") {
    candidates.push(
      path.join(
        process.env.PROGRAMFILES || "C:\\Program Files",
        "Google",
        "Chrome",
        "Application",
        "chrome.exe",
      ),
      path.join(
        process.env["PROGRAMFILES(X86)"] || "C:\\Program Files (x86)",
        "Google",
        "Chrome",
        "Application",
        "chrome.exe",
      ),
      path.join(
        process.env.LOCALAPPDATA || "",
        "Google",
        "Chrome",
        "Application",
        "chrome.exe",
      ),
    );
  }

  return candidates;
}

function resolveChromePath() {
  for (const candidate of candidateChromePaths()) {
    if (candidate && fs.existsSync(candidate)) {
      return candidate;
    }
  }

  throw new Error(
    "Could not resolve Chrome binary. Set CHROME_PATH or install Google Chrome.",
  );
}

function getFreePort() {
  return new Promise((resolve, reject) => {
    const server = net.createServer();
    server.unref();
    server.on("error", reject);
    server.listen(0, "127.0.0.1", () => {
      const address = server.address();
      server.close(() => {
        if (address && typeof address === "object") {
          resolve(address.port);
        } else {
          reject(new Error("Could not allocate Chrome debugging port."));
        }
      });
    });
  });
}

function chromeArgs(port, profileDir) {
  return [
    "--headless=new",
    "--disable-background-networking",
    "--disable-component-update",
    "--disable-dev-shm-usage",
    "--disable-extensions",
    "--disable-gpu",
    "--disable-sync",
    "--hide-scrollbars",
    "--no-default-browser-check",
    "--no-first-run",
    `--remote-debugging-port=${port}`,
    `--user-data-dir=${profileDir}`,
    "about:blank",
  ];
}

async function waitForJson(url) {
  for (let attempt = 0; attempt < 80; attempt += 1) {
    try {
      const response = await fetch(url);
      if (response.ok) {
        return await response.json();
      }
    } catch {
      // Chrome is still starting.
    }
    await delay(125);
  }
  throw new Error(`Timed out waiting for Chrome DevTools at ${url}`);
}

function createCdpClient(webSocketUrl) {
  const socket = new WebSocket(webSocketUrl);
  let nextId = 1;
  const pending = new Map();
  const events = new Map();

  socket.addEventListener("message", (event) => {
    const message = JSON.parse(event.data);
    if (message.id && pending.has(message.id)) {
      const { resolve, reject } = pending.get(message.id);
      pending.delete(message.id);
      if (message.error) {
        reject(new Error(message.error.message || JSON.stringify(message.error)));
      } else {
        resolve(message.result || {});
      }
      return;
    }

    if (message.method && events.has(message.method)) {
      for (const listener of events.get(message.method)) {
        listener(message.params || {});
      }
    }
  });

  const opened = new Promise((resolve, reject) => {
    socket.addEventListener("open", resolve, { once: true });
    socket.addEventListener("error", reject, { once: true });
  });

  function send(method, params = {}) {
    const id = nextId;
    nextId += 1;
    const payload = JSON.stringify({ id, method, params });
    return new Promise((resolve, reject) => {
      pending.set(id, { resolve, reject });
      socket.send(payload);
    });
  }

  function on(method, listener) {
    const listeners = events.get(method) || new Set();
    listeners.add(listener);
    events.set(method, listeners);
    return () => {
      listeners.delete(listener);
    };
  }

  function close() {
    socket.close();
  }

  return { opened, send, on, close };
}

async function openTarget(port) {
  const response = await fetch(`http://127.0.0.1:${port}/json/new?about:blank`, {
    method: "PUT",
  });
  if (!response.ok) {
    throw new Error(`Could not create Chrome target: ${response.status}`);
  }
  const target = await response.json();
  if (!target.webSocketDebuggerUrl) {
    throw new Error("Chrome target did not provide a WebSocket URL.");
  }
  return target;
}

async function waitForLoadEvent(client) {
  return new Promise((resolve) => {
    const off = client.on("Page.loadEventFired", () => {
      off();
      resolve();
    });
  });
}

async function waitForReady(client) {
  for (let attempt = 0; attempt < 120; attempt += 1) {
    const result = await client.send("Runtime.evaluate", {
      expression: "Boolean(window.__motionMatrixReady)",
      returnByValue: true,
    });
    if (result.result?.value === true) return;
    await delay(100);
  }
  throw new Error("Timed out waiting for motion matrix readiness marker.");
}

async function captureScenario(port, scenario) {
  const target = await openTarget(port);
  const client = createCdpClient(target.webSocketDebuggerUrl);
  await client.opened;

  try {
    await client.send("Page.enable");
    await client.send("Runtime.enable");
    await client.send("Emulation.setDeviceMetricsOverride", {
      width: viewport.width,
      height: viewport.height,
      deviceScaleFactor: 1,
      mobile: false,
    });
    await client.send("Emulation.setEmulatedMedia", {
      features:
        scenario.motion === "reduce"
          ? [{ name: "prefers-reduced-motion", value: "reduce" }]
          : [],
    });

    const loaded = waitForLoadEvent(client);
    await client.send("Page.navigate", {
      url: `${baseUrl}?theme=${scenario.theme}&motion=${scenario.motion}`,
    });
    await loaded;
    await waitForReady(client);
    await delay(scenario.motion === "reduce" ? 120 : 360);

    const layout = await client.send("Page.getLayoutMetrics");
    const contentSize = layout.cssContentSize || layout.contentSize || {};
    const captureHeight = Math.min(
      Math.max(Math.ceil(contentSize.height || viewport.height), viewport.height),
      6400,
    );
    const screenshot = await client.send("Page.captureScreenshot", {
      format: "png",
      fromSurface: true,
      captureBeyondViewport: true,
      clip: {
        x: 0,
        y: 0,
        width: viewport.width,
        height: captureHeight,
        scale: 1,
      },
    });
    const filePath = path.join(outputDir, `${scenario.name}.png`);
    fs.writeFileSync(filePath, Buffer.from(screenshot.data, "base64"));
    return {
      ...scenario,
      file: filePath,
    };
  } finally {
    client.close();
    if (target.id) {
      await fetch(`http://127.0.0.1:${port}/json/close/${target.id}`).catch(
        () => undefined,
      );
    }
  }
}

async function run() {
  fs.mkdirSync(outputDir, { recursive: true });

  const chromePath = resolveChromePath();
  const debugPort = await getFreePort();
  const profileDir = fs.mkdtempSync(
    path.join(os.tmpdir(), "lantai-motion-chrome-"),
  );
  const chrome = spawn(chromePath, chromeArgs(debugPort, profileDir), {
    stdio: ["ignore", "ignore", "pipe"],
  });
  let chromeStderr = "";
  chrome.stderr?.on("data", (chunk) => {
    chromeStderr += chunk.toString();
  });

  try {
    await waitForJson(`http://127.0.0.1:${debugPort}/json/version`);
    const captures = [];
    for (const scenario of scenarios) {
      captures.push(await captureScenario(debugPort, scenario));
    }
    fs.writeFileSync(
      path.join(outputDir, "manifest.json"),
      JSON.stringify(
        {
          capturedAt: new Date().toISOString(),
          source: baseUrl,
          renderer: "chrome-headless-cdp",
          viewport,
          captures,
        },
        null,
        2,
      ),
    );
  } catch (error) {
    if (chromeStderr.trim()) {
      process.stderr.write(chromeStderr);
    }
    throw error;
  } finally {
    await stopProcess(chrome);
    try {
      fs.rmSync(profileDir, {
        recursive: true,
        force: true,
        maxRetries: 3,
        retryDelay: 100,
      });
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      process.stderr.write(
        `Warning: could not remove temporary Chrome profile ${profileDir}: ${message}\n`,
      );
    }
  }
}

run().catch((error) => {
  console.error(error);
  process.exit(1);
});
