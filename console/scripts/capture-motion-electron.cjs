const fs = require("node:fs");
const path = require("node:path");
const { app, BrowserWindow } = require("electron");

const [baseUrl, outputDir] = process.argv.slice(2);

if (!baseUrl || !outputDir) {
  console.error("Usage: electron capture-motion-electron.cjs <baseUrl> <outputDir>");
  process.exit(1);
}

const scenarios = [
  { name: "motion-matrix-light", theme: "light", motion: "normal" },
  { name: "motion-matrix-dark", theme: "dark", motion: "normal" },
  { name: "motion-matrix-reduced", theme: "light", motion: "reduce" },
];

function delay(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function waitForReady(win) {
  for (let attempt = 0; attempt < 120; attempt += 1) {
    const ready = await win.webContents.executeJavaScript(
      "Boolean(window.__motionMatrixReady)",
      true,
    );
    if (ready) return;
    await delay(100);
  }
  throw new Error("Timed out waiting for motion matrix readiness marker.");
}

async function setReducedMotion(win, enabled) {
  const wc = win.webContents;
  if (!wc.debugger.isAttached()) {
    wc.debugger.attach("1.3");
  }

  await wc.debugger.sendCommand("Emulation.setEmulatedMedia", {
    features: enabled
      ? [{ name: "prefers-reduced-motion", value: "reduce" }]
      : [],
  });
}

async function captureScenario(win, scenario) {
  await setReducedMotion(win, scenario.motion === "reduce");
  const url = `${baseUrl}?theme=${scenario.theme}&motion=${scenario.motion}`;
  await win.loadURL(url);
  await waitForReady(win);
  await delay(scenario.motion === "reduce" ? 120 : 360);
  const image = await win.webContents.capturePage();
  const filePath = path.join(outputDir, `${scenario.name}.png`);
  fs.writeFileSync(filePath, image.toPNG());
  return {
    ...scenario,
    file: filePath,
  };
}

app.whenReady().then(async () => {
  fs.mkdirSync(outputDir, { recursive: true });
  const win = new BrowserWindow({
    width: 1440,
    height: 2400,
    show: false,
    backgroundColor: "#f6f7f9",
    webPreferences: {
      backgroundThrottling: false,
      contextIsolation: true,
      nodeIntegration: false,
    },
  });

  try {
    const captures = [];
    for (const scenario of scenarios) {
      captures.push(await captureScenario(win, scenario));
    }
    fs.writeFileSync(
      path.join(outputDir, "manifest.json"),
      JSON.stringify(
        {
          capturedAt: new Date().toISOString(),
          source: baseUrl,
          viewport: { width: 1440, height: 2400 },
          captures,
        },
        null,
        2,
      ),
    );
  } finally {
    if (win.webContents.debugger.isAttached()) {
      win.webContents.debugger.detach();
    }
    win.close();
    app.quit();
  }
});
