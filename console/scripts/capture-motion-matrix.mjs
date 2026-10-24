import { createRequire } from "node:module";
import fs from "node:fs";
import path from "node:path";
import { spawn } from "node:child_process";

const require = createRequire(import.meta.url);
const consoleRoot = process.cwd();
const port = Number(process.env.MOTION_MATRIX_PORT || 5174);
const baseUrl = `http://127.0.0.1:${port}/motion-matrix.html`;
const outputDir = path.resolve(
  consoleRoot,
  "../docs/assets/ui-motion/2026-07-23",
);
const viteBin = path.join(
  consoleRoot,
  "node_modules",
  ".bin",
  process.platform === "win32" ? "vite.cmd" : "vite",
);
const electronCaptureScript = path.join(
  consoleRoot,
  "scripts",
  "capture-motion-electron.cjs",
);
const chromeCaptureScript = path.join(
  consoleRoot,
  "scripts",
  "capture-motion-chrome.mjs",
);

function formatError(error) {
  return error instanceof Error ? error.message : String(error);
}

function isUsableElectronPath(electronPath) {
  if (typeof electronPath !== "string" || !fs.existsSync(electronPath)) {
    return false;
  }

  if (process.platform !== "darwin") {
    return true;
  }

  const contentsDir = path.dirname(path.dirname(electronPath));
  const frameworkPath = path.join(
    contentsDir,
    "Frameworks",
    "Electron Framework.framework",
    "Electron Framework",
  );
  const frameworkExists = fs.existsSync(frameworkPath);
  if (!frameworkExists) {
    process.stderr.write(
      `Bundled Electron is incomplete; missing ${frameworkPath}. Trying Chrome Headless.\n`,
    );
  }
  return frameworkExists;
}

function resolveElectronPath() {
  const electronPathFile = path.join(
    consoleRoot,
    "node_modules",
    "electron",
    "path.txt",
  );
  if (fs.existsSync(electronPathFile)) {
    try {
      const electronPathFromPackage = require("electron");
      if (isUsableElectronPath(electronPathFromPackage)) {
        return electronPathFromPackage;
      }
    } catch (error) {
      process.stderr.write(
        `Electron package path lookup failed; trying bundled binary. ${formatError(error)}\n`,
      );
    }
  } else {
    process.stderr.write(
      `Electron package path file is missing; trying bundled binary at node_modules/electron/dist.\n`,
    );
  }

  const bundledElectron = path.join(
    consoleRoot,
    "node_modules",
    "electron",
    "dist",
    "Electron.app",
    "Contents",
    "MacOS",
    "Electron",
  );
  if (isUsableElectronPath(bundledElectron)) {
    return bundledElectron;
  }

  return null;
}

function spawnProcess(command, args, options = {}) {
  return spawn(command, args, {
    cwd: consoleRoot,
    env: {
      ...process.env,
      BROWSER: "none",
      ELECTRON_DISABLE_SECURITY_WARNINGS: "1",
    },
    stdio: options.stdio || ["ignore", "pipe", "pipe"],
  });
}

function wait(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function waitForServer(url) {
  for (let attempt = 0; attempt < 80; attempt += 1) {
    try {
      const response = await fetch(url);
      if (response.ok) return;
    } catch {
      // Server is still starting.
    }
    await wait(250);
  }
  throw new Error(`Timed out waiting for Vite server at ${url}`);
}

function collectOutput(child) {
  child.stdout?.on("data", (chunk) => {
    process.stdout.write(chunk);
  });
  child.stderr?.on("data", (chunk) => {
    process.stderr.write(chunk);
  });
}

function runCapture(command, args, label) {
  return new Promise((resolve, reject) => {
    const capture = spawnProcess(command, args, { stdio: "inherit" });
    capture.on("exit", (code, signal) => {
      if (code === 0) resolve();
      else reject(new Error(`${label} capture failed with code ${code ?? signal}`));
    });
    capture.on("error", reject);
  });
}

async function runMotionCapture() {
  const electronPath = resolveElectronPath();
  if (electronPath) {
    try {
      await runCapture(
        electronPath,
        [electronCaptureScript, baseUrl, outputDir],
        "Electron",
      );
      return;
    } catch (error) {
      process.stderr.write(
        `Electron capture failed; trying Chrome Headless. ${formatError(error)}\n`,
      );
    }
  }

  await runCapture(
    process.execPath,
    [chromeCaptureScript, baseUrl, outputDir],
    "Chrome Headless",
  );
}

async function run() {
  fs.mkdirSync(outputDir, { recursive: true });

  const vite = spawnProcess(viteBin, [
    "--host",
    "127.0.0.1",
    "--port",
    String(port),
    "--strictPort",
  ]);
  collectOutput(vite);

  let viteExited = false;
  vite.on("exit", (code, signal) => {
    viteExited = true;
    const expectedTermination = signal === "SIGTERM" || code === 143;
    if (code !== 0 && !expectedTermination) {
      process.stderr.write(`Vite exited early with code ${code ?? signal}\n`);
    }
  });

  try {
    await waitForServer(baseUrl);
    if (viteExited) {
      throw new Error("Vite server exited before capture could start.");
    }

    await runMotionCapture();

    process.stdout.write(`Motion matrix screenshots written to ${outputDir}\n`);
  } finally {
    if (!viteExited) {
      vite.kill("SIGTERM");
    }
  }
}

run().catch((error) => {
  console.error(error);
  process.exit(1);
});
